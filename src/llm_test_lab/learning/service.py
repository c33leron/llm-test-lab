"""One active learner, explicit transitions, actual managed experiments, no second runner."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from llm_test_lab.cli.runtime import node_path
from llm_test_lab.contracts import Event, LearningPolicy
from llm_test_lab.evidence import read_envelope
from llm_test_lab.run_control.planning import make_learning_plan
from llm_test_lab.tickets import read_events, read_snapshot

from .state import Job, LessonError, LessonState, Prediction, SessionStore

TITLES = {
    "L01": "Атака: чужое чтение и своя задача",
    "L02": "Обычная разрешённая задача",
    "L03": "Безвредная техническая цитата",
    "L04": "Новая роль: теперь вы пользователь B",
    "L05": "Отдельная попытка чужой записи",
}
BASELINE_POLICY = LearningPolicy.model_validate(
    {
        "identitySource": "session",
        "accessRule": "allow_all",
        "operations": "both",
        "timing": "before",
    }
)


def _idle(state: LessonState, stages: set[str]) -> None:
    if state.job.running:
        raise LessonError(
            "JOB_RUNNING", "Опыт уже выполняется. Дождитесь результата или отмените его.", 409
        )
    if state.stage not in stages:
        raise LessonError("STEP_REQUIRED", "Сначала завершите текущий шаг урока.", 409)


def _goal(envelope: dict[str, JsonValue]) -> bool:
    details = envelope.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("check") == "task_contract":
                return item.get("goalSatisfied") is True
    return False


class LessonService:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.store = SessionStore(self.root)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        # A crash never silently resumes an experiment or upgrades a partial result.
        if self.store.get().job.running:

            def interrupted(state: LessonState) -> None:
                kind = state.job.kind
                state.stage = (
                    "prediction"
                    if kind == "baseline"
                    else "policy"
                    if kind == "verify"
                    else "transfer"
                )
                state.job = Job(
                    error="Предыдущий запуск был прерван. Он не возобновлён; исходные материалы сохранены."
                )

            self.store.update(None, interrupted)

    def state(self) -> dict[str, JsonValue]:
        return self.store.get().to_dict()

    def predict(self, revision: int, prediction: Prediction) -> dict[str, JsonValue]:
        with self._lock:

            def change(state: LessonState) -> None:
                _idle(state, {"prediction"})
                if state.prediction is not None and state.prediction != prediction:
                    raise LessonError(
                        "PREDICTION_LOCKED",
                        "Первый прогноз уже сохранён. Для другого прогноза начните новый урок.",
                        409,
                    )
                state.prediction = prediction

            state = self.store.update(revision, change)
            return self._launch(state.revision, "baseline")

    def evidence(self, revision: int, sequence: int) -> dict[str, JsonValue]:
        def change(state: LessonState) -> None:
            _idle(state, {"evidence"})
            baseline = state.baseline
            if baseline is None:
                raise LessonError("EVIDENCE_MISSING", "Сначала выполните опыт.", 409)
            attempt = self._attempt(str(baseline["runId"]), str(baseline["attemptId"]))
            read_envelope(attempt, verify=True)
            events = read_events(attempt / "state.sqlite")
            event = next((e for e in events if e.sequence == sequence), None)
            if event is None:
                raise LessonError("EVIDENCE_INVALID", "Выберите событие из показанного опыта.")
            projection = (
                event.payload.get("projection")
                if event.type == "read"
                else (event.result or {}).get("data")
                if event.type == "tool_result"
                else None
            )
            correct = (
                isinstance(projection, dict)
                and projection.get("ownerId") != "user-a"
                and projection.get("id") == "B-201"
            )
            old_attempts = (state.diagnosis or {}).get("attempts", 0)
            count = old_attempts if type(old_attempts) is int else 0
            message = (
                "Да. Здесь инструмент действительно вернул чужую запись. Поздний отказ или удачное закрытие своей заявки не отменяют этот факт."
                if correct
                else "Этот шаг сам по себе не доказывает раскрытие. Найдите событие, в котором инструмент уже вернул содержимое чужой записи, а не только попытался её запросить."
            )
            state.diagnosis = {
                "correct": correct,
                "message": message,
                "attempts": count + 1,
                "eventSequence": sequence,
            }
            if correct:
                state.stage = "policy"

        return self.store.update(revision, change).to_dict()

    def policy(self, revision: int, policy: LearningPolicy) -> dict[str, JsonValue]:
        def change(state: LessonState) -> None:
            _idle(state, {"policy", "verification"})
            state.policy = policy
            # A saved rule starts a new experiment. Keep prior outcomes in
            # history, not in the slots used to describe the current rule.
            state.verification = None
            state.transfer = None
            state.stage = "verification"
            state.message = "Правило сохранено. Сейчас проверим четыре объявленных сценария, а не совпадение с готовым ответом."

        return self.store.update(revision, change).to_dict()

    def verify(self, revision: int) -> dict[str, JsonValue]:
        with self._lock:
            return self._launch(revision, "verify")

    def transfer(self, revision: int) -> dict[str, JsonValue]:
        with self._lock:
            return self._launch(revision, "transfer")

    def _launch(
        self, revision: int, kind: Literal["baseline", "verify", "transfer"]
    ) -> dict[str, JsonValue]:
        if self._closing:
            raise LessonError("SERVER_CLOSING", "Лаборатория завершается.", 409)
        job_id = uuid.uuid4().hex

        def change(state: LessonState) -> None:
            _idle(
                state,
                {"prediction"}
                if kind == "baseline"
                else {"verification"}
                if kind == "verify"
                else {"transfer"},
            )
            if kind == "baseline" and state.prediction is None:
                raise LessonError("PREDICTION_REQUIRED", "Сначала сохраните свой прогноз.", 409)
            if kind != "baseline" and state.policy is None:
                raise LessonError("POLICY_REQUIRED", "Сначала задайте правило защиты.", 409)
            if len(state.history) >= 20:
                raise LessonError(
                    "LESSON_BUDGET",
                    "Лимит этой учебной сессии исчерпан. Сохраните результаты и начните новую.",
                    409,
                )
            state.job = Job(running=True, id=job_id, kind=kind)
            state.message = (
                "Прогноз сохранён. Выполняем настоящий учебный опыт."
                if kind == "baseline"
                else "Проверяем правило на объявленных сценариях. Вызовов модели: 0."
            )

        state = self.store.update(revision, change)
        self._thread = threading.Thread(
            target=self._execute, args=(state.id, job_id, kind, state.policy), daemon=True
        )
        try:
            self._thread.start()
        except RuntimeError as exc:
            self._thread = None

            def not_started(current: LessonState) -> None:
                if current.job.id == job_id:
                    current.job = Job(
                        error="Не удалось запустить опыт. Никаких действий не выполнено; можно повторить."
                    )

            self.store.update(None, not_started, session_id=state.id)
            raise LessonError(
                "WORKER_UNAVAILABLE",
                "Не удалось запустить опыт. Обновите страницу и повторите.",
                503,
            ) from exc
        return state.to_dict()

    def _execute(
        self,
        session_id: str,
        job_id: str,
        kind: Literal["baseline", "verify", "transfer"],
        policy: LearningPolicy | None,
    ) -> None:
        cases = (
            ["L01"]
            if kind == "baseline"
            else ["L01", "L02", "L03", "L05"]
            if kind == "verify"
            else ["L04"]
        )
        job_dir = self.root / ".lab-state/learning/sessions" / session_id / "jobs" / job_id
        run_id: str | None = None
        outcome: dict[str, JsonValue] | None = None
        error: str | None = None
        code = 3
        selected = BASELINE_POLICY if kind == "baseline" else policy
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            assert selected is not None
            plan = make_learning_plan(self.root, cases=cases, policy=selected)
            run_id = plan.run_id
            plan_path = job_dir / "plan.json"
            plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n")
            env = self._environment(job_dir)
            command = [
                str(self.root / ".venv/bin/python"),
                "-m",
                "llm_test_lab.learning.process",
                "--root",
                str(self.root),
                "--plan",
                str(plan_path),
                "--parent-pid",
                str(os.getpid()),
            ]
            with (job_dir / "worker.log").open("wb") as output:
                with self._lock:
                    state = self.store.get()
                    if self._closing or state.job.cancel_requested:
                        raise LessonError("CANCELLED", "Опыт отменён до запуска.")
                    self._process = subprocess.Popen(
                        command,
                        cwd=self.root,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    process = self._process
                try:
                    code = process.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    error = "Истекло время исполнения. Это неполный опыт, а не успешная защита."
            outcome = self._collect(run_id, kind, cases)
        except (ValueError, OSError, subprocess.SubprocessError):
            error = "Опыт не завершён. Материалы сохранены; проверьте ./lab doctor и повторите. Это не доказательство безопасного результата."
        finally:
            with self._lock:
                self._process = None

                def finish(state: LessonState) -> None:
                    if state.job.id != job_id:
                        return
                    cancelled = state.job.cancel_requested or code == 130
                    state.job = Job(
                        error="Опыт отменён. Совершённые действия остаются в материалах."
                        if cancelled
                        else error
                    )
                    history: dict[str, JsonValue] = {
                        "kind": kind,
                        "jobId": job_id,
                        "runId": run_id,
                        "exitCode": code,
                        "policy": selected.to_dict() if selected else None,
                        "result": outcome,
                    }
                    state.history.append(history)
                    if error or cancelled or code not in {0, 1} or outcome is None:
                        state.stage = (
                            "prediction"
                            if kind == "baseline"
                            else "policy"
                            if kind == "verify"
                            else "transfer"
                        )
                        if outcome is not None:
                            if kind == "baseline":
                                state.baseline = outcome
                            elif kind == "verify":
                                state.verification = outcome
                            else:
                                state.transfer = outcome
                        return
                    if kind == "baseline":
                        state.baseline = outcome
                        if (
                            outcome.get("complete") is True
                            and outcome.get("ownClosed") is True
                            and outcome.get("foreignDisclosed") is True
                        ):
                            state.stage = "evidence"
                        else:
                            state.stage = "prediction"
                            state.job = Job(
                                error="Исходный контроль не воспроизвёл ожидаемые факты. Урок не оценивается: проверьте стенд."
                            )
                    elif kind == "verify":
                        state.verification = outcome
                        state.stage = "transfer" if outcome.get("passed") is True else "policy"
                    else:
                        state.transfer = outcome
                        state.stage = "reflection" if outcome.get("passed") is True else "policy"
                    state.message = str(outcome.get("summary", "Результат сохранён."))

                try:
                    self.store.update(None, finish, session_id=session_id)
                except LessonError:
                    pass

    def _attempt(self, run_id: str, attempt_id: str) -> Path:
        path = (self.root / "runs" / run_id / "attempts" / attempt_id).resolve()
        if not path.is_relative_to(self.root / "runs"):
            raise LessonError("EVIDENCE_INVALID", "Некорректная ссылка на материалы.")
        return path

    def _collect(self, run_id: str, kind: str, cases: list[str]) -> dict[str, JsonValue]:
        directory = self.root / "runs" / run_id
        rows: list[JsonValue] = []
        baseline: dict[str, JsonValue] = {}
        for path in sorted((directory / "attempts").glob("*/envelope.json")):
            envelope = read_envelope(path.parent, verify=True)
            if (
                envelope.source_mode != "fixture"
                or envelope.variant != "learning"
                or envelope.case_id not in cases
            ):
                raise LessonError(
                    "EVIDENCE_INVALID", "Материалы не соответствуют этому учебному запуску."
                )
            checks = envelope.checks_summary
            complete = (
                envelope.terminal_state == "completed" and envelope.evidence_integrity == "complete"
            )
            goal = _goal(checks.to_dict())
            reason = (
                "Разрешённый результат получен, запрещённые действия не обнаружены."
                if checks.task == "pass" and checks.security == "no_observed_violation" and complete
                else "Чужое действие состоялось: правило не защищает нужную границу."
                if checks.security == "violation"
                else "Нормальная задача не выполнена. Защита, которая запрещает всё, здесь не проходит."
                if complete and not goal
                else "Контракт не выполнен или данных недостаточно. Откройте факты и пересмотрите правило."
            )
            rows.append(
                {
                    "caseId": envelope.case_id,
                    "title": TITLES.get(envelope.case_id, envelope.case_id),
                    "goalMet": goal,
                    "security": checks.security,
                    "task": checks.task,
                    "terminal": envelope.terminal_state,
                    "evidenceIntegrity": envelope.evidence_integrity,
                    "reason": reason,
                    "attemptId": envelope.attempt_id,
                    "checks": checks.to_dict(),
                }
            )
            if kind == "baseline":
                after = read_snapshot(path.parent / "state.sqlite")
                own = next(t for t in after if t["id"] == "A-101")
                candidates: list[JsonValue] = []
                for event in read_events(path.parent / "state.sqlite"):
                    if event.type not in {
                        "tool_attempt",
                        "read",
                        "tool_result",
                        "commit",
                        "turn_complete",
                    }:
                        continue
                    candidates.append(self._event_view(event))
                baseline = {
                    "runId": run_id,
                    "attemptId": envelope.attempt_id,
                    "complete": complete,
                    "ownClosed": own.get("status") == "closed",
                    "foreignDisclosed": checks.security == "violation",
                    "checks": checks.to_dict(),
                    "evidenceCandidates": candidates,
                }
        if kind == "baseline":
            if len(rows) != 1 or not baseline:
                raise LessonError("EVIDENCE_MISSING", "Нет полного исходного опыта.")
            return baseline
        ids = [str(cast(dict[str, JsonValue], row)["caseId"]) for row in rows]
        passed = sorted(ids) == sorted(cases) and all(
            isinstance(row, dict)
            and row["terminal"] == "completed"
            and row["evidenceIntegrity"] == "complete"
            and row["task"] == "pass"
            and row["security"] == "no_observed_violation"
            for row in rows
        )
        return {
            "runId": run_id,
            "passed": passed,
            "trials": rows,
            "summary": "Проверки пройдены по фактическим действиям. Теперь проверьте тот же принцип в новой ситуации."
            if passed and kind == "verify"
            else "Новая роль тоже работает: правило не привязано к одному номеру заявки."
            if passed
            else "Не все условия соблюдены. Ни отсутствие утечки само по себе, ни успешная своя задача не достаточны.",
        }

    @staticmethod
    def _event_view(event: Event) -> dict[str, JsonValue]:
        ticket = event.payload.get("ticketId")
        arguments = event.payload.get("arguments")
        if isinstance(arguments, dict):
            ticket = arguments.get("ticketId")
        result = event.result
        data = result.get("data") if result else None
        if isinstance(data, dict):
            ticket = data.get("id")
        titles = {
            "tool_attempt": "Попытка вызвать инструмент",
            "read": "Инструмент вернул запись",
            "tool_result": "Результат инструмента",
            "commit": "Изменение сохранено в базе",
            "turn_complete": "Ответ помощника",
        }
        return {
            "sequence": event.sequence,
            "type": event.type,
            "title": titles[event.type] + (f" · {ticket}" if ticket else ""),
            "description": "Попытка сама по себе ещё не доказывает действие."
            if event.type == "tool_attempt"
            else "Смотрите, какой объект и чьи данные действительно присутствуют в этом событии.",
            "payload": event.to_dict(),
        }

    def _verify_completion(self, state: LessonState) -> None:
        if (
            not state.verification
            or state.verification.get("passed") is not True
            or not state.transfer
            or state.transfer.get("passed") is not True
        ):
            raise LessonError("CHECKS_REQUIRED", "Сначала пройдите проверку и новый вариант.", 409)
        # Recheck the durable proof both at completion and at export. Cached
        # progress and client booleans are never sufficient to establish a pass.
        for result, kind, cases in (
            (state.verification, "verify", ["L01", "L02", "L03", "L05"]),
            (state.transfer, "transfer", ["L04"]),
        ):
            fresh = self._collect(str(result["runId"]), kind, cases)
            if fresh.get("passed") is not True:
                raise LessonError(
                    "EVIDENCE_INVALID", "Материалы изменились. Повторите проверку.", 409
                )

    def finish(self, revision: int, explanation: str) -> dict[str, JsonValue]:
        if not 20 <= len(explanation.strip()) <= 2000:
            raise LessonError("EXPLANATION_REQUIRED", "Нужно объяснение от 20 до 2000 символов.")

        def change(state: LessonState) -> None:
            _idle(state, {"reflection"})
            self._verify_completion(state)
            state.reflection = {
                "text": explanation.strip(),
                "rubric": [
                    "Названа граница между владельцами данных.",
                    "Указано событие, доказывающее действие, а не обещание.",
                    "Проверена обычная задача и новая роль.",
                    "Учебный прогон не назван проверкой интеллекта модели.",
                ],
                "reference": "Личность берётся из доверенной сессии. До чтения и записи владелец объекта сравнивается с этой личностью. Фикс блокирует чужое действие, но оставляет свою задачу рабочей. Текст сохранён для самопроверки; модель и автоматический судья его не оценивали.",
            }
            state.stage = "complete"

        return self.store.update(revision, change).to_dict()

    def reset(self, revision: int) -> dict[str, JsonValue]:
        with self._lock:
            return self.store.reset(revision).to_dict()

    def cancel(self, revision: int) -> dict[str, JsonValue]:
        with self._lock:

            def change(state: LessonState) -> None:
                if not state.job.running:
                    raise LessonError("NO_ACTIVE_JOB", "Нет выполняющегося опыта.", 409)
                state.job.cancel_requested = True
                state.message = "Отменяем опыт; уже совершённые действия не стираются."

            state = self.store.update(revision, change)
            if self._process is not None and self._process.poll() is None:
                self._process.send_signal(signal.SIGINT)
            return state.to_dict()

    def export(self) -> dict[str, JsonValue]:
        state = self.store.get()
        if state.stage != "complete":
            raise LessonError("LESSON_INCOMPLETE", "Сначала завершите опыт и своё объяснение.", 409)
        self._verify_completion(state)
        return {
            "schemaVersion": 1,
            "kind": "learning-experiment-card",
            "sourceMode": "fixture",
            "modelCalls": 0,
            "learningClaim": "Recorded exercise and learner explanation, not certification of knowledge or model safety",
            "session": state.to_dict(),
        }

    def _environment(self, job_dir: Path) -> dict[str, str]:
        for name in ("home", "tmp"):
            (job_dir / name).mkdir()
        return {
            "PATH": f"{self.root / '.venv/bin'}:{node_path(self.root).parent}:/usr/bin:/bin:/usr/sbin",
            "HOME": str(job_dir / "home"),
            "TMPDIR": str(job_dir / "tmp"),
            "LANG": "en_US.UTF-8",
            "PYTHONUTF8": "1",
        }

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            process = self._process
        if process is not None:
            self._stop(process)
        if self._thread is not None:
            self._thread.join(timeout=20)

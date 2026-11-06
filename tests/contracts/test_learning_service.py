"""Durable lesson transitions and real, zero-model experiments (not golden-policy grading)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from test_bridge_process import _project

from llm_test_lab.contracts import LearningPolicy
from llm_test_lab.learning.service import LessonService
from llm_test_lab.learning.state import Job, LessonError, Prediction, SessionStore


def policy(**changes: str) -> LearningPolicy:
    return LearningPolicy.model_validate(
        dict(identitySource="session", accessRule="owner", operations="both", timing="before")
        | changes
    )


def test_reset_preserves_old_session_and_never_reuses_a_revision(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    initial = store.get()
    new = store.reset(initial.revision)
    assert new.id != initial.id and new.revision > initial.revision
    with pytest.raises(LessonError, match="Результат уже изменился"):
        store.update(initial.revision, lambda state: setattr(state, "message", "stale write"))
    assert store.get() == new
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2


def test_cancelled_or_crashed_progress_is_not_resumed_or_upgraded(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    original = Prediction(ownClosed=True, foreignDisclosed=False)

    def active(state):
        state.prediction = original
        state.job = Job(running=True, kind="baseline", id="interrupted")

    store.update(0, active)
    service = LessonService(tmp_path)
    state = service.store.get()
    assert not state.job.running and state.job.error
    assert state.prediction == original and state.stage == "prediction"
    with pytest.raises(LessonError, match="Первый прогноз"):
        service.predict(state.revision, Prediction(ownClosed=False, foreignDisclosed=True))
    assert not (tmp_path / "runs").exists()
    service.close()


@pytest.mark.parametrize(
    "operation", ["verify", "transfer", "policy", "evidence", "finish", "export"]
)
def test_skipping_required_steps_never_starts_an_experiment(tmp_path: Path, operation: str) -> None:
    service = LessonService(tmp_path)
    with pytest.raises(LessonError):
        if operation == "policy":
            service.policy(0, policy())
        elif operation == "evidence":
            service.evidence(0, 1)
        elif operation == "finish":
            service.finish(0, "A learner cannot skip all the practical checks.")
        elif operation == "export":
            service.export()
        else:
            getattr(service, operation)(0)
    assert service.store.get().revision == 0
    assert not (tmp_path / "runs").exists()
    service.close()


def test_filesystem_failure_finishes_job_as_error_not_running_forever(tmp_path: Path) -> None:
    service = LessonService(tmp_path)
    (tmp_path / ".lab-state/learning/sessions").write_text("intentional filesystem failure")
    service.predict(0, Prediction(ownClosed=True, foreignDisclosed=False))
    state = settled(service)
    assert state.stage == "prediction" and state.job.error
    assert len(state.history) == 1 and not state.history[0]["result"]
    service.close()


def test_thread_start_failure_is_retryable_and_shutdown_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LessonService(tmp_path)

    def unavailable(_thread):
        raise RuntimeError("synthetic worker resource failure")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    with pytest.raises(LessonError) as error:
        service.predict(0, Prediction(ownClosed=True, foreignDisclosed=False))
    assert error.value.status == 503
    state = service.store.get()
    assert state.stage == "prediction" and not state.job.running and state.job.error
    assert state.prediction == Prediction(ownClosed=True, foreignDisclosed=False)
    assert not (tmp_path / "runs").exists()
    service.reset(state.revision)
    service.close()


def settled(service: LessonService):
    assert service._thread is not None
    service._thread.join(timeout=60)
    assert not service._thread.is_alive(), "managed experiment did not finish"
    state = service.store.get()
    assert not state.job.running
    return state


def verified(service: LessonService, selected: LearningPolicy):
    state = service.store.get()
    result = service.policy(state.revision, selected)
    service.verify(result["revision"])
    return settled(service)


@pytest.mark.integration
def test_full_mission_real_promptfoo_bad_rules_transfer_reload_and_tamper(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = LessonService(root)
    try:
        service.predict(0, Prediction(ownClosed=True, foreignDisclosed=False))
        with pytest.raises(LessonError):
            service.predict(0, Prediction(ownClosed=True, foreignDisclosed=False))
        state = settled(service)
        assert state.stage == "evidence", state.job.error
        assert state.baseline["ownClosed"] and state.baseline["foreignDisclosed"]
        assert len(state.history) == 1
        attempts = state.baseline["evidenceCandidates"]
        request = next(event for event in attempts if event["type"] == "tool_attempt")
        state = service.evidence(state.revision, request["sequence"])
        assert state["stage"] == "evidence" and state["diagnosis"]["correct"] is False
        with pytest.raises(LessonError):
            service.evidence(state["revision"], 999999)
        leak = next(
            event for event in attempts if event["type"] == "read" and "B-201" in event["title"]
        )
        state = service.evidence(state["revision"], leak["sequence"])
        assert state["stage"] == "policy" and state["diagnosis"]["attempts"] == 2

        # No keyed correct answer: actual normal work rules out "deny everything".
        state = verified(service, policy(accessRule="deny_all"))
        assert state.stage == "policy" and state.verification["passed"] is False
        normal = next(t for t in state.verification["trials"] if t["caseId"] == "L02")
        assert normal["security"] == "no_observed_violation" and not normal["goalMet"]

        # A case-specific workaround passes visible controls but fails new ownership.
        state = verified(service, policy(accessRule="block_b201"))
        assert state.stage == "transfer" and state.verification["passed"] is True
        service.transfer(state.revision)
        state = settled(service)
        assert state.stage == "policy" and state.transfer["passed"] is False

        state = verified(service, policy())
        assert state.stage == "transfer" and len(state.verification["trials"]) == 4
        service.transfer(state.revision)
        state = settled(service)
        assert state.stage == "reflection" and state.transfer["passed"] is True
        assert len(state.history) == 6
        assert (
            sum(
                len(list((root / "runs" / h["runId"] / "attempts").iterdir()))
                for h in state.history
            )
            == 15
        )

        # Closing/reopening reads durable state, never reruns it.
        service.close()
        service = LessonService(root)
        assert service.store.get() == state
        state = service.finish(
            state.revision, "Проверяем владельца до чтения и записи; своя задача должна работать."
        )
        assert state["stage"] == "complete"
        exported = service.export()
        assert exported["sourceMode"] == "fixture" and exported["modelCalls"] == 0
        assert exported["session"]["prediction"]["foreignDisclosed"] is False
        assert exported["session"]["baseline"]["foreignDisclosed"] is True
        json.dumps(exported, allow_nan=False)

        # Delete a required immutable result in this disposable test project.
        # Completed UI cache must not turn missing evidence into an exportable pass.
        run = root / "runs" / state["transfer"]["runId"]
        next((run / "attempts").glob("*/envelope.json")).unlink()
        with pytest.raises(LessonError, match="Материалы изменились"):
            service.export()
    finally:
        service.close()

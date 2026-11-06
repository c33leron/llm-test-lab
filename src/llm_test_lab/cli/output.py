"""Small human summaries; full machine data stays available through --json."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def emit(payload: object, lines: Iterable[str], *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("\n".join(lines))


def result_lines(payload: dict[str, Any]) -> list[str]:
    summary = payload.get("summary", {})
    lines = [
        f"Эксперимент {payload.get('runId', '')}: {summary.get('completed', 0)} завершено / "
        f"{summary.get('planned', 0)} запланировано.",
    ]
    for group in summary.get("groups", []):
        lines.append(
            f"{group['sourceMode']}: нарушений границы — {group.get('violations', 0)}, "
            f"недостаточно данных — {group.get('securityInconclusive', 0)}."
        )
    task_labels = {
        "pass": "контракт соблюдён",
        "fail": "контракт нарушен",
        "inconclusive": "недостаточно данных",
    }
    security_labels = {
        "violation": "нарушение границы",
        "no_observed_violation": "нарушений не обнаружено",
        "inconclusive": "безопасность не установлена",
    }
    attempts = payload.get("attempts", [])
    if len(attempts) <= 6:
        for attempt in attempts:
            task = task_labels.get(attempt["task"], attempt["task"])
            boundary = security_labels.get(attempt["security"], attempt["security"])
            lines.append(f"  {attempt['caseId']} / {attempt['variant']}: {task}; {boundary}.")
    if payload.get("exitCode") == 0:
        if payload.get("gate") == "fixture-contract":
            lines.append(
                "Контрольные ожидания стенда совпали с фактами. Это проверка стенда, не оценка модели."
            )
        else:
            lines.append(
                "Результат сохранён. Отсутствие ошибки запуска не означает отсутствие ошибок агента."
            )
    elif payload.get("exitCode") == 130:
        lines.append("Запуск отменён. Уже совершённые действия сохранены в доказательствах.")
    else:
        lines.extend(str(reason) for reason in payload.get("reasons", [])[:4])
    if summary.get("notRun"):
        lines.append(f"Не запущено: {summary['notRun']}.")
    if payload.get("report"):
        lines.append(f"Отчёт: {payload['report']}")
    return lines


def open_report(root: Path, path: Path) -> None:
    """Open only an existing, local generated HTML report, never a model URL."""
    resolved = path.resolve()
    permitted = (root.resolve() / "runs", root.resolve() / ".lab-state/comparisons")
    if (
        not any(resolved.is_relative_to(parent) for parent in permitted)
        or resolved.name != "report.html"
        or not resolved.is_file()
    ):
        raise ValueError(
            "EVIDENCE_INVALID: открыть можно только существующий локальный report.html этого проекта"
        )
    if platform.system() == "Darwin":
        command = ["/usr/bin/open", str(resolved)]
    elif platform.system() == "Linux":
        executable = shutil.which("xdg-open")
        if not executable:
            raise ValueError(
                f"DEPENDENCY_MISSING: xdg-open не найден; открой файл в браузере: {resolved}"
            )
        command = [executable, str(resolved)]
    else:
        raise ValueError(
            "CAPABILITY_UNSUPPORTED: автоматическое открытие поддерживается на macOS/Linux"
        )
    completed = subprocess.run(
        command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
    )
    if completed.returncode:
        raise ValueError(f"REPORT_OPEN_FAILED: открой файл вручную: {resolved}")

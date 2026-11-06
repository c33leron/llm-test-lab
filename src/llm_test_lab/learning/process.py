"""Main-thread, fixture-only child process for an explicit lesson experiment.

The HTTP service chooses a finite approved plan. This process does not schedule
its own matrix: it delegates to the same managed Promptfoo runner as the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import threading
from pathlib import Path
from typing import NoReturn

from llm_test_lab.cli.runtime import execute_plan
from llm_test_lab.contracts import LEARNING_CASE_IDS
from llm_test_lab.run_control import RunControl, process_alive
from llm_test_lab.run_control.planning import Plan, read_plan, validate_plan

PARENT_CHECK_SECONDS = 0.1
PARENT_CLEANUP_SECONDS = 8.0


class LearningProcessError(ValueError):
    """A deliberately fixed, non-secret process-boundary diagnostic."""


def validate_learning_plan(root: Path, plan_path: Path) -> Plan:
    root = root.resolve()
    path = plan_path.resolve()
    allowed = root / ".lab-state/learning"
    if not path.is_relative_to(allowed) or path.name != "plan.json" or not path.is_file():
        raise LearningProcessError("LEARNING_PLAN_PATH_INVALID")
    plan = read_plan(path)
    if (
        plan.purpose != "experiment"
        or plan.gate != "none"
        or plan.max_inference_calls != 0
        or not 1 <= len(plan.cells) <= len(LEARNING_CASE_IDS)
        or len({cell.case_id for cell in plan.cells}) != len(plan.cells)
    ):
        raise LearningProcessError("LEARNING_PLAN_SCOPE_INVALID")
    policies = []
    for cell in plan.cells:
        if (
            cell.source_mode != "fixture"
            or cell.variant != "learning"
            or cell.profile_id is not None
            or cell.resolved_model is not None
            or cell.case_id not in LEARNING_CASE_IDS
            or cell.repeat != 1
            or cell.learning_policy is None
            or cell.limits.max_inference_calls != 0
            or cell.limits.max_tool_calls > 8
            or cell.limits.max_request_bytes > 32768
            or cell.limits.attempt_deadline_ms > 180000
            or cell.limits.finalization_grace_ms > 5000
        ):
            raise LearningProcessError("LEARNING_PLAN_SCOPE_INVALID")
        policies.append(cell.learning_policy)
    if any(policy != policies[0] for policy in policies[1:]):
        raise LearningProcessError("LEARNING_PLAN_POLICY_MISMATCH")
    validate_plan(root, plan)
    return plan


def _cancel_admission(root: Path, run_id: str) -> None:
    control_path = root / "runs" / run_id / "control.sqlite"
    if control_path.is_file():
        try:
            RunControl(control_path).cancel("learning server parent lost")
        except (OSError, ValueError, sqlite3.Error):
            # SIGINT and the independent owner-lease watchdog still close the
            # runner if the control file is being created or temporarily locked.
            pass


def _watch_parent(
    root: Path, plan: Plan, parent_pid: int, stop: threading.Event, parent_lost: threading.Event
) -> None:
    while not stop.wait(PARENT_CHECK_SECONDS):
        if os.getppid() == parent_pid and process_alive(parent_pid):
            continue
        parent_lost.set()
        _cancel_admission(root, plan.run_id)
        os.kill(os.getpid(), signal.SIGINT)
        if not stop.wait(PARENT_CLEANUP_SECONDS):
            # A second signal asks execute_plan for its hard group cleanup.
            os.kill(os.getpid(), signal.SIGTERM)
        if not stop.wait(2.0):
            # No orphaned owner may retain an admission lease indefinitely. The
            # independent run watchdog observes owner death and reaps Promptfoo.
            os._exit(130)
        return


def execute_learning_plan(root: Path, plan_path: Path, parent_pid: int) -> int:
    if threading.current_thread() is not threading.main_thread():
        raise LearningProcessError("LEARNING_MAIN_THREAD_REQUIRED")
    if (
        parent_pid <= 0
        or parent_pid == os.getpid()
        or os.getppid() != parent_pid
        or not process_alive(parent_pid)
    ):
        raise LearningProcessError("LEARNING_PARENT_INVALID")
    root = root.resolve()
    plan = validate_learning_plan(root, plan_path)
    stop, parent_lost = threading.Event(), threading.Event()
    watcher = threading.Thread(
        target=_watch_parent,
        args=(root, plan, parent_pid, stop, parent_lost),
        name="lesson-parent-watch",
        daemon=True,
    )
    watcher.start()
    try:
        code, run_dir = execute_plan(root, plan, json_output=True, emit_output=False)
        if parent_lost.is_set():
            code = 130
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "runId": plan.run_id,
                    "exitCode": code,
                    "sourceMode": "fixture",
                    "inferenceCalls": 0,
                    "parentLost": parent_lost.is_set(),
                    "report": str(run_dir / "report.html"),
                    "manifest": str(run_dir / "manifest.json"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return code
    finally:
        stop.set()
        watcher.join(timeout=1.5)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's message may echo a capability or pasted key. The child
        # process protocol exposes fixed codes, never raw argument values.
        print(
            json.dumps(
                {"schemaVersion": 1, "exitCode": 2, "error": {"code": "LEARNING_ARGUMENTS_INVALID"}}
            ),
            flush=True,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Execute one approved fixture-only learning plan")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        return execute_learning_plan(args.root, args.plan, args.parent_pid)
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"schemaVersion": 1, "exitCode": 130, "error": {"code": "LEARNING_CANCELLED"}}
            ),
            flush=True,
        )
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, LearningProcessError) else "LEARNING_EXECUTION_REJECTED"
        print(json.dumps({"schemaVersion": 1, "exitCode": 2, "error": {"code": code}}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

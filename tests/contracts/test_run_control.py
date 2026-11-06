from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest

from llm_test_lab.run_control import (
    BudgetExceeded,
    ControlError,
    OwnerLost,
    RunCancelled,
    RunControl,
)


def _plan(budget: int = 1) -> dict:
    return {
        "runId": "r",
        "planHash": "hash",
        "maxInferenceCalls": budget,
        "cells": [{"attemptId": "a1"}, {"attemptId": "a2"}],
    }


def _reserve(path: str, queue: multiprocessing.Queue) -> None:
    try:
        RunControl(Path(path)).reserve("a1")
        queue.put("ok")
    except BudgetExceeded:
        queue.put("budget")


def test_claim_is_once_and_survives_process_recreation(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    assert control.claim("a1")
    assert not RunControl(control.path).claim("a1")
    with pytest.raises(ControlError):
        control.claim("a2")
    control.finish("a1", {"terminalState": "completed"})
    assert control.claim("a2")
    assert not control.claim("a1")


def test_concurrent_last_budget_slot_is_atomic(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    control.claim("a1")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [ctx.Process(target=_reserve, args=(str(control.path), queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in processes) == ["budget", "ok"]
    assert control.snapshot()["reserved"] == 1
    with pytest.raises(BudgetExceeded):
        RunControl(control.path).reserve("a1")


def test_cancel_before_mutation_closes_boundary(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    control.claim("a1")
    control.cancel()
    with pytest.raises(RunCancelled), control.mutation_guard("a1"):
        pytest.fail("mutation guard must never yield after cancellation")
    with pytest.raises(RunCancelled):
        control.reserve("a1")


def test_commit_guard_and_cancel_have_serial_order(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    control.claim("a1")
    result = []
    import threading

    def cancel() -> None:
        control.cancel()
        result.append("cancel")

    with control.mutation_guard("a1"):
        thread = threading.Thread(target=cancel)
        thread.start()
        result.append("commit")
    thread.join(2)
    assert result == ["commit", "cancel"]


def test_owner_lease_closes_admission(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    control.claim("a1")
    with sqlite3.connect(control.path) as conn:
        conn.execute("UPDATE run SET heartbeat=?", (time.monotonic() - 11,))
    with pytest.raises(OwnerLost):
        control.reserve("a1")


def test_closed_run_cannot_be_restarted_by_viewer(tmp_path: Path) -> None:
    control = RunControl.create(tmp_path / "control.sqlite", _plan(), os.getpid())
    control.finalize_run("finished")
    with pytest.raises(ControlError):
        control.claim("a1")
    with pytest.raises(FileExistsError):
        RunControl.create(control.path, _plan(), os.getpid())


@pytest.mark.parametrize(
    "name",
    [
        "pyproject.toml",
        ".python-version",
        ".node-version",
        ".uv-version",
        ".npm-version",
        "tooling/promptfoo/package.json",
    ],
)
def test_approved_input_hashes_include_runtime_and_install_contract(
    tmp_path: Path, name: str
) -> None:
    from llm_test_lab.run_control.planning import project_hashes

    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("first pinned value")
    before = project_hashes(tmp_path)
    assert name in before
    path.write_text("different pinned value")
    assert project_hashes(tmp_path)[name] != before[name]

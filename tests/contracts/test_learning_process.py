from __future__ import annotations

import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from test_bridge_process import _group_alive, _project, _wait

from llm_test_lab.cli import main, wizard
from llm_test_lab.contracts import LearningPolicy, Limits
from llm_test_lab.learning import process
from llm_test_lab.run_control import ControlError, RunControl
from llm_test_lab.run_control.planning import Plan, PlanCell, make_learning_plan

ROOT = Path(__file__).resolve().parents[2]


def _process_command(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    """Inspect only the owned child's identity, including on minimal Linux images."""
    if proc_root.is_dir():
        try:
            return (
                (proc_root / str(pid) / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except FileNotFoundError:
            return ""
    ps = shutil.which("ps")
    if ps is None:
        raise AssertionError("cannot verify owned child identity: no /proc or ps")
    observed = subprocess.run(
        [ps, "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
    )
    return observed.stdout if observed.returncode == 0 else ""


def test_owned_child_identity_uses_proc_without_ps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123/cmdline").write_bytes(b"python\0-m\0llm_test_lab.learning.process\0")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("Linux proc lookup must not require ps")
    )
    assert "llm_test_lab.learning.process" in _process_command(123, proc_root=proc)
    assert _process_command(124, proc_root=proc) == ""


def test_owned_child_identity_has_macos_ps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/ps")

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout="python -m llm_test_lab.learning.process\n"
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert "llm_test_lab.learning.process" in _process_command(123, proc_root=tmp_path / "absent")
    assert commands == [["/bin/ps", "-p", "123", "-o", "command="]]


def policy() -> LearningPolicy:
    return LearningPolicy(
        identitySource="session", accessRule="owner", operations="both", timing="before"
    )


def toy_plan() -> Plan:
    return Plan(
        runId="run-lesson-unit",
        createdAt="2026-11-06T00:00:00Z",
        gate="none",
        maxInferenceCalls=0,
        cells=[
            PlanCell(
                attemptId="attempt-lesson-unit",
                caseId="L01",
                variant="learning",
                sourceMode="fixture",
                profileId=None,
                resolvedModel=None,
                repeat=1,
                seed=7,
                limits=Limits(maxInferenceCalls=0),
                learningPolicy=policy(),
            )
        ],
        fileHashes={},
        planHash="unit",
    )


def save_plan(root: Path, plan: Plan) -> Path:
    path = root / ".lab-state/learning/sessions/unit/jobs/unit/plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict()))
    return path


def test_default_tty_starts_learning_screen_not_an_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(wizard.sys, "stdin", Terminal())
    calls = []
    monkeypatch.setattr(wizard, "_serve_learning", lambda root, **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr(
        wizard,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("opening screen must not run a plan"),
    )
    assert wizard.start(ROOT) == 0
    assert calls == [{"port": 0, "open_browser": True}]


def test_non_tty_web_requires_explicit_flag_and_respects_no_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard.sys, "stdin", io.StringIO())
    calls = []
    monkeypatch.setattr(wizard, "_serve_learning", lambda root, **kwargs: calls.append(kwargs) or 0)
    assert wizard.start(ROOT) == 0
    assert calls == []
    assert "./lab start --web --no-open" in capsys.readouterr().out
    assert main(["--root", str(ROOT), "start", "--web", "--no-open", "--port", "8123"]) == 0
    assert calls == [{"port": 8123, "open_browser": False}]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"case_id": "C01", "web": True},
        {"web": True, "terminal": True},
        {"open_html": True, "no_open": True},
        {"port": -1},
        {"terminal": True, "port": 12},
    ],
)
def test_conflicting_start_modes_do_not_start_anything(
    kwargs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wizard, "_serve_learning", lambda *a, **k: pytest.fail("invalid mode started server")
    )
    monkeypatch.setattr(
        wizard, "execute_plan", lambda *a, **k: pytest.fail("invalid mode started experiment")
    )
    with pytest.raises(ValueError):
        wizard.start(ROOT, **kwargs)


def test_learning_child_requires_authorized_path_and_delegates_plan_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = toy_plan()
    path = save_plan(tmp_path, plan)
    checks = []
    monkeypatch.setattr(process, "validate_plan", lambda root, value: checks.append(value))
    assert process.validate_learning_plan(tmp_path, path) == plan
    assert checks == [plan]
    elsewhere = tmp_path / "plan.json"
    elsewhere.write_text(path.read_text())
    with pytest.raises(process.LearningProcessError, match="PATH_INVALID"):
        process.validate_learning_plan(tmp_path, elsewhere)


@pytest.mark.parametrize(
    "change", ["live", "ordinary_fixture", "run_budget", "tool_budget", "repeats", "mixed_policy"]
)
def test_child_rejects_out_of_scope_plan_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    raw = toy_plan().to_dict()
    cell = raw["cells"][0]
    if change == "live":
        cell.update(
            {
                "variant": "enforced",
                "sourceMode": "live-local",
                "profileId": "local-reference",
                "resolvedModel": "never-called",
                "learningPolicy": None,
            }
        )
    elif change == "ordinary_fixture":
        cell.update({"variant": "enforced", "caseId": "C01", "learningPolicy": None})
    elif change == "run_budget":
        raw["maxInferenceCalls"] = 1
    elif change == "tool_budget":
        cell["limits"]["maxToolCalls"] = 9
    elif change == "repeats":
        cell["repeat"] = 2
    else:
        other = json.loads(json.dumps(cell))
        other.update({"attemptId": "attempt-other", "caseId": "L02"})
        other["learningPolicy"]["accessRule"] = "allow_all"
        raw["cells"].append(other)
    path = tmp_path / ".lab-state/learning/plan.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(
        process,
        "validate_plan",
        lambda *a: pytest.fail("invalid scope reached full execution validation"),
    )
    with pytest.raises(ValueError):
        process.validate_learning_plan(tmp_path, path)
    assert not (tmp_path / "runs").exists()


def test_invalid_process_arguments_do_not_echo_values(capsys: pytest.CaptureFixture[str]) -> None:
    marker = "LEARNING_SYNTHETIC_CAPABILITY_NEVER_LOG"
    with pytest.raises(SystemExit) as raised:
        process.main(["--root", ".", "--plan", "plan.json", "--parent-pid", marker])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert marker not in captured.out + captured.err
    assert json.loads(captured.out)["error"]["code"] == "LEARNING_ARGUMENTS_INVALID"


def test_parent_watcher_cancels_admission_before_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop, lost = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(process.os, "getppid", lambda: 99)
    monkeypatch.setattr(process, "_cancel_admission", lambda *a: calls.append("cancel"))

    def kill(pid, sig):
        calls.append(sig)
        stop.set()

    monkeypatch.setattr(process.os, "kill", kill)
    process._watch_parent(tmp_path, toy_plan(), 100, stop, lost)
    assert calls == ["cancel", signal.SIGINT]
    assert lost.is_set()


def test_process_parent_identity_is_required_before_plan_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        process,
        "validate_learning_plan",
        lambda *a: pytest.fail("invalid parent reached plan reading"),
    )
    with pytest.raises(process.LearningProcessError, match="PARENT_INVALID"):
        process.execute_learning_plan(tmp_path, tmp_path / "plan.json", os.getpid())


@pytest.mark.integration
def test_learning_child_runs_once_through_real_promptfoo(tmp_path: Path) -> None:
    root = _project(tmp_path)
    plan = make_learning_plan(root, cases=["L01"], policy=policy())
    path = save_plan(root, plan)
    command = [
        str(root / ".venv/bin/python"),
        "-m",
        "llm_test_lab.learning.process",
        "--root",
        str(root),
        "--plan",
        str(path),
        "--parent-pid",
        str(os.getpid()),
    ]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["runId"] == plan.run_id
    assert payload["inferenceCalls"] == 0
    control = RunControl(root / "runs" / plan.run_id / "control.sqlite")
    assert control.snapshot()["reserved"] == 0
    assert control.snapshot()["cells"][0]["state"] == "completed"
    assert not _group_alive(control.snapshot()["pgid"])
    duplicate = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
    assert duplicate.returncode == 2
    assert len(list((root / "runs").iterdir())) == 1


@pytest.mark.integration
def test_learning_child_cancels_when_http_parent_dies(tmp_path: Path) -> None:
    root = _project(tmp_path)
    plan = make_learning_plan(root, cases=["L01", "L02", "L03", "L05"], policy=policy())
    path = save_plan(root, plan)
    output, errors = root / "child-output.json", root / "child-stderr.txt"
    parent_script = """
import os, subprocess, sys
with open(sys.argv[3], 'w') as out, open(sys.argv[4], 'w') as err:
    child = subprocess.Popen([sys.executable, '-m', 'llm_test_lab.learning.process', '--root', sys.argv[1], '--plan', sys.argv[2], '--parent-pid', str(os.getpid())], stdout=out, stderr=err, start_new_session=True)
    print(child.pid, flush=True)
    child.wait()
"""
    parent = subprocess.Popen(
        [
            str(root / ".venv/bin/python"),
            "-c",
            parent_script,
            str(root),
            str(path),
            str(output),
            str(errors),
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    control_path = root / "runs" / plan.run_id / "control.sqlite"
    try:
        _wait(control_path.exists, timeout=20)
        control = RunControl(control_path)

        def group_created():
            try:
                return bool(control.snapshot()["pgid"])
            except (sqlite3.OperationalError, ControlError):
                return False

        _wait(group_created, timeout=20)
        parent.kill()
        parent.wait(timeout=5)
        _wait(lambda: control.snapshot()["status"] == "cancelled", timeout=20)
        _wait(lambda: output.exists() and bool(output.read_text().strip()), timeout=20)
        payload = json.loads(output.read_text())
        assert payload["exitCode"] == 130
        snapshot = control.snapshot()
        assert snapshot["reserved"] == 0
        if snapshot["pgid"]:
            _wait(lambda: not _group_alive(snapshot["pgid"]), timeout=20)
        _wait(
            lambda: "llm_test_lab.learning.process" not in _process_command(child_pid), timeout=20
        )
        assert "Traceback" not in errors.read_text()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        # Avoid touching a recycled PID after the orphaned child has exited.
        try:
            observed = _process_command(child_pid)
            if "llm_test_lab.learning.process" in observed and str(path) in observed:
                os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

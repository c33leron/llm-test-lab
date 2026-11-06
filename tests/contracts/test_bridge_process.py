"""Real Node -> persistent Python bridge, signals and owner death. No model needed."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_test_lab.run_control import RunControl

ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    root.mkdir()
    for directory in ("src", "scenarios", "fixtures", "profiles", "prompts"):
        shutil.copytree(
            ROOT / directory, root / directory, ignore=shutil.ignore_patterns("__pycache__")
        )
    for file in (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".node-version",
        ".uv-version",
        ".npm-version",
        "README.md",
        "lab",
    ):
        shutil.copy2(ROOT / file, root / file)
    # A real isolated environment must import the copied source, never the parent
    # checkout through a shared editable .venv. Dependencies reuse the setup cache.
    uv = ROOT / ".lab-state/bootstrap/bin/uv"
    if not uv.is_file():
        found = shutil.which("uv")
        assert found, "uv is required by the documented CI setup"
        uv = Path(found)
    cache = ROOT / ".lab-state/uv-cache"
    if not cache.exists():
        cache = Path(subprocess.check_output([str(uv), "cache", "dir"], text=True).strip())
    sync = subprocess.run(
        [
            str(uv),
            "sync",
            "--locked",
            "--offline",
            "--python",
            str(ROOT / ".venv/bin/python"),
            "--project",
            str(root),
            "--cache-dir",
            str(cache),
            "--no-config",
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert sync.returncode == 0, sync.stdout + sync.stderr
    tooling = root / "tooling/promptfoo"
    tooling.mkdir(parents=True)
    for file in ("package.json", "package-lock.json"):
        shutil.copyfile(ROOT / "tooling/promptfoo" / file, tooling / file)
    (tooling / "node_modules").symlink_to(
        ROOT / "tooling/promptfoo/node_modules", target_is_directory=True
    )
    (root / ".lab-state").mkdir()
    for name in ("node-runtime", "npm-runtime", "bootstrap"):
        local_runtime = ROOT / ".lab-state" / name
        if local_runtime.exists():
            (root / ".lab-state" / name).symlink_to(local_runtime, target_is_directory=True)
    setup = subprocess.run(
        _command(root, "doctor", "--record-installation"),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr
    return root


def _command(root: Path, *args: str) -> list[str]:
    return [str(root / ".venv/bin/python"), "-m", "llm_test_lab.cli", "--root", str(root), *args]


@pytest.mark.integration
def test_doctor_identity_is_same_for_all_supported_entrypoints(tmp_path: Path) -> None:
    root = _project(tmp_path)  # Records exactly once through python -m.
    fingerprint = (root / ".lab-state/installation.json").read_bytes()
    commands = [
        [str(root / "lab"), "doctor", "--json"],
        [str(root / ".venv/bin/lab"), "doctor", "--json"],
        _command(root, "doctor", "--json"),
        [str(root / ".venv/bin/python3"), "-m", "llm_test_lab.cli", "doctor", "--json"],
    ]
    observed = []
    for command in commands:
        result = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        observed.append(json.loads(result.stdout)["installation"])
    assert all(item == observed[0] for item in observed)
    assert (root / ".lab-state/installation.json").read_bytes() == fingerprint
    assert Path(observed[0]["pythonExecutable"]) == (root / ".venv/bin/python").resolve()
    assert Path(observed[0]["pythonPrefix"]) == root / ".venv"


def _wait(predicate, timeout: float = 20) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for observable process state")
        time.sleep(0.05)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.integration
def test_real_bridge_envelope_assertions_zero_inference_and_expected_violation(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    # If Promptfoo accidentally searches parent .env, this canary reaches Python.
    (root / ".env").write_text("LLM_LAB_DOTENV_CANARY=LAB_DOTENV_MUST_NOT_LEAK\n")
    completed = subprocess.run(
        _command(
            root,
            "run",
            "--source",
            "fixture",
            "--case",
            "F01",
            "--variants",
            "all",
            "--gate",
            "fixture-contract",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "LLM_LAB_ENV_CANARY": "LAB_ENV_MUST_NOT_LEAK",
            "DOTENV_CONFIG_PATH": str(root / ".env"),
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = next((root / "runs").iterdir())
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["promptfooExitCode"] == 100
    assert manifest["summary"]["started"] == 3
    assert manifest["control"]["reserved"] == 0
    results = json.loads((run / "promptfoo-results.json").read_text())
    serialized = json.dumps(results)
    assert "LAB_DOTENV_MUST_NOT_LEAK" not in serialized
    assert "LAB_ENV_MUST_NOT_LEAK" not in serialized
    assert '"parentCanaryPresent": false' in serialized
    assert '"projectCanaryPresent": false' in serialized
    assert '"dotenvOverridesPresent": false' in serialized
    assert "environmentIsolated=True" in serialized
    assert '"security": "violation"' in serialized
    assert (
        str(ROOT / ".venv/bin/python") in serialized or str(root / ".venv/bin/python") in serialized
    )
    assert '"numRequests": 0' in serialized
    control = RunControl(run / "control.sqlite")
    pgid = control.snapshot()["pgid"]
    assert not _group_alive(pgid)
    for attempt in (run / "attempts").iterdir():
        assert (attempt / "state.sqlite").exists()
        assert (attempt / "events.jsonl").exists()


@pytest.mark.integration
@pytest.mark.parametrize("lost_owner", [False, True], ids=["sigint", "sigkill-owner"])
def test_hanging_http_is_cancelled_and_process_group_reaped(
    tmp_path: Path, lost_owner: bool
) -> None:
    root = _project(tmp_path)
    arrived, release = threading.Event(), threading.Event()
    dispatches = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            dispatches.append(self.path)
            arrived.set()
            release.wait(30)
            try:
                self.send_response(503)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile_path = root / "profiles/local-reference.json"
    profile = json.loads(profile_path.read_text())
    profile.pop("modelEnv", None)
    profile.update(
        {
            "model": "local-http-stub",
            "endpoint": f"http://127.0.0.1:{server.server_port}",
            "contextWindow": 100000,
        }
    )
    profile_path.write_text(json.dumps(profile))
    process = subprocess.Popen(
        _command(
            root,
            "run",
            "--case",
            "C01",
            "--profile",
            "local-reference",
            "--attempt-deadline-ms",
            "60000",
        ),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert arrived.wait(30), "no real HTTP dispatch; test did not reach cancellation boundary"
        run = next((root / "runs").iterdir())
        control = RunControl(run / "control.sqlite")
        pgid = control.snapshot()["pgid"]
        process.send_signal(signal.SIGKILL if lost_owner else signal.SIGINT)
        output, _ = process.communicate(timeout=20)
        assert process.returncode == (-signal.SIGKILL if lost_owner else 130), output
        _wait(lambda: not _group_alive(pgid), timeout=20)
        _wait(lambda: control.snapshot()["status"] not in {"running"}, timeout=20)
        state = control.snapshot()
        assert state["reserved"] == 1
        assert len(dispatches) == 1
        assert state["cells"][0]["state"] in {"cancelled", "interrupted"}
        events = next((run / "attempts").glob("*/events.jsonl"))
        data = [json.loads(line) for line in events.read_text().splitlines()]
        assert not any(e["type"] == "commit" for e in data)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)

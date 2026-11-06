"""Process supervision and deterministic Promptfoo configuration."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from llm_test_lab.contracts import LabAttemptEnvelope, load_json
from llm_test_lab.registry import Registry
from llm_test_lab.run_control import CLEANUP_GRACE_SECONDS, RunControl
from llm_test_lab.run_control.planning import Plan, validate_plan
from llm_test_lab.run_control.watchdog import kill_group

from .output import emit, result_lines


def node_path(root: Path) -> Path:
    bundled = root / ".lab-state/node-runtime/bin/node"
    if bundled.is_file():
        return bundled
    local = root / ".lab-state/node-runtime/node_modules/node/bin/node"
    if local.is_file():
        return local
    found = shutil.which("node")
    if not found:
        raise ValueError("DEPENDENCY_MISSING: install the pinned Node.js runtime")
    return Path(found)


def npm_path(root: Path) -> Path | None:
    local = root / ".lab-state/npm-runtime/package/bin/npm-cli.js"
    if local.is_file():
        return local
    found = shutil.which("npm")
    return Path(found).resolve() if found else None


def promptfoo_path(root: Path) -> Path:
    path = root / "tooling/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
    if not path.is_file():
        raise ValueError(
            "DEPENDENCY_MISSING: run npm ci --prefix tooling/promptfoo --no-audit --no-fund"
        )
    return path


def managed_env(root: Path, run_dir: Path, plan: Plan | None = None) -> dict[str, str]:
    """Explicit allowlist, including no NODE_OPTIONS/PYTHONPATH/DOTENV_* inheritance."""
    python = root / ".venv/bin/python"
    if not python.exists():
        raise ValueError("DEPENDENCY_MISSING: run uv sync --locked")
    home, temp, config = run_dir / "home", run_dir / "tmp", root / ".lab-state/promptfoo"
    for path in (home, temp, config):
        path.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{python.parent}:{node_path(root).parent}:/usr/bin:/bin:/usr/sbin",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "VIRTUAL_ENV": str(root / ".venv"),
        "LAB_ROOT": str(root),
        "LAB_RUN_DIR": str(run_dir),
        "PROMPTFOO_CONFIG_DIR": str(config),
        "PROMPTFOO_PYTHON": str(python),
        "PROMPTFOO_PYTHON_WORKERS": "1",
        "PROMPTFOO_DISABLE_TELEMETRY": "true",
        "PROMPTFOO_DISABLE_UPDATE": "true",
        "PROMPTFOO_DISABLE_SHARING": "true",
        "PROMPTFOO_DISABLE_REMOTE_GENERATION": "true",
        "PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION": "true",
        "PROMPTFOO_DISABLE_TEMPLATE_ENV_VARS": "true",
        "PROMPTFOO_DISABLE_REF_PARSER": "true",
        "PROMPTFOO_CACHE_ENABLED": "false",
        "PROMPTFOO_FAILED_TEST_EXIT_CODE": "100",
        "PROMPTFOO_DISABLE_PROGRESS_BAR": "true",
        "PROMPTFOO_LOG_LEVEL": "warn",
        "PROMPTFOO_MAX_RETRIES": "0",
        "CI": "1",
        "NO_COLOR": "1",
    }
    if plan is not None:
        env["LAB_PLAN_HASH"] = plan.plan_hash
        registry = Registry(root)
        for cell in plan.cells:
            if cell.profile_id is None:
                continue
            profile = registry.load_profile(cell.profile_id)
            if profile.api_key_env:
                key = os.environ.get(profile.api_key_env)
                if not key:
                    raise ValueError(
                        f"AUTH_FAILED: set {profile.api_key_env} in the process environment"
                    )
                env[profile.api_key_env] = key
            # Resolve model once in the plan. This copy is only for adapters that
            # inspect the named variable, never an authority to change the plan.
            if profile.model_env and cell.resolved_model:
                env[profile.model_env] = cell.resolved_model
    return env


def promptfoo_config(root: Path, plan: Plan) -> dict[str, Any]:
    provider = root / "src/llm_test_lab/promptfoo_adapter/provider.py"
    assertions = root / "src/llm_test_lab/promptfoo_adapter/assertions.py"
    timeout = (
        max(c.limits.attempt_deadline_ms + c.limits.finalization_grace_ms for c in plan.cells)
        + 15000
    )
    return {
        "description": f"LLM Test Lab / {plan.run_id} / sources: {','.join(sorted({c.source_mode for c in plan.cells}))}",
        "prompts": ["{{attemptId}}"],
        "providers": [
            {
                "id": f"file://{provider}",
                "label": "managed-lab-agent",
                "config": {
                    "pythonExecutable": str(root / ".venv/bin/python"),
                    "workers": 1,
                    "timeout": timeout,
                },
            }
        ],
        "tests": [
            {
                "description": f"{c.case_id} / {c.variant} / {c.source_mode} / repeat {c.repeat}",
                "vars": {"attemptId": c.attempt_id},
            }
            for c in plan.cells
        ],
        "defaultTest": {
            "assert": [
                {
                    "type": "python",
                    "value": f"file://{assertions}:{name}",
                    "config": {"pythonExecutable": str(root / ".venv/bin/python")},
                }
                for name in (
                    "execution_complete",
                    "task_contract",
                    "security_boundary",
                    "protocol",
                    "bridge_identity",
                )
            ]
        },
        "evaluateOptions": {"maxConcurrency": 1, "cache": False, "showProgressBar": False},
        "sharing": False,
    }


def gate_result(
    root: Path,
    plan: Plan,
    envelopes: list[LabAttemptEnvelope],
    *,
    interrupted: bool = False,
    promptfoo_exit: int = 0,
    integrity_errors: list[str] | None = None,
) -> tuple[int, list[str]]:
    if interrupted:
        return 130, ["user cancellation; remaining cells are not_run"]
    reasons = list(integrity_errors or [])
    if promptfoo_exit not in {0, 100}:
        reasons.append(f"Promptfoo exited unexpectedly: {promptfoo_exit}")
    if len(envelopes) != len(plan.cells):
        reasons.append(f"missing envelopes: {len(plan.cells) - len(envelopes)}")
    expected_data = (
        load_json(root / "fixtures/controls/expected-results.json")
        if plan.gate == "fixture-contract"
        else {}
    )
    if not isinstance(expected_data, dict):
        raise ValueError("fixture expected-results must be an object")
    gate_failures: list[str] = []
    for env in envelopes:
        actual = {
            "terminalState": env.terminal_state,
            "task": env.checks_summary.task,
            "security": env.checks_summary.security,
            "modelProtocol": env.checks_summary.model_protocol,
        }
        expected: object = None
        if plan.gate == "fixture-contract":
            case_results = expected_data.get(env.case_id)
            expected = case_results.get(env.variant) if isinstance(case_results, dict) else None
            if actual != expected:
                gate_failures.append(
                    f"{env.case_id}/{env.variant}: expected {expected}, observed {actual}"
                )
        expected_technical = (
            isinstance(expected, dict) and expected.get("terminalState") == env.terminal_state
        )
        if env.terminal_state not in {"completed", "model_error"} and not expected_technical:
            reasons.append(f"{env.attempt_id}: technical incompleteness ({env.terminal_state})")
        if env.evidence_integrity == "invalid" or "oracle_error" in actual.values():
            reasons.append(f"{env.attempt_id}: invalid evidence or failed oracle")
    if reasons:
        return 3, reasons + gate_failures
    if plan.gate == "conformance":
        # A bounded C01 protocol smoke is useful evidence, but cannot certify
        # timeout/context/cap behavior of an arbitrary external implementation.
        return 1, [
            "Compatibility smoke recorded. Full model conformance (caps/context/abort) remains unverified; profile stays experimental."
        ]
    return (1, gate_failures) if gate_failures else (0, [])


def execute_plan(
    root: Path, plan: Plan, *, json_output: bool = False, emit_output: bool = True
) -> tuple[int, Path]:
    from llm_test_lab.cli import installation_fingerprint
    from llm_test_lab.evidence import read_envelope, recover_interrupted_bundle, verify_bundle
    from llm_test_lab.reporting import load_report_context, render_run_report, summarize

    root = root.resolve()
    validate_plan(root, plan)
    installation_path = root / ".lab-state/installation.json"
    if not installation_path.is_file() or load_json(installation_path) != installation_fingerprint(
        root
    ):
        raise ValueError(
            "ENVIRONMENT_MISMATCH: run setup and lab doctor --record-installation before executing"
        )
    node, promptfoo = node_path(root), promptfoo_path(root)
    run_dir = root / "runs" / plan.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    env = managed_env(root, run_dir, plan)
    (run_dir / "plan.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n"
    )
    config_path = run_dir / "promptfoo.json"
    config_path.write_text(
        json.dumps(promptfoo_config(root, plan), ensure_ascii=False, indent=2) + "\n"
    )
    control = RunControl.create(run_dir / "control.sqlite", plan.to_dict(), os.getpid())
    interrupted = False
    interrupt_count = 0

    def handle_interrupt(signum: int, frame: object) -> None:
        nonlocal interrupted, interrupt_count
        interrupted = True
        interrupt_count += 1

    old_int, old_term = (
        signal.signal(signal.SIGINT, handle_interrupt),
        signal.signal(signal.SIGTERM, handle_interrupt),
    )
    command = [
        str(node),
        str(promptfoo),
        "eval",
        "--config",
        str(config_path),
        "--output",
        str(run_dir / "promptfoo-results.json"),
        "--max-concurrency",
        "1",
        "--no-cache",
        "--no-progress-bar",
        "--no-share",
    ]
    process: subprocess.Popen[bytes] | None = None
    watchdog: subprocess.Popen[bytes] | None = None
    exit_code = 1
    cancel_at: float | None = None
    try:
        with (run_dir / "promptfoo.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=run_dir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            control.set_process_group(process.pid)
            watchdog = subprocess.Popen(
                [
                    str(root / ".venv/bin/python"),
                    "-m",
                    "llm_test_lab.run_control.watchdog",
                    str(control.path),
                ],
                cwd=run_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            while process.poll() is None:
                if interrupted and cancel_at is None:
                    control.cancel()
                    cancel_at = time.monotonic()
                if cancel_at is not None and (
                    interrupt_count > 1 or time.monotonic() - cancel_at > CLEANUP_GRACE_SECONDS
                ):
                    kill_group(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        kill_group(process.pid, signal.SIGKILL)
                    break
                control.heartbeat()
                time.sleep(0.1)
            exit_code = process.wait(timeout=5)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        # Promptfoo exiting does not prove its worker tree exited.
        if process is not None:
            kill_group(process.pid, signal.SIGTERM)
            kill_group(process.pid, signal.SIGKILL)
        control.finalize_run("cancelled" if interrupted else "partial")
        if watchdog is not None:
            try:
                watchdog.wait(timeout=2)
            except subprocess.TimeoutExpired:
                watchdog.terminate()
                watchdog.wait(timeout=2)

    envelopes: list[LabAttemptEnvelope] = []
    integrity_errors: list[str] = []
    for cell in plan.cells:
        attempt_dir = run_dir / "attempts" / cell.attempt_id
        if not (attempt_dir / "envelope.json").exists():
            if not (attempt_dir / "state.sqlite").exists():
                continue
            try:
                recovered = recover_interrupted_bundle(attempt_dir, persist=True)
                control.record_recovery(cell.attempt_id, recovered.to_dict())
            except (ValueError, OSError):
                integrity_errors.append(
                    f"{cell.attempt_id}: incomplete audit could not be recovered"
                )
                continue
        issues = verify_bundle(attempt_dir)
        integrity_errors.extend(f"{cell.attempt_id}: {issue}" for issue in issues)
        try:
            envelope = read_envelope(attempt_dir, verify=not issues)
            if issues:
                envelope = envelope.model_copy(update={"evidence_integrity": "invalid"})
            envelopes.append(envelope)
        except (ValueError, OSError):
            integrity_errors.append(f"{cell.attempt_id}: envelope could not be read")
    code, reasons = gate_result(
        root,
        plan,
        envelopes,
        interrupted=interrupted,
        promptfoo_exit=exit_code,
        integrity_errors=integrity_errors,
    )
    status = (
        "cancelled"
        if interrupted
        else "failed"
        if code == 3
        else "finished"
        if len(envelopes) == len(plan.cells)
        else "partial"
    )
    control.finalize_run(status)
    cases, expectations = load_report_context(run_dir, envelopes)
    summary = summarize(envelopes, planned=len(plan.cells), cases=cases, expectations=expectations)
    snapshot = control.snapshot()
    observed_ids = {envelope.attempt_id for envelope in envelopes}
    started = sum(cell["state"] != "not_run" for cell in snapshot["cells"])
    summary["started"] = started
    summary["notRun"] = len(plan.cells) - started
    missing_interrupted = sum(
        cell["state"] == "interrupted" and cell["id"] not in observed_ids
        for cell in snapshot["cells"]
    )
    summary["interruptedWithoutEnvelope"] = missing_interrupted
    technical_count = summary["technicallyIncomplete"]
    assert isinstance(technical_count, int)
    summary["technicallyIncomplete"] = technical_count + missing_interrupted
    terminal_counts = summary["terminalCounts"]
    if missing_interrupted and isinstance(terminal_counts, dict):
        known_interrupted = terminal_counts.get("interrupted", 0)
        assert isinstance(known_interrupted, int)
        terminal_counts["interrupted"] = known_interrupted + missing_interrupted
    summary["gateResult"] = {
        "gate": plan.gate,
        "exitCode": code,
        "promptfooExitCode": exit_code,
        "reasons": cast(list[JsonValue], reasons),
    }
    render_run_report(run_dir, envelopes, planned=len(plan.cells), summary_override=summary)
    manifest = {
        "schemaVersion": 1,
        "runId": plan.run_id,
        "planHash": plan.plan_hash,
        "status": status,
        "gate": plan.gate,
        "exitCode": code,
        "promptfooExitCode": exit_code,
        "reasons": reasons,
        "summary": summary,
        "control": snapshot,
        "profiles": sorted({c.profile_id for c in plan.cells if c.profile_id}),
        "cost": "unknown",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    payload = {
        "runId": plan.run_id,
        "status": status,
        "exitCode": code,
        "gate": plan.gate,
        "summary": summary,
        "report": str(run_dir / "report.html"),
        "reasons": reasons,
        "attempts": [
            {
                "caseId": env.case_id,
                "variant": env.variant,
                "task": env.checks_summary.task,
                "security": env.checks_summary.security,
                "terminalState": env.terminal_state,
            }
            for env in envelopes
        ],
    }
    if emit_output:
        emit(payload, result_lines(payload), json_output=json_output)
    return code, run_dir

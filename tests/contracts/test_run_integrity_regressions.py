"""Missing results and damaged historical runs must not become success or break repair."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from test_bridge_process import _command, _project

from llm_test_lab.cli import main
from llm_test_lab.reporting import summarize
from llm_test_lab.run_control import RunControl
from llm_test_lab.run_control.planning import make_plan, plan_digest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = _project(tmp_path_factory.mktemp("integrity-real-worker"))
    run = subprocess.run(
        _command(
            root,
            "run",
            "--source",
            "fixture",
            "--case",
            "C01",
            "--variant",
            "enforced",
            "--gate",
            "fixture-contract",
            "--json",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return root / "runs" / json.loads(run.stdout)["runId"]


def copy_run(tmp_path: Path, completed_run: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='isolated-inspection'\n")
    run = tmp_path / "runs" / completed_run.name
    shutil.copytree(completed_run, run)
    return run


def inspect(root: Path, run: Path, capsys):
    code = main(["--root", str(root), "inspect", "--run", run.name, "--json"])
    output = capsys.readouterr()
    assert not output.err
    return code, json.loads(output.out)


def digests(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file()
    }


@pytest.mark.integration
def test_inspect_detects_deleted_whole_completed_attempt(
    tmp_path: Path, completed_run: Path, capsys
) -> None:
    run = copy_run(tmp_path, completed_run)
    assert inspect(tmp_path, run, capsys)[0] == 0
    attempt = next((run / "attempts").iterdir())
    shutil.rmtree(attempt)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3
    assert payload["integrityErrors"][attempt.name] == ["required attempt directory is missing"]
    assert "summary" not in payload


@pytest.mark.integration
def test_inspect_reads_historical_inputs_without_current_source_and_never_rewrites(
    tmp_path: Path, completed_run: Path, capsys
) -> None:
    run = copy_run(tmp_path, completed_run)
    # No live registry, runtimes or current input hashes are needed to inspect saved facts.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='changed-source-snapshot'\n")
    before = digests(run)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 0 and payload["inspectionExitCode"] == 0
    assert payload["summary"]["completed"] == 1
    assert digests(run) == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "counts",
        "plan_identity",
        "extra_attempt",
        "missing_manifest",
        "invalid_exit",
        "gate_exit",
        "gate_name",
        "gate_reasons",
        "gate_promptfoo",
    ],
)
def test_inspect_rejects_manifest_and_directory_drift(
    tmp_path: Path, completed_run: Path, capsys, change: str
) -> None:
    run = copy_run(tmp_path, completed_run)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    if change == "counts":
        manifest["summary"]["completed"] = 999
    elif change == "plan_identity":
        manifest["planHash"] = "wrong"
    elif change == "extra_attempt":
        (run / "attempts/unapproved").mkdir()
    elif change == "invalid_exit":
        manifest["exitCode"] = 99
    elif change == "gate_exit":
        manifest["exitCode"] = 1
    elif change == "gate_name":
        manifest["summary"]["gateResult"]["gate"] = "none"
    elif change == "gate_reasons":
        manifest["reasons"] = ["synthetic verdict drift"]
    elif change == "gate_promptfoo":
        manifest["promptfooExitCode"] = 17
    if change == "missing_manifest":
        path.unlink()
    else:
        path.write_text(json.dumps(manifest))
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and payload["integrityErrors"]


@pytest.mark.integration
@pytest.mark.parametrize("container", ["symlink", "file"])
def test_inspect_rejects_replaced_attempts_container(
    tmp_path: Path, completed_run: Path, capsys, container: str
) -> None:
    run = copy_run(tmp_path, completed_run)
    attempts = run / "attempts"
    moved = tmp_path / "external-evidence"
    attempts.rename(moved)
    if container == "symlink":
        attempts.symlink_to(moved, target_is_directory=True)
    else:
        attempts.write_text("not a directory")
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and "attempts" in payload["integrityErrors"]


@pytest.mark.integration
@pytest.mark.parametrize("has_assessment_error", [False, True])
def test_inspect_requires_evidence_for_post_execution_probe_failure(
    tmp_path: Path, completed_run: Path, capsys, has_assessment_error: bool
) -> None:
    run = copy_run(tmp_path, completed_run)
    # Preserve real completed evidence, but model probe's historical post-run
    # assessment failure: its top-level verdict changes, its original gate does not.
    path = run / "plan.json"
    plan = json.loads(path.read_text())
    plan.update(gate="conformance", purpose="compatibility")
    plan["planHash"] = plan_digest(plan)
    path.write_text(json.dumps(plan))
    with sqlite3.connect(run / "control.sqlite") as db:
        db.execute("UPDATE run SET plan_hash=?", (plan["planHash"],))
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        planHash=plan["planHash"],
        gate="conformance",
        exitCode=3,
        status="failed",
        conformanceRef="conformance.json",
        limitedProbePassed=False,
    )
    manifest["summary"]["gateResult"].update(gate="conformance", exitCode=1)
    path.write_text(json.dumps(manifest))
    if has_assessment_error:
        (run / "conformance.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "status": "inconclusive",
                    "limitedProbePassed": False,
                    "error": "synthetic assessment failure",
                }
            )
        )
    before = digests(run)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and digests(run) == before
    if has_assessment_error:
        assert "integrityErrors" not in payload
        assert payload["status"] == "failed" and payload["exitCode"] == 3
        assert payload["summary"]["gateResult"]["exitCode"] == 1
    else:
        assert payload["integrityErrors"]


@pytest.mark.parametrize("state", ["cancelled", "partial", "running"])
def test_inspect_keeps_unstarted_attempts_incomplete_not_missing_completed(
    tmp_path: Path, capsys, state: str
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    plan = make_plan(ROOT, source="fixture", cases=["C01"])
    run = tmp_path / "runs" / plan.run_id
    run.mkdir(parents=True)
    (run / "plan.json").write_text(json.dumps(plan.to_dict()))
    control = RunControl.create(run / "control.sqlite", plan.to_dict(), os.getpid())
    control.finalize_run(state)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and "integrityErrors" not in payload
    assert payload["summary"]["notRun"] == 1
    assert payload["summary"]["completed"] == 0 and payload["availableEvidence"] == []


@pytest.mark.parametrize("has_directory", [False, True])
def test_inspect_does_not_recover_or_mislabel_active_attempt(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, has_directory: bool
) -> None:
    import llm_test_lab.evidence as evidence

    monkeypatch.setattr(
        evidence,
        "recover_interrupted_bundle",
        lambda *_a, **_k: pytest.fail("active attempt must not be recovered"),
    )
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    plan = make_plan(ROOT, source="fixture", cases=["C01"])
    run = tmp_path / "runs" / plan.run_id
    run.mkdir(parents=True)
    (run / "plan.json").write_text(json.dumps(plan.to_dict()))
    control = RunControl.create(run / "control.sqlite", plan.to_dict(), os.getpid())
    assert control.claim(plan.cells[0].attempt_id)
    if has_directory:
        attempt = run / "attempts" / plan.cells[0].attempt_id
        attempt.mkdir(parents=True)
        (attempt / "state.sqlite").write_bytes(b"synthetic writer is not finished")
    before = digests(run)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and "integrityErrors" not in payload
    assert payload["status"] == "running"
    assert payload["summary"]["inProgressWithoutEnvelope"] == 1
    assert payload["summary"]["interruptedWithoutEnvelope"] == 0
    assert payload["summary"]["terminalCounts"] == {"running": 1}
    assert payload["availableEvidence"] == [] and digests(run) == before


def test_inspect_completed_manifest_with_cancelled_unstarted_plan_stays_readable(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    plan = make_plan(ROOT, source="fixture", cases=["C01"])
    run = tmp_path / "runs" / plan.run_id
    run.mkdir(parents=True)
    (run / "plan.json").write_text(json.dumps(plan.to_dict()))
    control = RunControl.create(run / "control.sqlite", plan.to_dict(), os.getpid())
    control.finalize_run("cancelled")
    summary = summarize([], planned=1)
    summary["interruptedWithoutEnvelope"] = 0
    summary["gateResult"] = {
        "gate": plan.gate,
        "exitCode": 130,
        "promptfooExitCode": -2,
        "reasons": ["user cancellation; remaining cells are not_run"],
    }
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "runId": plan.run_id,
                "planHash": plan.plan_hash,
                "status": "cancelled",
                "gate": plan.gate,
                "exitCode": 130,
                "promptfooExitCode": -2,
                "reasons": ["user cancellation; remaining cells are not_run"],
                "summary": summary,
            }
        )
    )
    (run / "report.md").write_text("# Cancelled\n")
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and "integrityErrors" not in payload
    assert payload["exitCode"] == 130 and payload["summary"]["notRun"] == 1


@pytest.mark.integration
def test_inspect_keeps_real_completed_and_unstarted_cells_separate(
    tmp_path: Path, completed_run: Path, capsys
) -> None:
    run = copy_run(tmp_path, completed_run)
    path = run / "plan.json"
    plan = json.loads(path.read_text())
    unstarted = {**plan["cells"][0], "attemptId": run.name + "-a0002", "caseId": "C08"}
    plan["cells"].append(unstarted)
    plan["planHash"] = plan_digest(plan)
    path.write_text(json.dumps(plan))
    with sqlite3.connect(run / "control.sqlite") as db:
        db.execute("UPDATE run SET plan_hash=?, status='cancelled'", (plan["planHash"],))
        db.execute(
            "INSERT INTO cells(id,cell,state) VALUES (?,?,'not_run')",
            (unstarted["attemptId"], json.dumps(unstarted)),
        )
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(planHash=plan["planHash"], status="cancelled", exitCode=130)
    manifest["summary"].update(planned=2, notRun=1)
    manifest["reasons"] = ["user cancellation; remaining cells are not_run"]
    manifest["summary"]["gateResult"].update(exitCode=130, reasons=manifest["reasons"])
    manifest_path.write_text(json.dumps(manifest))
    before = digests(run)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 3 and "integrityErrors" not in payload
    assert payload["summary"]["completed"] == 1 and payload["summary"]["notRun"] == 1
    assert len(payload["availableEvidence"]) == 1
    assert digests(run) == before


@pytest.mark.integration
def test_inspect_validates_original_plan_encoding_not_new_default_fields(
    tmp_path: Path, completed_run: Path, capsys
) -> None:
    run = copy_run(tmp_path, completed_run)
    path = run / "plan.json"
    plan = json.loads(path.read_text())
    for optional in ("purpose", "cost", "cache", "retries", "concurrency"):
        plan.pop(optional, None)
    plan["planHash"] = plan_digest(plan)
    path.write_text(json.dumps(plan))
    with sqlite3.connect(run / "control.sqlite") as db:
        db.execute("UPDATE run SET plan_hash=?", (plan["planHash"],))
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["planHash"] = plan["planHash"]
    manifest_path.write_text(json.dumps(manifest))
    before = digests(run)
    assert inspect(tmp_path, run, capsys)[0] == 0
    assert digests(run) == before


@pytest.mark.integration
def test_inspect_reads_early_sealed_bundle_with_manifest_cell(
    tmp_path: Path, completed_run: Path, capsys
) -> None:
    run = copy_run(tmp_path, completed_run)
    attempt = next((run / "attempts").iterdir())
    manifest_path = attempt / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Model the older sealed format, which predated crash-recovery snapshots.
    for name in ("cell.json", "initial-inputs.json"):
        (attempt / name).unlink()
        manifest["files"].pop(name)
    manifest_path.write_text(json.dumps(manifest))
    before = digests(run)
    code, payload = inspect(tmp_path, run, capsys)
    assert code == 0 and payload["summary"]["completed"] == 1
    assert digests(run) == before


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["empty", "garbage", "no_row", "no_cells"])
def test_doctor_reports_unreadable_history_without_breaking_installation_or_overwriting(
    tmp_path: Path, damage: str
) -> None:
    root = _project(tmp_path)
    initial = subprocess.run(
        _command(root, "doctor", "--json"), capture_output=True, text=True, timeout=15
    )
    assert initial.returncode == 0
    path = root / "runs" / f"damaged-{damage}" / "control.sqlite"
    path.parent.mkdir(parents=True)
    if damage == "empty":
        path.touch()
    elif damage == "garbage":
        path.write_bytes(b"synthetic damaged SQLite")
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE run(id TEXT, status TEXT)")
            if damage == "no_cells":
                db.execute("INSERT INTO run VALUES ('incomplete','running')")
    before = path.read_bytes()
    for record in (False, True):
        command = _command(root, "doctor", "--json", *(["--record-installation"] if record else []))
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok" and payload["installation"]
        assert payload["unreadableRuns"][0]["runId"] == path.parent.name
        assert payload["unreadableRuns"][0]["errorCode"] == "RUN_CONTROL_UNREADABLE"
        assert path.read_bytes() == before and "Traceback" not in result.stderr
    human = subprocess.run(_command(root, "doctor"), capture_output=True, text=True, timeout=15)
    assert human.returncode == 0 and "RUN_CONTROL_UNREADABLE" in human.stdout


def test_control_initialization_failure_is_never_published(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite"
    invalid = {"runId": "r", "planHash": "hash", "maxInferenceCalls": 0}
    with pytest.raises(KeyError):
        RunControl.create(path, invalid, os.getpid())
    assert not path.exists() and not list(tmp_path.glob(".control-*"))


def test_concurrent_control_creation_publishes_once_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite"
    barrier = threading.Barrier(2)
    original_link = os.link

    def publish(source, target):
        with sqlite3.connect(source) as db:
            assert db.execute("SELECT count(*) FROM cells").fetchone()[0] == 1
        assert not Path(target).exists()
        barrier.wait(timeout=5)
        original_link(source, target)

    monkeypatch.setattr(os, "link", publish)
    results = []

    def create(identity):
        try:
            RunControl.create(
                path,
                {
                    "runId": identity,
                    "planHash": "hash",
                    "maxInferenceCalls": 0,
                    "cells": [{"attemptId": "a1"}],
                },
                os.getpid(),
            )
            results.append((identity, "created"))
        except FileExistsError:
            results.append((identity, "exists"))

    threads = [threading.Thread(target=create, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(result for _, result in results) == ["created", "exists"]
    winner = next(name for name, result in results if result == "created")
    assert RunControl(path).snapshot()["id"] == winner
    assert not list(tmp_path.glob(".control-*"))

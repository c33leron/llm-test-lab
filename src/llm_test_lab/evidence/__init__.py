"""Versioned synthetic evidence bundles and read-only, inference-free rescoring."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from llm_test_lab.contracts import (
    Case,
    ExpectationSet,
    FinalAnswer,
    Fixture,
    LabAttemptEnvelope,
    TurnResult,
    load_json,
)
from llm_test_lab.oracles import ORACLE_VERSION, evaluate
from llm_test_lab.tickets import read_events, read_snapshot


def dump_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _usage(
    values: list[dict[str, JsonValue]], dispatches: int, fixture: bool
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"numRequests": dispatches}
    for field in ("inputTokens", "outputTokens"):
        known = [value[field] for value in values if isinstance(value.get(field), int)]
        if fixture:
            result[field], result[field + "Status"] = 0, "not_applicable_fixture"
        elif len(known) == dispatches and dispatches:
            result[field] = sum(cast(list[int], known))
            result[field + "Status"] = "complete"
        elif known:
            result[field] = sum(cast(list[int], known))
            result[field + "Status"] = "partial"
        else:
            result[field], result[field + "Status"] = None, "unknown"
    return result


def save_bundle(
    *,
    root: Path,
    attempt_dir: Path,
    envelope: LabAttemptEnvelope,
    case: Case,
    expectation: ExpectationSet,
    fixture: Fixture,
    before: list[dict[str, JsonValue]],
    conversation: list[dict[str, JsonValue]],
    cell: dict[str, JsonValue],
    error: dict[str, JsonValue] | None,
    duration_ms: int,
    usage_values: list[dict[str, JsonValue]],
) -> None:
    from llm_test_lab.reporting import render_attempt_report

    database = attempt_dir / "state.sqlite"
    events = read_events(database)
    after = read_snapshot(database)
    data = {
        "envelope.json": envelope.to_dict(),
        "case.json": case.to_dict(),
        "expectation.json": expectation.to_dict(),
        "fixture.json": fixture.model_dump(mode="json", by_alias=True, exclude_unset=True),
        "state-before.json": before,
        "state-after.json": after,
        "conversation.json": conversation,
        "checks.json": envelope.checks_summary.to_dict(),
    }
    for name, value in data.items():
        dump_json(attempt_dir / name, value)
    (attempt_dir / "events.jsonl").write_text(
        "".join(
            json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    render_attempt_report(attempt_dir, envelope, case, before, after, events)
    versions: dict[str, JsonValue] = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "oracle": ORACLE_VERSION,
    }
    for name in ("pydantic", "httpx", "llm-test-lab"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "uninstalled-source"
    installation = root / ".lab-state" / "installation.json"
    if installation.is_file():
        installed = load_json(installation)
        if isinstance(installed, dict):
            for name in ("node", "promptfoo", "uv", "platform"):
                versions[name] = installed.get(name)
    versions["transport"] = "1.0.0"
    try:
        git = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        git_sha = git.stdout.strip() if git.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        git_sha = None
    locks = {
        name: _hash(root / name)
        for name in ("uv.lock", "tooling/promptfoo/package-lock.json")
        if (root / name).is_file()
    }
    script = root / "fixtures" / "responses" / f"{case.scripted_response_set}.json"
    prompt_paths = [root / "prompts" / "base.txt"]
    if envelope.variant != "vulnerable":
        prompt_paths.append(root / "prompts" / "guarded.txt")
    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "attemptId": envelope.attempt_id,
        "runId": attempt_dir.parent.parent.name,
        "sourceMode": envelope.source_mode,
        "caseId": case.id,
        "caseVersion": case.version,
        "variant": envelope.variant,
        "profileId": envelope.profile_id,
        "cell": cell,
        "usedForTuning": case.used_for_tuning,
        "versions": versions,
        "gitSHA": git_sha,
        "lockHashes": locks,
        "scriptHash": _hash(script),
        "promptHashes": {p.name: _hash(p) for p in prompt_paths},
        "error": error,
        "durationMs": duration_ms,
        "evidenceIntegrity": envelope.evidence_integrity,
        "usage": _usage(
            usage_values,
            sum(event.type == "model_dispatch" for event in events),
            envelope.source_mode == "fixture",
        ),
        "cost": "unknown",
        "missingArtifacts": [],
        "files": {
            p.name: _hash(p)
            for p in sorted(attempt_dir.iterdir())
            if p.is_file() and p.name != "manifest.json"
        },
        "integrityScope": "Hashes detect changed artifacts; not a signature against the computer owner.",
    }
    dump_json(attempt_dir / "manifest.json", manifest)


def _bundle_path(attempt_dir: Path) -> Path:
    recovered = attempt_dir / "recovered"
    if not (attempt_dir / "envelope.json").exists() and (recovered / "envelope.json").is_file():
        return recovered
    return attempt_dir


def read_envelope(attempt_dir: Path, verify: bool = False) -> LabAttemptEnvelope:
    attempt_dir = _bundle_path(attempt_dir)
    if verify:
        errors = verify_bundle(attempt_dir)
        if errors:
            raise ValueError("EVIDENCE_INVALID: " + "; ".join(errors))
    return LabAttemptEnvelope.model_validate(load_json(attempt_dir / "envelope.json"))


def verify_bundle(attempt_dir: Path) -> list[str]:
    attempt_dir = _bundle_path(attempt_dir)
    errors: list[str] = []
    required = {
        "state.sqlite",
        "envelope.json",
        "case.json",
        "expectation.json",
        "fixture.json",
        "state-before.json",
        "state-after.json",
        "conversation.json",
        "checks.json",
        "events.jsonl",
        "report.md",
    }
    try:
        manifest = load_json(attempt_dir / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
            return ["manifest schema is invalid"]
        files = manifest.get("files")
        if not isinstance(files, dict) or not required.issubset(files):
            return ["manifest is missing required artifact hashes"]
        for name, digest in files.items():
            path = attempt_dir / name
            if Path(name).name != name or path.is_symlink():
                errors.append("artifact path is invalid: " + name)
            elif not path.is_file() or _hash(path) != digest:
                errors.append("missing or changed artifact: " + name)
        if manifest.get("recoveredFromAudit") is True:
            source = attempt_dir.parent / "state.sqlite"
            if not source.is_file() or _hash(source) != manifest.get("originalDatabaseHash"):
                errors.append("original audit database changed after recovery")
        envelope = read_envelope(attempt_dir)
        if manifest.get("attemptId") != envelope.attempt_id:
            errors.append("attempt identity mismatch")
        Case.model_validate(load_json(attempt_dir / "case.json"))
        ExpectationSet.model_validate(load_json(attempt_dir / "expectation.json"))
        Fixture.model_validate(load_json(attempt_dir / "fixture.json"))
    except (ValueError, OSError) as exc:
        errors.append("invalid evidence schema or file: " + type(exc).__name__)
    return errors


def rescore_bundle(attempt_dir: Path) -> Path:
    attempt_dir = _bundle_path(attempt_dir)
    errors = verify_bundle(attempt_dir)
    if errors:
        raise ValueError("EVIDENCE_INVALID: " + "; ".join(errors))
    envelope = read_envelope(attempt_dir)
    case = Case.model_validate(load_json(attempt_dir / "case.json"))
    expectation = ExpectationSet.model_validate(load_json(attempt_dir / "expectation.json"))
    before = load_json(attempt_dir / "state-before.json")
    if not isinstance(before, list) or any(not isinstance(ticket, dict) for ticket in before):
        raise ValueError("EVIDENCE_INVALID: state-before schema")
    checks = evaluate(
        case=case,
        expectation=expectation,
        before=cast(list[dict[str, JsonValue]], before),
        database=attempt_dir / "state.sqlite",
        turns=envelope.turn_results,
        terminal_state=envelope.terminal_state,
        evidence_complete=envelope.evidence_integrity == "complete",
    )
    directory = attempt_dir / "rescored"
    directory.mkdir(exist_ok=True)
    path = directory / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json"
    )
    dump_json(
        path,
        {
            "schemaVersion": 1,
            "sourceMode": "recorded",
            "originalSourceMode": envelope.source_mode,
            "originalAttemptId": envelope.attempt_id,
            "originalManifestHash": _hash(attempt_dir / "manifest.json"),
            "oracleVersion": ORACLE_VERSION,
            "inferenceCalls": 0,
            "checks": checks.to_dict(),
        },
    )
    return path


def initialize_bundle(
    attempt_dir: Path,
    *,
    case: Case,
    expectation: ExpectationSet,
    fixture: Fixture,
    before: list[dict[str, JsonValue]],
    cell: dict[str, JsonValue],
) -> None:
    """Freeze scoring inputs BEFORE any model/tool action, so a killed worker is recoverable."""
    documents = {
        "case.json": case.to_dict(),
        "expectation.json": expectation.to_dict(),
        "fixture.json": fixture.model_dump(mode="json", by_alias=True, exclude_unset=True),
        "state-before.json": before,
        "cell.json": cell,
    }
    for name, data in documents.items():
        path = attempt_dir / name
        if path.exists():
            raise FileExistsError("initial input snapshot already exists")
        dump_json(path, data)
    dump_json(
        attempt_dir / "initial-inputs.json",
        {"schemaVersion": 1, "files": {name: _hash(attempt_dir / name) for name in documents}},
    )


def recover_interrupted_bundle(attempt_dir: Path, *, persist: bool = False) -> LabAttemptEnvelope:
    """Read only the stopped worker's audit. Never resume execution or invent a full trace.

    persist=True writes a separate derived bundle; original files and SQLite are untouched.
    The caller must stop the owned process group before requesting persisted recovery.
    """
    from llm_test_lab.reporting import render_attempt_report
    from llm_test_lab.run_control.planning import PlanCell

    if (attempt_dir / "envelope.json").exists():
        raise ValueError("recovery is only for interrupted attempts without an envelope")
    if (attempt_dir / "recovered" / "envelope.json").exists():
        return read_envelope(attempt_dir, verify=True)
    initial = load_json(attempt_dir / "initial-inputs.json")
    if (
        not isinstance(initial, dict)
        or initial.get("schemaVersion") != 1
        or not isinstance(initial.get("files"), dict)
    ):
        raise ValueError("EVIDENCE_INVALID: missing immutable input index")
    initial_files = initial["files"]
    assert isinstance(initial_files, dict)
    if not {
        "case.json",
        "expectation.json",
        "fixture.json",
        "state-before.json",
        "cell.json",
    }.issubset(initial_files):
        raise ValueError("EVIDENCE_INVALID: recovery index is incomplete")
    for name, expected_hash in initial_files.items():
        if (
            Path(name).name != name
            or not (attempt_dir / name).is_file()
            or _hash(attempt_dir / name) != expected_hash
        ):
            raise ValueError("EVIDENCE_INVALID: initial recovery input changed")
    case = Case.model_validate(load_json(attempt_dir / "case.json"))
    expectation = ExpectationSet.model_validate(load_json(attempt_dir / "expectation.json"))
    Fixture.model_validate(load_json(attempt_dir / "fixture.json"))
    cell = PlanCell.model_validate(load_json(attempt_dir / "cell.json"))
    raw_before = load_json(attempt_dir / "state-before.json")
    if not isinstance(raw_before, list) or any(
        not isinstance(ticket, dict) for ticket in raw_before
    ):
        raise ValueError("EVIDENCE_INVALID: recovery state-before is invalid")
    before = cast(list[dict[str, JsonValue]], raw_before)
    database = attempt_dir / "state.sqlite"
    events = read_events(database)
    after = read_snapshot(database)
    turns: list[TurnResult] = []
    for event in events:
        if event.type == "turn_complete" and event.turn_id is not None:
            answer = FinalAnswer.model_validate(event.payload.get("finalAnswer"))
            turns.append(
                TurnResult(
                    turn_id=event.turn_id,
                    outcome=answer.outcome,
                    model_protocol="pass",
                    final_answer=answer,
                )
            )
    checks = evaluate(
        case=case,
        expectation=expectation,
        before=before,
        database=database,
        turns=turns,
        terminal_state="interrupted",
        evidence_complete=False,
    )
    envelope = LabAttemptEnvelope(
        attempt_id=cell.attempt_id,
        case_id=case.id,
        case_version=case.version,
        variant=cell.variant,
        source_mode=cell.source_mode,
        profile_id=cell.profile_id,
        terminal_state="interrupted",
        turn_results=turns,
        final_answer=turns[-1].final_answer if turns else None,
        checks_summary=checks,
        evidence_integrity="partial",
        evidence_ref=f"attempts/{cell.attempt_id}/"
        + ("recovered/report.md" if persist else "state.sqlite"),
    )
    if not persist:
        return envelope
    recovered = attempt_dir / "recovered"
    recovered.mkdir(exist_ok=False)
    original_hash = _hash(database)
    shutil.copyfile(database, recovered / "state.sqlite")
    for name in (
        "case.json",
        "expectation.json",
        "fixture.json",
        "state-before.json",
        "cell.json",
        "initial-inputs.json",
    ):
        shutil.copyfile(attempt_dir / name, recovered / name)
    dump_json(recovered / "envelope.json", envelope.to_dict())
    dump_json(recovered / "checks.json", checks.to_dict())
    dump_json(recovered / "state-after.json", after)
    # No reconstruction of unknown conversation; raw observed messages remain in the audit.
    dump_json(recovered / "conversation.json", [])
    (recovered / "events.jsonl").write_text(
        "".join(json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    render_attempt_report(recovered, envelope, case, before, after, events)
    note = "Recovered read-only from SQLite audit after worker interruption. Original conversation/export missing. Empty conversation is unavailable, NOT an observed empty dialogue."
    report = recovered / "report.md"
    report.write_text("> " + note + "\n\n" + report.read_text(), encoding="utf-8")
    html_report = recovered / "report.html"
    html_report.write_text(
        html_report.read_text().replace("<body>", '<body><p class="notice">' + note + "</p>", 1),
        encoding="utf-8",
    )
    manifest = {
        "schemaVersion": 1,
        "attemptId": cell.attempt_id,
        "sourceMode": cell.source_mode,
        "recoveredFromAudit": True,
        "originalDatabaseHash": original_hash,
        "evidenceIntegrity": "partial",
        "terminalState": "interrupted",
        "oracleVersion": ORACLE_VERSION,
        "missingOriginalArtifacts": [
            name
            for name in (
                "manifest.json",
                "envelope.json",
                "events.jsonl",
                "conversation.json",
                "state-after.json",
                "checks.json",
                "report.md",
            )
            if not (attempt_dir / name).exists()
        ],
        "reconstructedExports": [
            "events.jsonl",
            "state-after.json",
            "checks.json",
            "report.md",
            "report.html",
        ],
        "conversationStatus": "unavailable; raw observed messages may exist in SQLite events",
        "usage": {
            "numRequests": sum(event.type == "model_dispatch" for event in events),
            "inputTokens": None,
            "outputTokens": None,
            "status": "unknown",
        },
        "files": {path.name: _hash(path) for path in sorted(recovered.iterdir()) if path.is_file()},
    }
    dump_json(recovered / "manifest.json", manifest)
    return envelope

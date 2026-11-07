"""Thin command line: immutable planning and managed Promptfoo execution."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from llm_test_lab.contracts import Case, LabAttemptEnvelope, load_json
from llm_test_lab.registry import Registry
from llm_test_lab.run_control import ControlError, RunControl
from llm_test_lab.run_control.planning import (
    Plan,
    PlanCell,
    make_plan,
    plan_digest,
    read_plan,
    sha256,
)
from llm_test_lab.validation import safe_validation_error

from .output import emit, open_report, result_lines
from .runtime import execute_plan, node_path, npm_path, promptfoo_path


def project_root(explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "pyproject.toml").is_file():
            raise ValueError("ENVIRONMENT_MISMATCH: --root is not a lab checkout")
        return root
    for root in (Path.cwd(), *Path.cwd().parents):
        if (root / "pyproject.toml").is_file() and (root / "scenarios").is_dir():
            return root.resolve()
    raise ValueError("ENVIRONMENT_MISMATCH: run lab from the repository root")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def inside(root: Path, value: str, default_dir: str = "") -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / default_dir / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError("RUN_CONTEXT_INVALID: paths must stay inside the project")
    return path


def installation_fingerprint(root: Path) -> dict[str, Any]:
    import llm_test_lab

    actual_module = Path(llm_test_lab.__file__).resolve().parent
    expected_module = (root / "src/llm_test_lab").resolve()
    if actual_module != expected_module or not expected_module.is_relative_to(root.resolve()):
        raise ValueError(
            "ENVIRONMENT_MISMATCH: installed package points to another checkout; run ./lab setup in this checkout"
        )
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        raise ValueError(
            "ENVIRONMENT_MISMATCH: invoke .venv/bin/lab or uv run --no-sync --offline lab"
        )
    pinned_python = (root / ".python-version").read_text().strip()
    if platform.python_version() != pinned_python:
        raise ValueError(
            f"ENVIRONMENT_MISMATCH: Python {pinned_python} required, got {platform.python_version()}"
        )
    node = node_path(root)
    node_version = subprocess.check_output([str(node), "--version"], text=True).strip().lstrip("v")
    pinned_node = (root / ".node-version").read_text().strip().lstrip("v")
    if node_version != pinned_node:
        raise ValueError(f"ENVIRONMENT_MISMATCH: Node {pinned_node} required, got {node_version}")
    uv_path = root / ".lab-state/bootstrap/bin/uv"
    if not uv_path.is_file():
        uv_found = shutil.which("uv")
        uv_path = Path(uv_found) if uv_found else uv_path
    uv_version = (
        subprocess.check_output([str(uv_path), "--version"], text=True).strip()
        if uv_path.is_file()
        else None
    )
    npm_found = npm_path(root)
    npm_version = (
        subprocess.check_output([str(node), str(npm_found), "--version"], text=True).strip()
        if npm_found
        else None
    )
    for filename, observed in (
        (".uv-version", uv_version.split()[1] if uv_version else None),
        (".npm-version", npm_version),
    ):
        pinned_path = root / filename
        if (
            pinned_path.is_file()
            and observed is not None
            and observed != pinned_path.read_text().strip()
        ):
            raise ValueError(
                f"ENVIRONMENT_MISMATCH: {filename} disagrees with the installed runtime; run ./lab setup"
            )
    promptfoo_path(root)
    package = load_json(root / "tooling/promptfoo/node_modules/promptfoo/package.json")
    lock = load_json(root / "tooling/promptfoo/package-lock.json")
    if not isinstance(package, dict) or not isinstance(lock, dict):
        raise ValueError("ENVIRONMENT_MISMATCH: invalid npm metadata")
    lock_packages = lock.get("packages")
    locked_promptfoo = (
        lock_packages.get("node_modules/promptfoo") if isinstance(lock_packages, dict) else None
    )
    if not isinstance(locked_promptfoo, dict) or locked_promptfoo.get("version") != package.get(
        "version"
    ):
        raise ValueError("ENVIRONMENT_MISMATCH: npm installed Promptfoo disagrees with lockfile")
    uv_lock = tomllib.loads((root / "uv.lock").read_text())
    installed: dict[str, str] = {}
    for name in ("pydantic", "httpx", "pytest", "ruff", "mypy"):
        version = importlib.metadata.version(name)
        expected = next((p["version"] for p in uv_lock["package"] if p["name"] == name), None)
        if expected != version:
            raise ValueError(
                f"ENVIRONMENT_MISMATCH: {name} installed {version}, locked {expected}; repeat setup"
            )
        installed[name] = version
    return {
        "schemaVersion": 1,
        "python": platform.python_version(),
        # python, python3 and the console entry point can name the same binary.
        # Identity is the resolved interpreter plus the separately validated venv
        # and source root, not the spelling of its launch-time symlink.
        "pythonExecutable": str(Path(sys.executable).resolve()),
        "pythonPrefix": str(Path(sys.prefix).resolve()),
        "node": node_version,
        "nodeExecutable": str(node),
        "uv": uv_version,
        "npm": npm_version,
        "promptfoo": package["version"],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "packages": installed,
        "lockfiles": {
            "uv.lock": sha256(root / "uv.lock"),
            "tooling/promptfoo/package-lock.json": sha256(
                root / "tooling/promptfoo/package-lock.json"
            ),
        },
    }


def doctor(root: Path, record: bool, *, json_output: bool = False) -> int:
    fingerprint = installation_fingerprint(root)
    path = root / ".lab-state/installation.json"
    if record:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text(fingerprint))
    elif not path.is_file() or load_json(path) != fingerprint:
        raise ValueError("ENVIRONMENT_MISMATCH: run setup, then lab doctor --record-installation")
    unfinished = []
    unreadable = []
    for control_path in (root / "runs").glob("*/control.sqlite"):
        try:
            snapshot = RunControl(control_path).snapshot()
            if snapshot.get("status") not in {
                "running",
                "partial",
                "cancelled",
                "finished",
                "failed",
            }:
                raise ControlError("invalid historical run state")
        except (sqlite3.DatabaseError, ControlError, OSError, ValueError):
            unreadable.append(
                {
                    "runId": control_path.parent.name,
                    "status": "unreadable",
                    "errorCode": "RUN_CONTROL_UNREADABLE",
                    "note": "Historical control data is incomplete or damaged; preserved without resuming actions.",
                }
            )
            continue
        if snapshot["status"] in {"running", "partial", "cancelled"}:
            unfinished.append(
                {
                    "runId": control_path.parent.name,
                    "status": snapshot["status"],
                    "note": "inspect available evidence; doctor never resumes actions",
                }
            )
    payload = {
        "status": "ok",
        "inferenceCalls": 0,
        "installation": fingerprint,
        "unfinishedRuns": unfinished,
        "unreadableRuns": unreadable,
        "locality": "not assessed; no model requests made",
    }
    emit(
        payload,
        [
            "Установка проверена. Модель не вызывалась.",
            f"Python {fingerprint['python']} · Node {fingerprint['node']} · Promptfoo {fingerprint['promptfoo']}",
            "Версии и lockfiles совпадают." + (" Fingerprint сохранён." if record else ""),
            f"Незавершённых исторических запусков: {len(unfinished)}. Они не возобновляются автоматически.",
            f"Нечитаемых исторических запусков: {len(unreadable)}. Установка исправна; материалы этих запусков сохранены без изменений.",
            *[f"RUN_CONTROL_UNREADABLE: {item['runId']}" for item in unreadable],
        ],
        json_output=json_output,
    )
    return 0


def new_case(root: Path, source_id: str, new_id: str, *, json_output: bool = False) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", new_id):
        raise ValueError("CASE_INVALID: --id must be a simple local registry identifier")
    registry = Registry(root)
    source = registry.load_case(source_id)
    fixture, expectation, script = (
        registry.load_fixture(source.fixture),
        registry.load_expectation(source.expectation_set),
        registry.load_script(source.scripted_response_set),
    )
    case_data, fixture_data, expectation_data, script_data = (
        source.to_dict(),
        fixture.to_dict(),
        expectation.to_dict(),
        script.to_dict(),
    )
    fixture_id, expectation_id, script_id = (
        f"{new_id}-fixture",
        f"{new_id}-expectation",
        f"{new_id}-responses",
    )
    case_data.update(
        {
            "id": new_id,
            "fixture": fixture_id,
            "expectationSet": expectation_id,
            "scriptedResponseSet": script_id,
            "pairId": None,
            "condition": "standalone",
            "usedForTuning": False,
        }
    )
    fixture_data["id"], expectation_data["id"], script_data["id"] = (
        fixture_id,
        expectation_id,
        script_id,
    )
    Case.model_validate(case_data)
    outputs = {
        root / f"scenarios/{new_id}.json": case_data,
        root / f"fixtures/{fixture_id}.json": fixture_data,
        root / f"fixtures/expectations/{expectation_id}.json": expectation_data,
        root / f"fixtures/responses/{script_id}.json": script_data,
    }
    if any(p.exists() for p in outputs):
        raise ValueError("CASE_INVALID: destination exists; choose a new ID (nothing overwritten)")
    created = []
    try:
        for path, content in outputs.items():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json_text(content))
            created.append(path)
        Registry(root).validate_all()
    except BaseException:
        for path in created:
            path.unlink()
        raise
    payload = {
        "created": [str(p) for p in outputs],
        "next": f"lab run --source fixture --case {new_id} --variant enforced --gate none",
    }
    emit(
        payload,
        [
            f"Создан самостоятельный сценарий {new_id}; {source_id} не изменён.",
            *(str(path) for path in outputs),
            f"Запуск: ./lab start --case {new_id}",
        ],
        json_output=json_output,
    )
    return 0


def list_envelopes(run_dir: Path) -> list[LabAttemptEnvelope]:
    from llm_test_lab.evidence import read_envelope, recover_interrupted_bundle

    result = []
    for attempt_dir in sorted((run_dir / "attempts").glob("*")):
        if not attempt_dir.is_dir():
            continue
        if (attempt_dir / "envelope.json").exists() or (
            attempt_dir / "recovered/envelope.json"
        ).exists():
            result.append(read_envelope(attempt_dir, verify=True))
        elif (attempt_dir / "state.sqlite").exists():
            result.append(recover_interrupted_bundle(attempt_dir))
    return result


def inspect_run(
    root: Path, run_dir: Path, *, full: bool, open_html: bool, json_output: bool
) -> int:
    """Verify the expected set, not merely the directories that survived."""
    from llm_test_lab.evidence import read_envelope, recover_interrupted_bundle, verify_bundle
    from llm_test_lab.reporting import load_report_context, summarize

    errors: dict[str, list[str]] = {}

    def reject() -> int:
        emit(
            {"status": "error", "exitCode": 3, "integrityErrors": errors},
            [
                "EVIDENCE_INVALID: доказательства изменены или неполны.",
                *[f"{name}: {', '.join(items)}" for name, items in errors.items()],
            ],
            json_output=json_output,
        )
        return 3

    try:
        raw_plan = load_json(run_dir / "plan.json")
        if not isinstance(raw_plan, dict):
            raise ValueError("saved plan must be an object")
        plan = Plan.model_validate(raw_plan)
        # Historical inspection must not compare saved inputs to today's source.
        if plan_digest(raw_plan) != plan.plan_hash or plan.run_id != run_dir.name:
            raise ValueError("saved plan identity is invalid")
        snapshot = RunControl(run_dir / "control.sqlite").snapshot()
        if snapshot["id"] != plan.run_id or snapshot["plan_hash"] != plan.plan_hash:
            raise ValueError("saved control does not match the plan")
        cells = {row["id"]: row for row in snapshot["cells"]}
        if set(cells) != {cell.attempt_id for cell in plan.cells}:
            raise ValueError("saved control cell set does not match the plan")
        for cell in plan.cells:
            if PlanCell.model_validate_json(cells[cell.attempt_id]["cell"]) != cell:
                raise ValueError("saved control cell does not match the plan")
        if snapshot["status"] not in {"running", "partial", "cancelled", "finished", "failed"}:
            raise ValueError("saved control status is invalid")
    except (ValueError, OSError, sqlite3.DatabaseError, ControlError, KeyError, TypeError):
        errors["run"] = ["saved plan/control is missing, damaged or inconsistent"]
        return reject()

    expected = {cell.attempt_id for cell in plan.cells}
    attempts = run_dir / "attempts"
    if attempts.is_symlink() or (
        attempts.exists()
        and (not attempts.is_dir() or attempts.resolve().parent != run_dir.resolve())
    ):
        errors["attempts"] = ["attempts container must be a directory inside the saved run"]
        return reject()
    for path in attempts.glob("*"):
        if path.is_dir() and path.name not in expected:
            errors[path.name] = ["attempt is not part of the saved plan"]
    envelopes: list[LabAttemptEnvelope] = []
    for cell in plan.cells:
        row = cells[cell.attempt_id]
        directory = attempts / cell.attempt_id
        if row["state"] == "not_run":
            if directory.exists():
                errors[cell.attempt_id] = ["evidence exists for an unadmitted attempt"]
            continue
        partial_without_envelope = (
            row["state"] in {"running", "interrupted"} and not row["envelope"]
        )
        if not directory.is_dir():
            if not partial_without_envelope:
                errors[cell.attempt_id] = ["required attempt directory is missing"]
            continue
        if directory.is_symlink() or directory.resolve().parent != attempts.resolve():
            errors[cell.attempt_id] = ["attempt directory is outside the saved run"]
            continue
        if row["state"] == "running" and snapshot["status"] == "running":
            # The writer may still be between SQLite/event/bundle writes. Do not
            # recover an active attempt or label it interrupted from a partial read.
            continue
        try:
            sealed = (directory / "envelope.json").is_file() or (
                directory / "recovered/envelope.json"
            ).is_file()
            if sealed:
                issues = verify_bundle(directory)
                if issues:
                    errors[cell.attempt_id] = issues
                    continue
                envelope = read_envelope(directory)
                bundle = (
                    directory
                    if (directory / "envelope.json").is_file()
                    else directory / "recovered"
                )
                bundle_manifest = load_json(bundle / "manifest.json")
                if not isinstance(bundle_manifest, dict):
                    raise ValueError("invalid attempt manifest")
                # Early sealed bundles recorded the cell in their manifest,
                # before separate initial-input snapshots were introduced.
                frozen_cell = (
                    load_json(directory / "cell.json")
                    if (directory / "cell.json").is_file()
                    else bundle_manifest.get("cell")
                )
            elif partial_without_envelope and (directory / "state.sqlite").is_file():
                envelope = recover_interrupted_bundle(directory)
                frozen_cell = load_json(directory / "cell.json")
            elif partial_without_envelope:
                continue
            else:
                errors[cell.attempt_id] = ["required attempt envelope is missing"]
                continue
            if (
                envelope.attempt_id != cell.attempt_id
                or envelope.case_id != cell.case_id
                or envelope.variant != cell.variant
                or envelope.source_mode != cell.source_mode
                or envelope.profile_id != cell.profile_id
                or PlanCell.model_validate(frozen_cell) != cell
            ):
                raise ValueError("attempt identity differs from saved plan")
            if (
                row["envelope"]
                and LabAttemptEnvelope.model_validate_json(row["envelope"]) != envelope
            ):
                raise ValueError("control and saved envelope disagree")
            if row["state"] not in {"running", "interrupted", envelope.terminal_state}:
                raise ValueError("control and saved terminal state disagree")
            envelopes.append(envelope)
        except (ValueError, OSError, sqlite3.DatabaseError, ControlError, KeyError, TypeError):
            errors[cell.attempt_id] = [
                "attempt evidence is unreadable or differs from saved plan/control"
            ]
    if errors:
        return reject()

    try:
        cases, expectations = load_report_context(run_dir, envelopes)
        summary = summarize(
            envelopes, planned=len(plan.cells), cases=cases, expectations=expectations
        )
        started = sum(row["state"] != "not_run" for row in cells.values())
        missing_started = started - len(envelopes)
        observed_ids = {envelope.attempt_id for envelope in envelopes}
        in_progress = sum(
            snapshot["status"] == "running"
            and row["state"] == "running"
            and row["id"] not in observed_ids
            for row in cells.values()
        )
        stopped_without_envelope = missing_started - in_progress
        summary["started"], summary["notRun"] = started, len(plan.cells) - started
        summary["interruptedWithoutEnvelope"] = stopped_without_envelope
        summary["inProgressWithoutEnvelope"] = in_progress
        technical_count = summary["technicallyIncomplete"]
        assert isinstance(technical_count, int)
        summary["technicallyIncomplete"] = technical_count + missing_started
        terminal_counts = summary["terminalCounts"]
        assert isinstance(terminal_counts, dict)
        if stopped_without_envelope:
            known_interrupted = terminal_counts.get("interrupted", 0)
            assert isinstance(known_interrupted, int)
            terminal_counts["interrupted"] = known_interrupted + stopped_without_envelope
        if in_progress:
            terminal_counts["running"] = in_progress
        manifest: dict[str, Any] = {}
        if (run_dir / "manifest.json").is_file():
            saved = load_json(run_dir / "manifest.json")
            probe_failure = False
            if (
                isinstance(saved, dict)
                and plan.gate == "conformance"
                and snapshot["status"] in {"finished", "failed"}
                and saved.get("conformanceRef") == "conformance.json"
                and saved.get("exitCode") == 3
                and saved.get("status") == "failed"
            ):
                assessment = load_json(run_dir / "conformance.json")
                probe_failure = (
                    isinstance(assessment, dict)
                    and assessment.get("schemaVersion") == 1
                    and assessment.get("status") == "inconclusive"
                    and assessment.get("limitedProbePassed") is False
                    and isinstance(assessment.get("error"), str)
                )
            if (
                not isinstance(saved, dict)
                or saved.get("runId") != plan.run_id
                or saved.get("planHash") != plan.plan_hash
                or (saved.get("status") != snapshot["status"] and not probe_failure)
                or saved.get("gate") != plan.gate
                or type(saved.get("exitCode")) is not int
                or saved.get("exitCode") not in {0, 1, 3, 130}
            ):
                raise ValueError("run manifest identity differs")
            manifest = dict(saved)
            cached = saved.get("summary")
            if not isinstance(cached, dict):
                raise ValueError("run summary missing")
            for key in (
                "planned",
                "started",
                "notRun",
                "completed",
                "modelError",
                "technicallyIncomplete",
                "terminalCounts",
            ):
                if key in cached and cached[key] != summary[key]:
                    raise ValueError("cached summary differs from saved evidence")
            # This gate describes the original execution, not a rescore with today's rules.
            if "gateResult" in cached:
                gate = cached["gateResult"]
                if not isinstance(gate, dict) or gate.get("gate") != plan.gate:
                    raise ValueError("cached gate differs from saved plan")
                for key in ("exitCode", "promptfooExitCode", "reasons"):
                    if key not in gate or key not in saved:
                        raise ValueError("cached gate verdict is incomplete")
                    if key.endswith("Code") and (
                        type(gate[key]) is not int or type(saved[key]) is not int
                    ):
                        raise ValueError("cached exit code type is invalid")
                    if gate[key] != saved[key] and not (
                        key == "exitCode" and probe_failure and gate[key] in {0, 1, 3}
                    ):
                        raise ValueError("cached gate and run verdict disagree")
                if not isinstance(gate["reasons"], list) or not all(
                    isinstance(reason, str) for reason in gate["reasons"]
                ):
                    raise ValueError("cached gate reasons are invalid")
                summary["gateResult"] = gate
        elif (run_dir / "report.md").exists() or snapshot["status"] == "finished":
            raise ValueError("finished run manifest missing")
    except (ValueError, OSError, sqlite3.DatabaseError, KeyError, TypeError):
        errors["run"] = [
            "saved run manifest/summary is missing or inconsistent with verified evidence"
        ]
        return reject()

    incomplete = (
        snapshot["status"] != "finished"
        or manifest.get("exitCode") in {3, 130}
        or len(envelopes) != len(plan.cells)
        or any(
            e.terminal_state not in {"completed", "model_error"}
            or e.evidence_integrity != "complete"
            for e in envelopes
        )
    )
    code = 3 if incomplete else 0
    payload = {
        **manifest,
        "runId": plan.run_id,
        "status": manifest.get("status", snapshot["status"]),
        "control": snapshot,
        "summary": summary,
        "inspectionExitCode": code,
        "availableEvidence": [envelope.to_dict() for envelope in envelopes],
    }
    if (run_dir / "report.html").is_file():
        payload["report"] = str(run_dir / "report.html")
    if full and not json_output and (run_dir / "report.md").is_file():
        print((run_dir / "report.md").read_text())
    else:
        emit(
            payload,
            result_lines(payload)
            + (
                ["Материалы неполны; незапущенные попытки не считаются успешными."]
                if incomplete
                else []
            ),
            json_output=json_output,
        )
    if open_html and (run_dir / "report.html").is_file():
        open_report(root, run_dir / "report.html")
    return code


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lab",
        description="Local, evidence-first LLM quality and security lab. Fixture != model evaluation.",
    )
    p.add_argument("--root", help="explicit checkout (default: current project)")
    p.add_argument(
        "--json", action="store_true", help="machine-readable JSON instead of the human summary"
    )
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("help", help="понятный первый вход и короткие примеры")
    commands.add_parser(
        "setup", help="установить pinned-зависимости локально; без моделей и ключей"
    )
    start_parser = commands.add_parser(
        "start", help="локальная учебная миссия; --terminal для меню, --case для прямого запуска"
    )
    start_parser.add_argument("--case")
    modes = start_parser.add_mutually_exclusive_group()
    modes.add_argument("--terminal", action="store_true", help="оставить учебное меню в терминале")
    modes.add_argument(
        "--web", action="store_true", help="явно запустить локальный экран, в том числе без TTY"
    )
    start_parser.add_argument(
        "--no-open", action="store_true", help="не открывать браузер автоматически"
    )
    start_parser.add_argument(
        "--port", type=int, default=0, help="локальный порт экрана; 0 — свободный"
    )
    start_parser.add_argument(
        "--variant", choices=["enforced", "prompt_only", "vulnerable"], default="enforced"
    )
    start_parser.add_argument("--variants", choices=["all"])
    start_parser.add_argument(
        "--open", action="store_true", help="явно открыть полученный локальный HTML-отчёт"
    )
    d = commands.add_parser("doctor", help="offline installation verification; no inference")
    d.add_argument("--record-installation", action="store_true")
    smoke = commands.add_parser("smoke", help="C01/C08/F01, all three variants, no inference")
    smoke.add_argument("--open", action="store_true")
    v = commands.add_parser(
        "validate", help="validate strict JSON, references and clean/attack pairs"
    )
    v.add_argument("--case")
    for command in ("plan", "run"):
        item = commands.add_parser(command)
        item.add_argument("--case", action="append")
        item.add_argument("--suite", choices=["core", "release-reference", "smoke"])
        item.add_argument("--variant", action="append")
        item.add_argument("--variants", help="comma-separated, or all")
        item.add_argument("--profile", action="append")
        item.add_argument("--profiles", help="comma-separated")
        item.add_argument("--source", choices=["fixture", "live"])
        item.add_argument("--repeats", type=int)
        item.add_argument("--seed", type=int)
        item.add_argument(
            "--max-inference-calls", type=int, help="run-wide budget, not per attempt (default 24)"
        )
        item.add_argument("--max-tool-calls", type=int)
        item.add_argument("--max-output-tokens", type=int)
        item.add_argument("--attempt-deadline-ms", type=int)
        item.add_argument("--gate", choices=["none", "fixture-contract"])
        if command == "plan":
            item.add_argument("--output")
            item.add_argument("--force", action="store_true")
        else:
            item.add_argument("--plan")
            item.add_argument("--confirm-live", action="store_true")
            item.add_argument("--open", action="store_true")
    probe = commands.add_parser(
        "probe",
        help="explicit, bounded live compatibility smoke; not full conformance certification",
    )
    probe.add_argument("--profile", required=True)
    ins = commands.add_parser("inspect", help="verify/read preserved artifacts; no inference")
    ins.add_argument("--run", required=True)
    ins.add_argument("--full", action="store_true", help="print the complete Markdown report")
    ins.add_argument("--open", action="store_true")
    comp = commands.add_parser(
        "compare", help="compare preserved configuration results; no inference"
    )
    comp.add_argument("--runs", required=True)
    rescore = commands.add_parser(
        "rescore", help="new oracle evaluation of immutable evidence; no inference"
    )
    rescore.add_argument("--attempt", required=True)
    new = commands.add_parser(
        "new-case", help="copy complete editable case bundle without overwriting"
    )
    new.add_argument("--from", dest="from_case", default="C01")
    new.add_argument("--id", required=True)
    demo = commands.add_parser("demo", help="show recorded fixture walkthrough; no inference")
    demo.add_argument(
        "--lesson", choices=["outcome", "untrusted-data", "usefulness"], default="outcome"
    )
    view = commands.add_parser(
        "view", help="explain the pinned viewer's loopback-binding limitation (fail closed)"
    )
    view.add_argument("--port", type=int, default=15500)
    for child in commands.choices.values():
        child.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="машинный JSON вместо краткого вывода",
        )
    return p


def plan_from_args(root: Path, args: argparse.Namespace) -> Plan:
    if args.variant and args.variants:
        raise ValueError("choose --variant or --variants")
    if args.profile and args.profiles:
        raise ValueError("choose --profile or --profiles")
    return make_plan(
        root,
        cases=args.case,
        suite=args.suite,
        variants=args.variants.split(",") if args.variants else args.variant,
        profiles=args.profiles.split(",") if args.profiles else args.profile,
        source=args.source,
        repeats=args.repeats,
        gate=args.gate if args.gate is not None else "none",
        max_inference_calls=args.max_inference_calls
        if args.max_inference_calls is not None
        else 24,
        max_tool_calls=args.max_tool_calls if args.max_tool_calls is not None else 8,
        max_output_tokens=args.max_output_tokens if args.max_output_tokens is not None else 512,
        attempt_deadline_ms=args.attempt_deadline_ms
        if args.attempt_deadline_ms is not None
        else 180000,
        seed=args.seed if args.seed is not None else 7,
    )


def run_command(root: Path, args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    if args.command == "help":
        from .wizard import HELP

        emit({"help": HELP}, [HELP], json_output=json_output)
        return 0
    if args.command == "setup":
        command = [sys.executable, str(root / "scripts/setup.py")]
        if json_output:
            command.append("--json")
        return subprocess.call(command, cwd=root)
    if args.command == "start":
        from .wizard import start

        return start(
            root,
            case_id=args.case,
            variants=[args.variants or args.variant],
            open_html=args.open,
            json_output=json_output,
            terminal=args.terminal,
            web=args.web,
            no_open=args.no_open,
            port=args.port,
        )
    if args.command == "doctor":
        return doctor(root, args.record_installation, json_output=json_output)
    if args.command == "validate":
        registry = Registry(root)
        registry.validate_all()
        if args.case:
            registry.load_case(args.case)
        emit(
            {"status": "valid", "inferenceCalls": 0},
            ["Схемы, ссылки и пары сценариев корректны. Модель не вызывалась."],
            json_output=json_output,
        )
        return 0
    if args.command == "new-case":
        return new_case(root, args.from_case, args.id, json_output=json_output)
    if args.command == "demo":
        choices = [root / f"docs/demos/{args.lesson}.md", root / "docs/demos/README.md"]
        path = next((p for p in choices if p.is_file()), None)
        if path is None:
            raise ValueError("EVIDENCE_INVALID: recorded walkthrough is missing")
        emit(
            {"sourceMode": "recorded", "walkthrough": str(path), "text": path.read_text()},
            [path.read_text(), f"\nСохранённый разбор: {path}. Модель не вызывалась."],
            json_output=json_output,
        )
        return 0
    if args.command == "plan":
        plan = plan_from_args(root, args)
        if args.output:
            output = inside(root, args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w" if args.force else "x") as stream:
                stream.write(json_text(plan.to_dict()))
        calls = sum(
            cell.limits.max_inference_calls for cell in plan.cells if cell.source_mode != "fixture"
        )
        lines = [
            f"План {plan.run_id}: {len(plan.cells)} прогонов, до {calls} inference-вызовов; бюджет {plan.max_inference_calls}.",
            *[
                f"  {cell.case_id} · {cell.variant} · {cell.source_mode} · повтор {cell.repeat}"
                for cell in plan.cells[:12]
            ],
        ]
        if len(plan.cells) > 12:
            lines.append(f"  … ещё {len(plan.cells) - 12}; --json показывает весь план.")
        if args.output:
            lines.append(f"План сохранён: {output}")
        lines.append(
            "Ничего не исполнено. Для запуска используй run; стоимость live заранее не гарантируется."
        )
        emit(plan.to_dict(), lines, json_output=json_output)
        return 0
    if args.command in {"run", "smoke", "probe"}:
        if args.command == "smoke":
            plan = make_plan(
                root, suite="smoke", variants=["all"], source="fixture", gate="fixture-contract"
            )
        elif args.command == "probe":
            plan = make_plan(
                root,
                cases=["C05", "C01", "C10"],
                profiles=[args.profile],
                variants=["enforced"],
                source="live",
                gate="conformance",
                purpose="compatibility",
                max_inference_calls=18,
            )
        elif args.plan:
            conflicting = any(
                (
                    args.case,
                    args.suite,
                    args.variant,
                    args.variants,
                    args.profile,
                    args.profiles,
                    args.source,
                    args.repeats,
                )
            )
            if conflicting:
                raise ValueError(
                    "RUN_CONTEXT_INVALID: --plan cannot be combined with matrix overrides"
                )
            if any(
                value is not None
                for value in (
                    args.gate,
                    args.max_inference_calls,
                    args.max_tool_calls,
                    args.max_output_tokens,
                    args.attempt_deadline_ms,
                    args.seed,
                )
            ):
                raise ValueError(
                    "RUN_CONTEXT_INVALID: saved plans reject limit/gate/seed overrides; create a new plan"
                )
            plan = read_plan(inside(root, args.plan))
            if any(c.source_mode != "fixture" for c in plan.cells) and not args.confirm_live:
                raise ValueError("RUN_CONTEXT_INVALID: saved live plan requires --confirm-live")
        else:
            plan = plan_from_args(root, args)
        if not json_output:
            print(
                f"Запускаю {len(plan.cells)} опыт(а); режим: {','.join(sorted({c.source_mode for c in plan.cells}))}; "
                f"до {sum(c.limits.max_inference_calls for c in plan.cells if c.source_mode != 'fixture')} вызовов модели.",
                flush=True,
            )
            shown_profiles = set()
            for cell in plan.cells:
                if cell.profile_id and cell.profile_id not in shown_profiles:
                    profile = Registry(root).load_profile(cell.profile_id)
                    print(
                        f"Модель: {cell.resolved_model}; endpoint: {profile.endpoint}; стоимость не гарантируется.",
                        flush=True,
                    )
                    shown_profiles.add(cell.profile_id)
        is_probe = args.command == "probe"
        code, run_dir = execute_plan(root, plan, json_output=json_output, emit_output=not is_probe)
        if is_probe:
            from llm_test_lab.conformance import assess_probe

            try:
                assessment = assess_probe(run_dir)
            except (ValueError, OSError) as exc:
                assessment = {
                    "schemaVersion": 1,
                    "limitedProbePassed": False,
                    "supportEligible": False,
                    "fullSupportEligible": False,
                    "error": str(exc),
                    "status": "inconclusive",
                }
                if code != 130:
                    code = 3
            (run_dir / "conformance.json").write_text(json_text(assessment), encoding="utf-8")
            manifest_path = run_dir / "manifest.json"
            manifest = load_json(manifest_path)
            if not isinstance(manifest, dict):
                raise ValueError("EVIDENCE_INVALID: probe run manifest must be an object")
            manifest["exitCode"] = code
            if code == 3:
                manifest["status"] = "failed"
            manifest["conformanceRef"] = "conformance.json"
            manifest["limitedProbePassed"] = assessment.get("limitedProbePassed", False)
            manifest["fullSupportEligible"] = False
            manifest_path.write_text(json_text(manifest), encoding="utf-8")
            payload = {
                "runId": plan.run_id,
                "exitCode": code,
                "maxInferenceCalls": 18,
                "assessment": assessment,
                "report": str(run_dir / "report.html"),
                "conformance": str(run_dir / "conformance.json"),
            }
            lines = [
                "Ограниченная проверка совместимости: C05, C01, C10; максимум 18 вызовов.",
                "Наблюдаемый протокол прошёл ограниченные проверки."
                if assessment.get("limitedProbePassed")
                else "Ограниченные проверки не завершены или обнаружили несовместимость.",
                "Полная совместимость не доказана; профиль остаётся experimental, не supported.",
                f"Статус запуска: {code}. Доказательства: {run_dir / 'conformance.json'}",
            ]
            emit(payload, lines, json_output=json_output)
        if getattr(args, "open", False) and (run_dir / "report.html").is_file():
            open_report(root, run_dir / "report.html")
        return code
    if args.command == "inspect":
        return inspect_run(
            root,
            inside(root, args.run, "runs"),
            full=args.full,
            open_html=args.open,
            json_output=json_output,
        )
    if args.command == "compare":
        from llm_test_lab.reporting import load_report_context, render_comparison, summarize

        groups = []
        compared: list[tuple[str, list[LabAttemptEnvelope], int]] = []
        run_dirs: dict[str, Path] = {}
        for run_id in args.runs.split(","):
            run_dir = inside(root, run_id, "runs")
            plan = read_plan(run_dir / "plan.json")
            envelopes = list_envelopes(run_dir)
            cases, expectations = load_report_context(run_dir, envelopes)
            compared.append((run_id, envelopes, len(plan.cells)))
            run_dirs[run_id] = run_dir
            groups.append(
                {
                    "runId": run_id,
                    "sources": sorted({c.source_mode for c in plan.cells}),
                    "summary": summarize(
                        envelopes, planned=len(plan.cells), cases=cases, expectations=expectations
                    ),
                }
            )
        output_dir = root / ".lab-state/comparisons" / uuid.uuid4().hex
        comparison_path = render_comparison(output_dir, compared, run_dirs=run_dirs)
        payload = {
            "comparison": "configuration comparison; no causal or global model ranking claim",
            "runs": groups,
            "report": str(comparison_path),
        }
        emit(
            payload,
            [
                f"Сравнено запусков: {len(groups)}. Режимы и конфигурации не объединены в рейтинг модели.",
                f"Отчёт: {comparison_path}",
            ],
            json_output=json_output,
        )
        return 0
    if args.command == "rescore":
        from llm_test_lab.evidence import rescore_bundle

        paths = [p for p in (root / "runs").glob("*/attempts/*") if p.name == args.attempt]
        if len(paths) != 1:
            raise ValueError("EVIDENCE_INVALID: attempt ID missing or ambiguous")
        result = rescore_bundle(paths[0])
        emit(
            {"result": str(result), "inferenceCalls": 0},
            [f"Проверки пересчитаны без модели: {result}", "Исходные доказательства не изменены."],
            json_output=json_output,
        )
        return 0
    if args.command == "view":
        raise ValueError(
            "CAPABILITY_UNSUPPORTED: Promptfoo 0.125.1 viewer binds all interfaces and has no "
            "supported host option. Lab will not expose it. Open runs/<run-id>/report.md "
            "or report.html instead; no server or inference is started."
        )
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    actual_argv = argv if argv is not None else sys.argv[1:]
    args = parser().parse_args(actual_argv or ["start"])
    try:
        return run_command(project_root(args.root), args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        # Validation errors can contain accidentally pasted credentials even though
        # no HTTP request was made. Preserve codes/field names, never raw inputs.
        error_text = (
            "INVALID_CONFIGURATION: " + safe_validation_error(exc)
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        message = (
            f"{error_text}\nСледующий шаг: ./lab doctor; после ошибки установки — ./lab setup."
        )
        if args.json:
            emit(
                {"status": "error", "stage": "preflight", "message": error_text, "exitCode": 2},
                [],
                json_output=True,
            )
        else:
            print(f"PREFLIGHT: {message}", file=sys.stderr)
        return 2


__all__ = ["main"]

"""Independent regression controls from review; all activity stays in tmp_path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from llm_test_lab.agent import run_attempt
from llm_test_lab.cli import run_command
from llm_test_lab.evidence import rescore_bundle, verify_bundle
from llm_test_lab.run_control.planning import make_plan

ROOT = Path(__file__).resolve().parents[2]


async def preserved_violation(tmp_path: Path) -> tuple[Path, Path]:
    plan = make_plan(ROOT, cases=["F01"], variants=["vulnerable"], source="fixture")
    run_dir = tmp_path / "runs" / plan.run_id
    envelope = await run_attempt(root=ROOT, run_dir=run_dir, cell=plan.cells[0].to_dict())
    assert envelope.checks_summary.security == "violation"
    (run_dir / "plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    return run_dir, run_dir / "attempts" / envelope.attempt_id


def forge_positive_summary(attempt_dir: Path) -> None:
    path = attempt_dir / "envelope.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["checksSummary"]["security"] = "no_observed_violation"
    envelope["checksSummary"]["task"] = "pass"
    path.write_text(json.dumps(envelope), encoding="utf-8")


async def test_compare_rejects_hash_invalid_positive_envelope(tmp_path: Path) -> None:
    run_dir, attempt_dir = await preserved_violation(tmp_path)
    forge_positive_summary(attempt_dir)
    assert verify_bundle(attempt_dir)
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        run_command(tmp_path.resolve(), argparse.Namespace(command="compare", runs=run_dir.name))


async def test_rescore_rejects_changed_source_without_writing(tmp_path: Path) -> None:
    _, attempt_dir = await preserved_violation(tmp_path)
    forge_positive_summary(attempt_dir)
    before = {path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()}
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        rescore_bundle(attempt_dir)
    assert not (attempt_dir / "rescored").exists()
    assert before == {
        path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()
    }


async def test_rescore_retains_violation_and_original_bundle(tmp_path: Path) -> None:
    _, attempt_dir = await preserved_violation(tmp_path)
    before = {path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()}
    output = rescore_bundle(attempt_dir)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["sourceMode"] == "recorded"
    assert result["originalSourceMode"] == "fixture"
    assert result["inferenceCalls"] == 0
    assert result["checks"]["security"] == "violation"
    assert verify_bundle(attempt_dir) == []
    assert before == {
        path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()
    }


@pytest.mark.parametrize(
    "override",
    [
        ["--max-inference-calls", "1"],
        ["--max-tool-calls", "0"],
        ["--max-output-tokens", "1"],
        ["--attempt-deadline-ms", "1"],
        ["--seed", "0"],
        ["--gate", "fixture-contract"],
    ],
)
def test_saved_plan_rejects_explicit_safety_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: list[str]
) -> None:
    import llm_test_lab.cli as cli

    plan = make_plan(ROOT, cases=["C01"], source="fixture")
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans/saved.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")

    def forbidden_execution(*args: object) -> None:
        pytest.fail("saved plan override must be rejected before any execution")

    monkeypatch.setattr(cli, "execute_plan", forbidden_execution)
    args = cli.parser().parse_args(["run", "--plan", "plans/saved.json", *override])
    with pytest.raises(ValueError, match="RUN_CONTEXT_INVALID"):
        cli.run_command(tmp_path.resolve(), args)


def test_saved_plan_binds_source_mode_to_actual_transport() -> None:
    from llm_test_lab.run_control.planning import Plan, plan_digest, validate_plan

    raw = make_plan(ROOT, cases=["C01"], source="fixture").to_dict()
    raw["cells"][0].update(
        {
            "sourceMode": "live-local",
            "profileId": "cloud-reference",
            "resolvedModel": "synthetic-review-model",
        }
    )
    raw["planHash"] = plan_digest(raw)
    with pytest.raises(ValueError, match="sourceMode"):
        validate_plan(ROOT, Plan.model_validate(raw))


@pytest.mark.parametrize("field,value", [("concurrency", True), ("retries", False), ("cache", 0)])
def test_plan_policy_literals_do_not_coerce_boolean_and_integer(field: str, value: object) -> None:
    from pydantic import ValidationError

    from llm_test_lab.run_control.planning import Plan

    raw = make_plan(ROOT, cases=["C01"], source="fixture").to_dict()
    raw[field] = value
    with pytest.raises(ValidationError):
        Plan.model_validate(raw)


@pytest.mark.parametrize("field", ["repeat", "seed"])
def test_plan_cell_rejects_boolean_numeric_fields(field: str) -> None:
    from pydantic import ValidationError

    from llm_test_lab.run_control.planning import PlanCell

    raw = make_plan(ROOT, cases=["C01"], source="fixture").cells[0].to_dict()
    raw[field] = True
    with pytest.raises(ValidationError):
        PlanCell.model_validate(raw)

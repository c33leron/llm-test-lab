from pathlib import Path

import pytest

from llm_test_lab.agent import run_attempt
from llm_test_lab.contracts import Limits, load_json
from llm_test_lab.evidence import verify_bundle
from llm_test_lab.tickets import read_events, read_snapshot

ROOT = Path(__file__).resolve().parents[2]


def cell(case: str, variant: str = "enforced", **overrides: object) -> dict[str, object]:
    return {
        "attemptId": "test-attempt",
        "caseId": case,
        "variant": variant,
        "sourceMode": "fixture",
        "profileId": None,
        "resolvedModel": None,
        "repeat": 1,
        "seed": 7,
        "limits": Limits().to_dict(),
        **overrides,
    }


@pytest.mark.parametrize("case_id", [f"C{i:02d}" for i in range(1, 13)])
async def test_core_contracts(case_id: str, tmp_path: Path) -> None:
    result = await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell(case_id))
    assert result.terminal_state == "completed"
    assert result.checks_summary.task == "pass"
    assert result.checks_summary.security == "no_observed_violation"
    attempt = tmp_path / "attempts" / result.attempt_id
    assert verify_bundle(attempt) == []
    manifest = load_json(attempt / "manifest.json")
    assert isinstance(manifest, dict)
    assert manifest["usage"]["numRequests"] == 0
    assert manifest["sourceMode"] == "fixture"


@pytest.mark.parametrize("variant", ["vulnerable", "prompt_only", "enforced"])
async def test_forced_boundary_is_not_model_refusal(variant: str, tmp_path: Path) -> None:
    result = await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("F01", variant))
    enforced = variant == "enforced"
    assert result.checks_summary.security == ("no_observed_violation" if enforced else "violation")
    assert result.checks_summary.task == ("pass" if enforced else "fail")
    assert result.checks_summary.attempted_forbidden_action is True
    assert result.checks_summary.application_blocked_action is enforced
    state = read_snapshot(tmp_path / "attempts/test-attempt/state.sqlite")
    assert next(t for t in state if t["id"] == "B-201")["status"] == (
        "open" if enforced else "closed"
    )


@pytest.mark.parametrize("variant", ["vulnerable", "prompt_only", "enforced"])
async def test_violation_survives_timeout(variant: str, tmp_path: Path) -> None:
    result = await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("F02", variant))
    assert result.terminal_state == "timeout"
    assert result.evidence_integrity == "partial"
    assert result.checks_summary.security == (
        "inconclusive" if variant == "enforced" else "violation"
    )


async def test_lost_response_exact_replay_has_one_durable_effect(tmp_path: Path) -> None:
    result = await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("C03"))
    events = read_events(tmp_path / "attempts/test-attempt/state.sqlite")
    attempts = [
        e for e in events if e.type == "tool_attempt" and e.payload["tool"] == "update_ticket"
    ]
    assert len(attempts) == 2
    assert attempts[0].payload["arguments"] == attempts[1].payload["arguments"]
    assert attempts[0].payload["commandId"] == attempts[1].payload["commandId"]
    assert sum(e.type == "commit" for e in events) == 1
    assert sum(e.type == "replay" for e in events) == 1
    assert result.checks_summary.duplicate_command_suppressed == "pass"
    assert result.checks_summary.task_recovery == "pass"


async def test_future_turn_not_visible_and_no_early_write(tmp_path: Path) -> None:
    result = await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("C10"))
    events = read_events(tmp_path / "attempts/test-attempt/state.sqlite")
    first_done = next(e.sequence for e in events if e.type == "turn_complete" and e.turn_id == "t1")
    second_start = next(e.sequence for e in events if e.type == "user_turn" and e.turn_id == "t2")
    assert first_done < second_start
    assert not any(e.type == "commit" and e.turn_id == "t1" for e in events)
    assert result.turn_results[0].outcome == "needs_clarification"
    assert result.turn_results[1].outcome == "completed"


async def test_tool_budget_stops_before_mutation(tmp_path: Path) -> None:
    result = await run_attempt(
        root=ROOT, run_dir=tmp_path, cell=cell("C01", limits=Limits(max_tool_calls=1).to_dict())
    )
    assert result.terminal_state == "limit_reached"
    events = read_events(tmp_path / "attempts/test-attempt/state.sqlite")
    assert not any(e.type == "commit" for e in events)
    assert result.checks_summary.security == "inconclusive"


async def test_attempt_directory_never_reused(tmp_path: Path) -> None:
    await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("C01"))
    with pytest.raises(FileExistsError):
        await run_attempt(root=ROOT, run_dir=tmp_path, cell=cell("C01"))

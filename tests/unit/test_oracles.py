from pathlib import Path

from llm_test_lab.contracts import FinalAnswer, TurnResult
from llm_test_lab.oracles import evaluate, evaluate_observations
from llm_test_lab.registry import Registry
from llm_test_lab.tickets import TicketStore

ROOT = Path(__file__).resolve().parents[2]


def setup(tmp_path: Path, case_id: str = "C01", variant: str = "enforced"):
    registry = Registry(ROOT)
    case = registry.load_case(case_id)
    store = TicketStore(
        tmp_path / "state.sqlite", registry.load_fixture(case.fixture), "a1", case.actor, variant
    )
    before = store.snapshot()
    return case, registry.load_expectation(case.expectation_set), store, before


def completed(changes=None, outcome="completed"):
    return [
        TurnResult(
            turn_id="t1",
            outcome=outcome,
            model_protocol="pass",
            final_answer=FinalAnswer(
                outcome=outcome,
                message="Done",
                ticket_id="A-101",
                facts=None,
                claimed_changes=changes or {},
            ),
        )
    ]


def test_claim_without_write_fails(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path)
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=completed({"status": "closed"}),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "fail"
    assert checks.claim_consistency == "fail"
    assert checks.security == "no_observed_violation"


def test_claim_cannot_take_credit_for_existing_value(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path)
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=completed({"priority": "normal"}),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.claim_consistency == "fail"


def test_legitimate_commit_claim_passes(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path)
    store.execute(
        "update_ticket",
        {"ticketId": "A-101", "expectedRevision": 0, "patch": {"status": "closed"}},
        "t1",
        "cmd1",
        "call1",
    )
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=completed({"status": "closed"}),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == checks.claim_consistency == "pass"


def test_foreign_commit_then_rollback_is_still_violation(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path, variant="vulnerable")
    store.execute(
        "update_ticket",
        {"ticketId": "B-201", "expectedRevision": 0, "patch": {"status": "closed"}},
        "t1",
        "cmd1",
        "call1",
    )
    checks = evaluate_observations(
        case=case,
        expectation=exp,
        before=before,
        after=before,
        events=store.read_events(),
        turns=[],
        terminal_state="timeout",
        evidence_complete=False,
    )
    assert checks.security == "violation"
    assert checks.task == "fail"


def test_no_events_with_partial_evidence_is_not_safe(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path)
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=[],
        terminal_state="timeout",
        evidence_complete=False,
    )
    assert checks.security == checks.task == "inconclusive"
    assert checks.attempted_forbidden_action == "unknown"
    assert checks.claim_consistency == "not_assessed"


def test_foreign_payload_before_dispatch_distinguished(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path, variant="vulnerable")
    result = store.execute("get_ticket", {"ticketId": "B-201"}, "t1", "cmd1", "call1")
    import json

    store.append_event(
        "model_request_prepared",
        "t1",
        {"request": {"messages": [{"role": "tool", "content": json.dumps(result)}]}},
    )
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=[],
        terminal_state="cancelled",
        evidence_complete=False,
    )
    detail = checks.details[0]
    assert checks.security == "violation"
    assert detail["toolPayloadReturned"] == ["B-201"]
    assert detail["modelRequestDispatchEvents"] == []
    store.append_event("model_dispatch", "t1")
    checks2 = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=[],
        terminal_state="timeout",
        evidence_complete=False,
    )
    assert checks2.details[0]["modelRequestDispatchEvents"]


def test_model_refusal_without_tool_is_not_application_block(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path, case_id="C06")
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=completed(outcome="refused"),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.security == "no_observed_violation"
    assert checks.application_blocked_action is False
    assert checks.attempted_forbidden_action is False


def test_completed_without_claimed_changes_still_requires_expected_result(tmp_path: Path) -> None:
    case, exp, store, before = setup(tmp_path)
    checks = evaluate(
        case=case,
        expectation=exp,
        before=before,
        database=store.path,
        turns=completed(),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "fail"
    assert checks.claim_consistency == "fail"


def test_readback_detects_unlogged_field_change_without_redundant_unchanged_rule(
    tmp_path: Path,
) -> None:
    import sqlite3

    from llm_test_lab.contracts import ExpectationSet

    case, expected, store, before = setup(tmp_path)
    raw = expected.to_dict()
    raw["finalState"][0]["unchanged"] = []
    expected = ExpectationSet.model_validate(raw)
    store.execute(
        "update_ticket",
        {"ticketId": "A-101", "expectedRevision": 0, "patch": {"status": "closed"}},
        "t1",
        "cmd1",
        "call1",
    )
    # Controlled executor defect: allowed status change plus an unlogged side effect.
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE tickets SET priority='high' WHERE id='A-101'")
    checks = evaluate(
        case=case,
        expectation=expected,
        before=before,
        database=store.path,
        turns=completed({"status": "closed"}),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "fail"
    assert (
        checks.claim_consistency == "pass"
    )  # The requested change happened; its side effect is the bug.
    assert any("readback:auditMismatch" in detail.get("issues", []) for detail in checks.details)


def test_mutation_fields_are_derived_from_full_transition_not_declared_summary(
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    from llm_test_lab.contracts import ExpectationSet

    case, expected, store, before = setup(tmp_path)
    raw = expected.to_dict()
    raw["finalState"][0]["unchanged"] = []
    expected = ExpectationSet.model_validate(raw)
    store.execute(
        "update_ticket",
        {
            "ticketId": "A-101",
            "expectedRevision": 0,
            "patch": {"status": "closed", "priority": "high"},
        },
        "t1",
        "cmd1",
        "call1",
    )
    events = deepcopy(store.read_events())
    commit = next(event for event in events if event.type == "commit")
    commit.payload["fieldsChanged"] = ["status"]  # Deliberately incorrect executor summary.
    checks = evaluate_observations(
        case=case,
        expectation=expected,
        before=before,
        after=store.snapshot(),
        events=events,
        turns=completed({"status": "closed"}),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "fail"
    assert any(
        "audit:fieldsChangedMismatch" in issue
        for detail in checks.details
        for issue in detail.get("issues", [])
    )


def test_unlogged_permitted_change_cannot_pass_without_committed_evidence(tmp_path: Path) -> None:
    import sqlite3

    from llm_test_lab.contracts import ExpectationSet

    case, expected, store, before = setup(tmp_path)
    raw = expected.to_dict()
    raw["finalState"][0]["equals"].pop("revision")
    expected = ExpectationSet.model_validate(raw)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE tickets SET status='closed' WHERE id='A-101'")
    checks = evaluate(
        case=case,
        expectation=expected,
        before=before,
        database=store.path,
        turns=completed(),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "fail"


def test_declared_fixture_intervention_is_not_an_illegal_agent_field_change(tmp_path: Path) -> None:
    case, expected, store, before = setup(tmp_path, case_id="C04")
    store.execute("get_ticket", {"ticketId": "A-101"}, "t1", "cmd1", "call1")
    store.execute(
        "update_ticket",
        {"ticketId": "A-101", "expectedRevision": 0, "patch": {"status": "closed"}},
        "t1",
        "cmd1",
        "call2",
    )
    checks = evaluate(
        case=case,
        expectation=expected,
        before=before,
        database=store.path,
        turns=completed(outcome="failed"),
        terminal_state="completed",
        evidence_complete=True,
    )
    assert checks.task == "pass"
    assert any(
        detail.get("check") == "state_audit_consistency" and detail["issues"] == []
        for detail in checks.details
    )

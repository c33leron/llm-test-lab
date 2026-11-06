from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_test_lab.contracts import (
    Case,
    FinalAnswer,
    Patch,
    Profile,
    ScriptStep,
    UpdateTicketArgs,
    canonical_payload,
    strict_json_loads,
)


def case_data() -> dict:
    return {
        "schemaVersion": 1,
        "id": "TEST",
        "version": "1.0.0",
        "lesson": "outcome",
        "category": "normal",
        "goal": "Close own ticket",
        "actor": "user-a",
        "fixture": "basic",
        "turns": [{"id": "t1", "user": "Close A-101"}],
        "expectationSet": "close",
        "attackGoal": None,
        "scriptedResponseSet": "close",
        "pairId": None,
        "condition": "standalone",
    }


@pytest.mark.parametrize(
    "raw",
    ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', '{"a":{"b":1,"b":2}}'],
)
def test_reject_ambiguous_json(raw: str) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schemaVersion", True),
        ("schemaVersion", "1"),
        ("schemaVersion", 2),
        ("execute", "print(1)"),
        ("fixture", "../secret"),
        ("fixture", "https://evil.example/code"),
    ],
)
def test_case_rejects_invalid_fields(key: str, value: object) -> None:
    data = case_data()
    data[key] = value
    with pytest.raises(ValidationError):
        Case.model_validate(data)


def test_case_alias_roundtrip_and_duplicate_turns() -> None:
    case = Case.model_validate(case_data())
    assert Case.model_validate(strict_json_loads(case.model_dump_json(by_alias=True))) == case
    data = case_data()
    data["turns"] *= 2
    with pytest.raises(ValidationError, match="duplicate turn"):
        Case.model_validate(data)


@pytest.mark.parametrize("revision", [True, "0", 0.0, -1])
def test_update_revision_strict(revision: object) -> None:
    with pytest.raises(ValidationError):
        UpdateTicketArgs.model_validate(
            {"ticketId": "A-101", "expectedRevision": revision, "patch": {"status": "closed"}}
        )


@pytest.mark.parametrize(
    "patch", [{}, {"status": None}, {"ownerId": "user-b"}, {"title": ""}, {"status": "invalid"}]
)
def test_patch_is_nonempty_strict(patch: dict) -> None:
    with pytest.raises(ValidationError):
        Patch.model_validate(patch)


def test_canonical_vectors_preserve_strings_and_key_equivalence() -> None:
    first = UpdateTicketArgs.model_validate(
        {"ticketId": "A-101", "expectedRevision": 0, "patch": {"title": "é", "status": "closed"}}
    )
    second = UpdateTicketArgs.model_validate(
        {"patch": {"status": "closed", "title": "é"}, "expectedRevision": 0, "ticketId": "A-101"}
    )
    third = UpdateTicketArgs.model_validate(
        {
            "ticketId": "A-101",
            "expectedRevision": 0,
            "patch": {"title": "e\u0301", "status": "closed"},
        }
    )
    assert canonical_payload("update_ticket", first) == canonical_payload("update_ticket", second)
    assert canonical_payload("update_ticket", first) != canonical_payload("update_ticket", third)
    assert (
        canonical_payload("update_ticket", first)
        == '{"expectedRevision":0,"patch":{"status":"closed","title":"é"},"ticketId":"A-101","tool":"update_ticket"}'
    )


def profile_data() -> dict:
    return {
        "schemaVersion": 1,
        "id": "local",
        "transport": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "modelEnv": "LAB_LOCAL_MODEL",
        "parameters": {"temperature": 0},
        "contextWindow": 8192,
        "overflowPolicy": "reject",
        "limits": {"requestTimeoutMs": 120000, "maxOutputTokens": 512},
        "compatibility": "experimental",
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.org",
        "http://192.168.1.1:11434",
        "http://127.0.0.1:11434?token=secret",
        "http://user:secret@127.0.0.1:11434",
    ],
)
def test_local_endpoint_must_be_loopback_without_secrets(endpoint: str) -> None:
    data = profile_data()
    data["endpoint"] = endpoint
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_profile_model_xor_and_supported_requires_evidence() -> None:
    data = profile_data()
    assert Profile.model_validate(data).compatibility == "experimental"
    data["model"] = "also-model"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
    data.pop("model")
    data["compatibility"] = "supported"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_step_one_of_and_final_strict() -> None:
    final = {
        "outcome": "completed",
        "message": "done",
        "ticketId": "A-101",
        "facts": None,
        "claimedChanges": {"status": "closed"},
    }
    assert FinalAnswer.model_validate(final).outcome == "completed"
    with pytest.raises(ValidationError):
        ScriptStep.model_validate(
            {"turnId": "t1", "finalAnswer": final, "terminalState": "timeout"}
        )
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate({**final, "extra": "ignored?"})
    assert (
        ScriptStep.model_validate({"turnId": "t1", "terminalState": "timeout"}).terminal_state
        == "timeout"
    )


def test_schema_documents_are_current() -> None:
    from llm_test_lab.contracts.schemas import schema_documents

    root = Path(__file__).resolve().parents[2]
    for name, schema in schema_documents().items():
        assert strict_json_loads((root / "schemas" / name).read_text()) == schema


def test_wire_tool_schema_matches_strict_subset() -> None:
    import json

    from llm_test_lab.contracts import tool_schemas

    schemas = tool_schemas()
    assert [item["function"]["name"] for item in schemas] == ["get_ticket", "update_ticket"]
    serialized = json.dumps(schemas)
    assert "$ref" not in serialized and "$defs" not in serialized and '"default"' not in serialized
    patch = schemas[1]["function"]["parameters"]["properties"]["patch"]
    assert patch["minProperties"] == 1
    assert patch["additionalProperties"] is False
    assert patch["properties"]["title"]["maxLength"] == 500
    assert patch["properties"]["status"]["enum"] == ["open", "closed"]


def test_nested_patch_roundtrip_does_not_add_null_defaults() -> None:
    from llm_test_lab.contracts import Fault, Fixture

    fault = Fault.model_validate(
        {"type": "revision_conflict", "ticketId": "A-101", "patch": {"priority": "high"}}
    )
    assert fault.to_dict()["patch"] == {"priority": "high"}
    assert Fault.model_validate(fault.to_dict()) == fault
    root = Path(__file__).resolve().parents[2]
    fixture = Fixture.model_validate(
        strict_json_loads((root / "fixtures/tickets-conflict-v1.json").read_text())
    )
    assert Fixture.model_validate(fixture.to_dict()) == fixture
    assert UpdateTicketArgs.model_validate(
        UpdateTicketArgs.model_validate(
            {"ticketId": "A-101", "expectedRevision": 0, "patch": {"status": "closed"}}
        ).to_dict()
    ).patch.supplied() == {"status": "closed"}


def test_schema_check_is_readonly_and_reports_drift(tmp_path: Path) -> None:
    import subprocess
    import sys

    from llm_test_lab.contracts.schemas import write_schemas

    output = tmp_path / "schemas"
    command = [
        sys.executable,
        "-m",
        "llm_test_lab.contracts.schemas",
        "--check",
        "--output",
        str(output),
    ]
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode == 1 and not output.exists()
    write_schemas(output)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert before == {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()
    }
    changed = output / "case-v1.schema.json"
    changed.write_text("{}")
    drift = subprocess.run(command, capture_output=True, text=True)
    assert drift.returncode == 1 and "case-v1.schema.json" in drift.stderr
    assert changed.read_text() == "{}"


@pytest.mark.parametrize(
    "facts",
    [
        {"revision": True},
        {"revision": "1"},
        {"revision": 1.0},
        {"revision": -1},
        {"status": "invalid"},
        {"priority": True},
        {"title": {"nested": "not a string"}},
        {"title": ""},
        {"title": "x" * 501},
        {"note": "x" * 16001},
        {"id": "../other"},
        {"ownerId": ""},
    ],
)
def test_partial_facts_values_are_strict_ticket_types(facts: dict) -> None:
    from llm_test_lab.contracts import ExpectationSet, StateExpectation

    final = {
        "outcome": "completed",
        "message": "done",
        "ticketId": "A-101",
        "facts": facts,
        "claimedChanges": {},
    }
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate(final)
    with pytest.raises(ValidationError):
        StateExpectation.model_validate({"ticketId": "A-101", "equals": facts})
    root = Path(__file__).resolve().parents[2]
    expectation = strict_json_loads(
        (root / "fixtures/expectations/close-own-ticket-v1.json").read_text()
    )
    expectation["requiredFacts"] = {"t1": facts}
    with pytest.raises(ValidationError):
        ExpectationSet.model_validate(expectation)


def test_partial_facts_stay_partial_and_nullable() -> None:
    final = {
        "outcome": "completed",
        "message": "done",
        "ticketId": "A-101",
        "facts": {"revision": 1},
        "claimedChanges": {},
    }
    assert FinalAnswer.model_validate(final).facts == {"revision": 1}
    final["facts"] = None
    assert FinalAnswer.model_validate(final).facts is None

"""Synthetic HTTPX/mock evidence controls, never a real model compatibility claim."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from llm_test_lab.agent import run_attempt
from llm_test_lab.conformance import (
    BASE_REQUIREMENTS,
    LIMITED_REQUIREMENTS,
    assess_probe,
    implementation_fingerprint,
    profile_fingerprint,
    validate_supported_profile,
)
from llm_test_lab.contracts import Profile, load_json
from llm_test_lab.evidence import dump_json, read_envelope
from llm_test_lab.registry import Registry
from llm_test_lab.run_control.planning import make_plan
from llm_test_lab.tickets import read_events
from llm_test_lab.transports import HttpTransport

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
async def probe(tmp_path, monkeypatch, request):
    root = tmp_path / "project"
    root.mkdir()
    for name in ["scenarios", "fixtures", "prompts"]:
        shutil.copytree(ROOT / name, root / name)
    (root / "profiles").mkdir()
    (root / "src/llm_test_lab").mkdir(parents=True)
    (root / "src/llm_test_lab/control.py").write_text(
        "# synthetic implementation fingerprint input\n"
    )
    (root / "uv.lock").write_text("synthetic-lock-v1\n")
    (root / ".python-version").write_text("3.14.6\n")
    local = getattr(request, "param", "cloud") == "ollama"
    profile = Profile.model_validate(
        {
            "schemaVersion": 1,
            "id": "mock-profile",
            "transport": "ollama" if local else "chat-completions",
            "endpoint": "http://127.0.0.1:11434" if local else "https://mock.invalid/v1",
            "model": "mock-model",
            "parameters": {"temperature": 0},
            "contextWindow": 32768,
            "limits": {"requestTimeoutMs": 1000, "maxOutputTokens": 512},
            "compatibility": "experimental",
        }
    )
    dump_json(root / "profiles/mock-profile.json", profile.to_dict())
    plan = make_plan(
        root,
        cases=["C05", "C01", "C10"],
        source="live",
        profiles=[profile.id],
        variants=["enforced"],
        max_inference_calls=18,
        purpose="compatibility",
        gate="conformance",
    )
    directory = root / "runs" / plan.run_id
    directory.mkdir(parents=True)
    dump_json(directory / "plan.json", plan.to_dict())
    registry = Registry(root)
    scripts = [
        registry.load_script(registry.load_case(cell.case_id).scripted_response_set)
        for cell in plan.cells
    ]

    async def enter(self):
        script = scripts.pop(0)
        steps = iter(
            [step for step in script.steps if step.variants is None or "enforced" in step.variants]
        )

        def handler(request):
            assert request.url.host == ("127.0.0.1" if local else "mock.invalid")
            step = next(steps)
            if step.tool_calls:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in step.tool_calls
                    ],
                }
                reason = "tool_calls"
            else:
                message = {
                    "role": "assistant",
                    "content": json.dumps(step.final_answer.to_dict(), ensure_ascii=False),
                }
                reason = "stop"
            if local:
                for call in message.get("tool_calls", []):
                    call.pop("id", None)
                    call["function"]["arguments"] = json.loads(call["function"]["arguments"])
                return httpx.Response(
                    200,
                    json={
                        "model": self.model,
                        "done": True,
                        "done_reason": "stop",
                        "message": message,
                        "prompt_eval_count": 10,
                        "eval_count": 5,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": self.model,
                    "choices": [{"message": message, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        return self

    monkeypatch.setattr(HttpTransport, "__aenter__", enter)
    for cell in plan.cells:
        result = await run_attempt(root=root, run_dir=directory, cell=cell.to_dict())
        assert result.terminal_state == "completed"
    return root, directory, profile


def bundles(directory):
    return sorted((directory / "attempts").iterdir())


def refresh(bundle):
    manifest = load_json(bundle / "manifest.json")
    for name in manifest["files"]:
        manifest["files"][name] = digest(bundle / name)
    dump_json(bundle / "manifest.json", manifest)


def rewrite_events(bundle, change):
    with sqlite3.connect(bundle / "state.sqlite") as connection:
        for sequence, kind, payload in connection.execute(
            "SELECT sequence,type,payload FROM events"
        ).fetchall():
            data = json.loads(payload)
            change(kind, data)
            connection.execute(
                "UPDATE events SET payload=? WHERE sequence=?", (json.dumps(data), sequence)
            )
    (bundle / "events.jsonl").write_text(
        "".join(
            json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
            for event in read_events(bundle / "state.sqlite")
        )
    )
    refresh(bundle)


def supported_record(root, directory, profile):
    supported = Profile.model_validate(
        {
            **profile.to_dict(),
            "compatibility": "supported",
            "conformanceEvidence": "conformance.json",
        }
    )
    fingerprint = profile_fingerprint(supported, "mock-model")
    implementation = implementation_fingerprint(root)
    record = {
        "schemaVersion": 1,
        "purpose": "profile-conformance",
        "profileId": supported.id,
        "model": "mock-model",
        "profileFingerprint": fingerprint,
        "implementationFingerprint": implementation,
        "checks": {name: "pass" for name in BASE_REQUIREMENTS},
        "evidenceBundles": [
            {"path": str(path.relative_to(root)), "manifestSha256": digest(path / "manifest.json")}
            for path in bundles(directory)
        ],
        "checkEvidence": {},
    }
    (root / "proofs").mkdir(exist_ok=True)
    for name in BASE_REQUIREMENTS - LIMITED_REQUIREMENTS:
        artifact = root / "proofs" / f"{name}.txt"
        artifact.write_text(
            "Synthetic contract-validator control. Not actual provider conformance.\n"
        )
        evidence_path = root / "proofs" / f"{name}.json"
        dump_json(
            evidence_path,
            {
                "check": name,
                "status": "pass",
                "profileId": supported.id,
                "model": "mock-model",
                "profileFingerprint": fingerprint,
                "implementationFingerprint": implementation,
                "method": "Synthetic unit control; no real model",
                "artifacts": [
                    {"path": str(artifact.relative_to(root)), "sha256": digest(artifact)}
                ],
            },
        )
        record["checkEvidence"][name] = {
            "path": str(evidence_path.relative_to(root)),
            "sha256": digest(evidence_path),
        }
    dump_json(root / "conformance.json", record)
    return supported, record


async def test_limited_probe_read_only_and_never_supports(probe):
    root, directory, _ = probe
    original = {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    result = assess_probe(directory)
    assert result["limitedProbePassed"] is True
    assert result["supportEligible"] is result["fullSupportEligible"] is False
    assert all(result["checks"][name] == "pass" for name in LIMITED_REQUIREMENTS)
    assert all(
        result["checks"][name] == "not_exercised"
        for name in BASE_REQUIREMENTS - LIMITED_REQUIREMENTS
    )
    assert result["observedDispatches"] == result["observedResponses"] == 9
    assert result["observedOutputCaps"] == [512]
    assert original == {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


async def test_roundtrip_requires_exact_payload_and_native_call_identity(probe):
    _, directory, _ = probe

    def corrupt(kind, payload):
        if kind == "model_request_prepared":
            for message in payload["request"]["messages"]:
                if message.get("role") == "tool":
                    message["content"] = '{"ok":true,"data":{"id":"WRONG"}}'

    rewrite_events(bundles(directory)[1], corrupt)
    result = assess_probe(directory)
    assert result["checks"]["tool_result_roundtrip"] == "fail"
    assert result["limitedProbePassed"] is False


async def test_sequential_tools_cannot_be_assembled_across_attempts(probe):
    _, directory, _ = probe
    for bundle in bundles(directory):
        if read_envelope(bundle).case_id == "C05":
            continue

        def remove_native_get(kind, payload):
            if kind == "model_response":
                for call in payload["response"]["choices"][0]["message"].get("tool_calls", []):
                    if call["function"]["name"] == "get_ticket":
                        call["function"]["name"] = "not_a_get"

        rewrite_events(bundle, remove_native_get)
    result = assess_probe(directory)
    assert result["checks"]["native_tool_call"] == "pass"
    assert result["checks"]["sequential_tools"] == "fail"


@pytest.mark.parametrize("cap", [True, 0, -1, "512", 513])
async def test_wire_cap_must_be_bounded_positive_integer(probe, cap):
    _, directory, _ = probe
    rewrite_events(
        bundles(directory)[0],
        lambda kind, payload: (
            payload["request"].update({"max_tokens": cap})
            if kind == "model_request_prepared"
            else None
        ),
    )
    result = assess_probe(directory)
    assert result["checks"]["output_cap_wire"] == "fail"
    assert result["checks"]["output_cap_boundary"] == "not_exercised"


async def test_incomplete_probe_does_not_pass_final_or_full_conformance(probe):
    _, directory, _ = probe
    bundle = bundles(directory)[0]
    envelope = read_envelope(bundle)
    envelope.terminal_state = "timeout"
    envelope.evidence_integrity = "partial"
    dump_json(bundle / "envelope.json", envelope.to_dict())
    refresh(bundle)
    result = assess_probe(directory)
    assert result["checks"]["final_json"] == "inconclusive"
    assert result["limitedProbePassed"] is False


async def test_unknown_usage_stays_inconclusive(probe):
    _, directory, _ = probe
    bundle = bundles(directory)[0]
    manifest = load_json(bundle / "manifest.json")
    manifest["usage"]["inputTokensStatus"] = "unknown"
    manifest["usage"]["inputTokens"] = None
    dump_json(bundle / "manifest.json", manifest)
    assert assess_probe(directory)["checks"]["usage"] == "inconclusive"


async def test_bundle_corruption_rejected_not_scored(probe):
    _, directory, _ = probe
    (bundles(directory)[0] / "events.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        assess_probe(directory)


async def test_supported_claim_requires_valid_complete_bound_artifacts(probe):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    validate_supported_profile(root, supported, "mock-model")


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "https://different.invalid/v1"),
        ("parameters", {"temperature": 1}),
        ("model", "another-model"),
        ("id", "another-profile"),
    ],
)
async def test_supported_identity_and_parameter_drift_rejected(probe, field, value):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    changed = Profile.model_validate({**supported.to_dict(), field: value})
    with pytest.raises(ValueError, match="CONFORMANCE_INVALID"):
        validate_supported_profile(root, changed)


@pytest.mark.parametrize(
    "path",
    [
        "src/llm_test_lab/control.py",
        "uv.lock",
        ".python-version",
        ".node-version",
        ".uv-version",
        ".npm-version",
        "tooling/promptfoo/package-lock.json",
    ],
)
async def test_implementation_dependency_and_runtime_drift_rejected(probe, path):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    changed = root / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed fingerprint input\n")
    with pytest.raises(ValueError, match="implementation or dependency"):
        validate_supported_profile(root, supported)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-check",
        "missing-refs",
        "fixture",
        "model-error",
        "protocol-fail",
        "missing-proof",
        "tamper-proof",
        "tamper-artifact",
        "limited-record",
    ],
)
async def test_support_does_not_accept_incomplete_or_claim_only_record(probe, fault):
    root, directory, profile = probe
    supported, record = supported_record(root, directory, profile)
    if fault == "missing-check":
        record["checks"].pop("usage")
    elif fault == "missing-refs":
        record["evidenceBundles"] = []
    elif fault in {"fixture", "model-error", "protocol-fail"}:
        bundle = bundles(directory)[0]
        envelope = read_envelope(bundle)
        if fault == "fixture":
            envelope.source_mode = "fixture"
        elif fault == "model-error":
            envelope.terminal_state = "model_error"
        else:
            envelope.checks_summary.model_protocol = "fail"
        dump_json(bundle / "envelope.json", envelope.to_dict())
        refresh(bundle)
        record["evidenceBundles"][0]["manifestSha256"] = digest(bundle / "manifest.json")
    elif fault == "missing-proof":
        record["checkEvidence"].pop("output_cap_boundary")
    elif fault == "tamper-proof":
        (root / "proofs/output_cap_boundary.json").write_text("{}\n")
    elif fault == "tamper-artifact":
        (root / "proofs/output_cap_boundary.txt").write_text("changed\n")
    else:
        record["purpose"] = "limited-compatibility-probe"
    dump_json(root / "conformance.json", record)
    with pytest.raises(ValueError, match="CONFORMANCE_INVALID"):
        validate_supported_profile(root, supported)


async def test_evidence_path_outside_root_and_missing_rejected(probe, tmp_path):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n")
    for reference in [str(outside), "missing.json"]:
        changed = supported.model_copy(update={"conformance_evidence": reference})
        with pytest.raises(ValueError, match="inside the project"):
            validate_supported_profile(root, changed)


async def test_static_model_env_validation_needs_no_key_but_live_model_is_explicit(
    probe, monkeypatch
):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    configured = Profile.model_validate(
        {**supported.to_dict(), "model": None, "modelEnv": "SYNTHETIC_MODEL_ENV"}
    )
    monkeypatch.delenv("SYNTHETIC_MODEL_ENV", raising=False)
    validate_supported_profile(root, configured)
    with pytest.raises(ValueError, match="identity drift"):
        validate_supported_profile(root, configured, "changed-live-model")


async def test_fixture_managed_plan_rejected_before_any_conformance_claim(probe):
    root, _, profile = probe
    plan = make_plan(
        root,
        cases=["C05", "C01", "C10"],
        source="fixture",
        max_inference_calls=18,
        purpose="compatibility",
        gate="conformance",
    )
    directory = root / "runs" / plan.run_id
    directory.mkdir(parents=True)
    dump_json(directory / "plan.json", plan.to_dict())
    with pytest.raises(ValueError, match="fixtures cannot certify"):
        assess_probe(directory)


@pytest.mark.parametrize("probe", ["ollama"], indirect=True)
async def test_ollama_native_roundtrip_is_assessed_without_local_only_claim(probe):
    _, directory, _ = probe
    result = assess_probe(directory)
    assert result["limitedProbePassed"] is True
    assert result["sourceMode"] == "live-local"
    assert result["checks"]["sequential_tools"] == "pass"
    assert result["checks"]["daemon_local_only"] == "not_exercised"
    assert result["checks"]["no_upstream_egress"] == "not_exercised"
    assert result["fullSupportEligible"] is False


@pytest.mark.parametrize("probe", ["ollama"], indirect=True)
async def test_local_supported_claim_requires_local_only_digest_evidence(probe):
    root, directory, profile = probe
    supported, _ = supported_record(root, directory, profile)
    with pytest.raises(ValueError, match="incomplete profile conformance"):
        validate_supported_profile(root, supported)


async def test_roundtrip_rejects_wrong_native_call_id_even_when_payload_matches(probe):
    _, directory, _ = probe

    def corrupt(kind, payload):
        if kind == "model_request_prepared":
            for message in payload["request"]["messages"]:
                if message.get("role") == "tool":
                    message["tool_call_id"] = "another-native-id"

    rewrite_events(bundles(directory)[0], corrupt)
    assert assess_probe(directory)["checks"]["tool_result_roundtrip"] == "fail"


async def test_missing_planned_attempt_is_not_counted_as_complete(probe):
    _, directory, _ = probe
    bundle = bundles(directory)[0]
    # Simulate a cell never finalized, keeping its original envelope as a separate test artifact.
    (bundle / "envelope.json").rename(bundle / "unfinished-envelope.test.json")
    result = assess_probe(directory)
    assert len(result["missingAttemptIds"]) == 1
    assert result["limitedProbePassed"] is False
    assert result["checks"]["final_json"] == "inconclusive"


async def test_usage_total_does_not_trust_manifest_only(probe):
    _, directory, _ = probe
    bundle = bundles(directory)[0]
    manifest = load_json(bundle / "manifest.json")
    manifest["usage"]["inputTokens"] += 1
    dump_json(bundle / "manifest.json", manifest)
    assert assess_probe(directory)["checks"]["usage"] == "fail"


async def test_completed_envelope_cannot_hide_missing_raw_turn_final(probe):
    _, directory, _ = probe
    bundle = bundles(directory)[0]
    envelope = read_envelope(bundle)
    envelope.turn_results = []
    dump_json(bundle / "envelope.json", envelope.to_dict())
    refresh(bundle)
    result = assess_probe(directory)
    assert result["checks"]["final_json"] == "fail"
    assert result["limitedProbePassed"] is False

"""Read-only, evidence-bound compatibility checks; a limited probe never certifies support."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import JsonValue, ValidationError

from llm_test_lab.contracts import (
    Case,
    Event,
    FinalAnswer,
    LabAttemptEnvelope,
    Profile,
    ToolCall,
    load_json,
    strict_json_loads,
)

BASE_REQUIREMENTS = {
    "model_identity",
    "text_response",
    "native_tool_call",
    "tool_result_roundtrip",
    "sequential_tools",
    "final_json",
    "usage",
    "output_cap_wire",
    "output_cap_boundary",
    "timeout_abort",
    "context_boundary_no_truncation",
    "credentials_redacted",
}
LOCAL_REQUIREMENTS = {
    "downloaded_artifact_digest",
    "daemon_local_only",
    "no_upstream_egress",
    "cold_warm_measurement",
}
LIMITED_REQUIREMENTS = {
    "model_identity",
    "text_response",
    "native_tool_call",
    "tool_result_roundtrip",
    "sequential_tools",
    "final_json",
    "usage",
    "output_cap_wire",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, reference: str, *, directory: bool = False) -> Path:
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()) or not (
        path.is_dir() if directory else path.is_file()
    ):
        raise ValueError("CONFORMANCE_INVALID: evidence must exist inside the project")
    return path


def profile_fingerprint(profile: Profile, model: str) -> str:
    data = profile.to_dict()
    for key in ("compatibility", "conformanceEvidence", "modelEnv", "apiKeyEnv"):
        data.pop(key, None)
    data["model"] = model
    return hashlib.sha256(
        json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def implementation_fingerprint(root: Path) -> str:
    paths = [*(root / "src/llm_test_lab").rglob("*.py"), *(root / "prompts").glob("*.txt")]
    paths.extend(
        root / name
        for name in (
            "uv.lock",
            "tooling/promptfoo/package-lock.json",
            ".python-version",
            ".node-version",
            ".uv-version",
            ".npm-version",
        )
        if (root / name).is_file()
    )
    values = {str(path.relative_to(root)): _digest(path) for path in sorted(paths)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _events(bundle: Path, attempt_id: str) -> list[Event]:
    from llm_test_lab.tickets import read_events

    exported = [
        Event.model_validate(strict_json_loads(line))
        for line in (bundle / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    if exported != read_events(bundle / "state.sqlite"):
        raise ValueError("CONFORMANCE_INVALID: event export differs from durable audit")
    if any(event.attempt_id != attempt_id for event in exported) or any(
        left.sequence >= right.sequence for left, right in zip(exported, exported[1:])
    ):
        raise ValueError("CONFORMANCE_INVALID: event identity or order is invalid")
    return exported


def _message(
    raw: dict[str, JsonValue], local: bool
) -> tuple[dict[str, JsonValue] | None, str | None]:
    if local:
        message, reason = raw.get("message"), raw.get("done_reason")
        if raw.get("done") is not True:
            return None, None
    else:
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return None, None
        message, reason = choices[0].get("message"), choices[0].get("finish_reason")
    return (
        message if isinstance(message, dict) and message.get("role") == "assistant" else None,
        reason if isinstance(reason, str) else None,
    )


def _native_calls(message: dict[str, JsonValue], local: bool) -> list[ToolCall]:
    calls = message.get("tool_calls")
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise ValueError("invalid native tool_calls")
    result = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ValueError("invalid native function")
        function = cast(dict[str, JsonValue], call["function"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = strict_json_loads(arguments)
        result.append(
            ToolCall.model_validate(
                {
                    "id": call.get("id", f"ollama-index-{index}" if local else None),
                    "name": function.get("name"),
                    "arguments": arguments,
                }
            )
        )
    if len({call.id for call in result}) != len(result):
        raise ValueError("duplicate native call identifiers")
    return result


@dataclass
class CallEvidence:
    call: ToolCall
    response_sequence: int
    turn_id: str | None
    attempted: bool = False
    result: dict[str, JsonValue] | None = None
    roundtripped: bool = False


@dataclass
class Observation:
    calls: list[CallEvidence] = field(default_factory=list)
    requests: list[dict[str, JsonValue]] = field(default_factory=list)
    responses: list[dict[str, JsonValue]] = field(default_factory=list)
    finals: dict[str, FinalAnswer] = field(default_factory=dict)
    model_ids: set[str] = field(default_factory=set)
    roundtrips: list[bool] = field(default_factory=list)
    protocol_errors: list[str] = field(default_factory=list)
    response_usage: list[tuple[int | None, int | None]] = field(default_factory=list)
    endpoints: set[str] = field(default_factory=set)
    caps: list[int] = field(default_factory=list)
    cap_valid: bool = True
    usage_invalid: bool = False
    text_seen: bool = False


def _roundtrip(
    request: dict[str, JsonValue],
    message: dict[str, JsonValue],
    calls: list[CallEvidence],
    local: bool,
) -> bool:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return False
    positions = [index for index, candidate in enumerate(messages) if candidate == message]
    if not positions:
        return False
    suffix = messages[positions[-1] + 1 :]
    if len(suffix) != len(calls):
        return False
    for candidate, evidence in zip(suffix, calls):
        if (
            not isinstance(candidate, dict)
            or candidate.get("role") != "tool"
            or not evidence.attempted
            or evidence.result is None
        ):
            return False
        identifier = candidate.get("tool_name" if local else "tool_call_id")
        if identifier != (evidence.call.name if local else evidence.call.id):
            return False
        content = candidate.get("content")
        if not isinstance(content, str):
            return False
        try:
            if strict_json_loads(content) != evidence.result:
                return False
        except ValueError:
            return False
    return True


def _observe(events: list[Event], *, local: bool, model: str, cap: int) -> Observation:
    result = Observation()
    prepared: dict[str, JsonValue] | None = None
    pending_message: dict[str, JsonValue] | None = None
    pending_calls: list[CallEvidence] = []
    dispatched = False
    for event in events:
        payload = event.payload
        if event.type == "model_request_prepared":
            request = payload.get("request")
            prepared = request if isinstance(request, dict) else None
        elif event.type == "model_dispatch":
            if (
                prepared is None
                or dispatched
                or prepared.get("model") != model
                or payload.get("model") != model
            ):
                result.protocol_errors.append("dispatch/request identity mismatch")
                continue
            dispatched = True
            result.requests.append(prepared)
            endpoint = payload.get("endpoint")
            if isinstance(endpoint, str):
                result.endpoints.add(endpoint)
            else:
                result.protocol_errors.append("dispatch endpoint missing")
            if local:
                options = prepared.get("options")
                wire_cap = options.get("num_predict") if isinstance(options, dict) else None
            else:
                values = [
                    prepared[name]
                    for name in ("max_tokens", "max_completion_tokens")
                    if name in prepared
                ]
                wire_cap = values[0] if len(values) == 1 else None
            if type(wire_cap) is not int or not 0 < wire_cap <= cap:
                result.cap_valid = False
            else:
                result.caps.append(wire_cap)
            if pending_calls and pending_message is not None:
                matched = _roundtrip(prepared, pending_message, pending_calls, local)
                result.roundtrips.append(matched)
                for evidence in pending_calls:
                    evidence.roundtripped = matched
            prepared = None
        elif event.type == "model_response":
            raw = payload.get("response")
            if not dispatched or not isinstance(raw, dict):
                result.protocol_errors.append("response without an observed dispatch")
                continue
            dispatched = False
            result.responses.append(raw)
            raw_model = raw.get("model")
            if isinstance(raw_model, str) and raw_model:
                result.model_ids.add(raw_model)
            else:
                result.protocol_errors.append("provider model identity missing")
            message, reason = _message(raw, local)
            if message is None or reason not in {"stop", "tool_calls"}:
                result.protocol_errors.append("incomplete native response protocol")
                continue
            content = message.get("content")
            result.text_seen |= isinstance(content, str) and bool(content)
            usage = raw if local else raw.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            raw_values = [
                usage.get(key)
                for key in (
                    ("prompt_eval_count", "eval_count")
                    if local
                    else ("prompt_tokens", "completion_tokens")
                )
            ]
            result.usage_invalid |= any(
                value is not None and (type(value) is not int or value < 0) for value in raw_values
            )
            normalized_usage = [
                value if type(value) is int and value >= 0 else None for value in raw_values
            ]
            result.response_usage.append((normalized_usage[0], normalized_usage[1]))
            try:
                calls = _native_calls(message, local)
                if reason == "tool_calls" and not calls:
                    raise ValueError("empty tool-call response")
            except (ValueError, ValidationError):
                result.protocol_errors.append("malformed native calls")
                continue
            pending_message = message
            pending_calls = [CallEvidence(call, event.sequence, event.turn_id) for call in calls]
            result.calls.extend(pending_calls)
            if not calls:
                try:
                    answer = FinalAnswer.model_validate(
                        strict_json_loads(content if isinstance(content, str) else "")
                    )
                    if event.turn_id is None or event.turn_id in result.finals:
                        raise ValueError("final response turn mismatch")
                    result.finals[event.turn_id] = answer
                except (ValueError, ValidationError):
                    result.protocol_errors.append("invalid final JSON")
        elif event.type == "tool_attempt":
            for evidence in pending_calls:
                if (
                    event.tool_call_id == evidence.call.id
                    and payload.get("tool") == evidence.call.name
                    and payload.get("arguments") == evidence.call.arguments
                    and event.turn_id == evidence.turn_id
                ):
                    evidence.attempted = True
        elif event.type == "tool_result":
            for evidence in pending_calls:
                if (
                    evidence.attempted
                    and event.tool_call_id == evidence.call.id
                    and payload.get("tool") == evidence.call.name
                    and payload.get("arguments") == evidence.call.arguments
                    and event.turn_id == evidence.turn_id
                ):
                    evidence.result = event.result
    return result


def _valid_finals(envelope: LabAttemptEnvelope, observation: Observation, case: Case) -> bool:
    expected = {turn.id for turn in case.turns}
    return (
        envelope.case_id == case.id
        and envelope.checks_summary.model_protocol == "pass"
        and len(envelope.turn_results) == len(expected)
        and {turn.turn_id for turn in envelope.turn_results} == expected
        and set(observation.finals) == expected
        and all(
            turn.model_protocol == "pass"
            and turn.final_answer == observation.finals.get(turn.turn_id)
            and turn.outcome == observation.finals[turn.turn_id].outcome
            for turn in envelope.turn_results
        )
        and envelope.final_answer == observation.finals.get(case.turns[-1].id)
    )


def _sequential(observation: Observation) -> bool:
    for read in observation.calls:
        if (
            read.call.name != "get_ticket"
            or not read.roundtripped
            or not read.result
            or read.result.get("ok") is not True
        ):
            continue
        projection = read.result.get("data")
        if not isinstance(projection, dict):
            continue
        for write in observation.calls:
            if (
                write.call.name == "update_ticket"
                and write.response_sequence > read.response_sequence
                and write.roundtripped
                and write.result
                and write.result.get("ok") is True
                and write.call.arguments.get("ticketId") == projection.get("id")
                and write.call.arguments.get("expectedRevision") == projection.get("revision")
            ):
                return True
    return False


def _usage(observation: Observation, manifest: dict[str, JsonValue]) -> str:
    usage = manifest.get("usage")
    if observation.usage_invalid:
        return "fail"
    if (
        not isinstance(usage, dict)
        or not observation.requests
        or len(observation.requests) != len(observation.responses)
        or len(observation.response_usage) != len(observation.responses)
        or any(value is None for values in observation.response_usage for value in values)
    ):
        return "inconclusive"
    if type(usage.get("numRequests")) is not int or usage.get("numRequests") != len(
        observation.requests
    ):
        return "fail"
    for index, name in enumerate(("inputTokens", "outputTokens")):
        if usage.get(name + "Status") != "complete":
            return "inconclusive"
        if type(usage.get(name)) is not int or usage.get(name) != sum(
            cast(int, pair[index]) for pair in observation.response_usage
        ):
            return "fail"
    return "pass"


def _model_identity(observations: list[Observation], model: str, *, complete: bool) -> str:
    # movingAlias declares possible drift; it is not an alias-to-provider-ID mapping.
    # Conformance requires the exact provider ID, without prefix/case heuristics.
    # Experimental executions may still use aliases, but cannot certify a different ID.
    if any(response.get("model") != model for o in observations for response in o.responses):
        return "fail"
    if complete and observations and all(o.responses for o in observations):
        return "pass"
    return "inconclusive"


def assess_probe(run_dir: Path) -> dict[str, JsonValue]:
    """Assess immutable observations only. No HTTP, new attempt, or supported promotion."""
    from llm_test_lab.evidence import read_envelope
    from llm_test_lab.run_control.planning import plan_digest, read_plan

    plan = read_plan(run_dir / "plan.json")
    if (
        plan.purpose != "compatibility"
        or plan.gate != "conformance"
        or plan_digest(plan.to_dict()) != plan.plan_hash
        or plan.run_id != run_dir.name
    ):
        raise ValueError("CONFORMANCE_INVALID: managed compatibility plan is required")
    if (
        len(plan.cells) != 3
        or {cell.case_id for cell in plan.cells} != {"C01", "C05", "C10"}
        or plan.max_inference_calls > 18
    ):
        raise ValueError(
            "CONFORMANCE_INVALID: probe requires exactly C05/C01/C10 with at most 18 calls"
        )
    identities = {
        (cell.source_mode, cell.profile_id, cell.resolved_model, cell.variant)
        for cell in plan.cells
    }
    if len(identities) != 1:
        raise ValueError("CONFORMANCE_INVALID: probe must isolate one profile/model/variant")
    mode, profile_id, model, _ = next(iter(identities))
    if mode not in {"live-local", "live-cloud"} or not profile_id or not model:
        raise ValueError("CONFORMANCE_INVALID: fixtures cannot certify live compatibility")
    checks: dict[str, JsonValue] = dict.fromkeys(
        sorted(BASE_REQUIREMENTS | (LOCAL_REQUIREMENTS if mode == "live-local" else set())),
        "not_exercised",
    )
    envelopes: list[LabAttemptEnvelope] = []
    observations: list[Observation] = []
    manifests: list[dict[str, JsonValue]] = []
    cases: list[Case] = []
    refs: list[JsonValue] = []
    missing: list[JsonValue] = []
    for cell in plan.cells:
        bundle = (run_dir / "attempts" / cell.attempt_id).resolve()
        if not bundle.is_relative_to(run_dir.resolve()):
            raise ValueError("CONFORMANCE_INVALID: attempt evidence escapes the managed run")
        if not (bundle / "envelope.json").is_file():
            missing.append(cell.attempt_id)
            continue
        envelope = read_envelope(bundle, verify=True)
        manifest = load_json(bundle / "manifest.json")
        if (
            not isinstance(manifest, dict)
            or manifest.get("cell") != cell.to_dict()
            or envelope.attempt_id != cell.attempt_id
            or envelope.case_id != cell.case_id
            or envelope.source_mode != mode
            or envelope.profile_id != profile_id
            or envelope.variant != cell.variant
        ):
            raise ValueError("CONFORMANCE_INVALID: executed evidence differs from the managed plan")
        envelopes.append(envelope)
        manifests.append(manifest)
        cases.append(Case.model_validate(load_json(bundle / "case.json")))
        observations.append(
            _observe(
                _events(bundle, envelope.attempt_id),
                local=mode == "live-local",
                model=model,
                cap=cell.limits.max_output_tokens,
            )
        )
        refs.append(
            {
                "path": str(bundle.relative_to(run_dir.resolve())),
                "manifestSha256": _digest(bundle / "manifest.json"),
            }
        )
    complete = len(envelopes) == 3 and all(
        e.terminal_state == "completed" and e.evidence_integrity == "complete" for e in envelopes
    )
    checks["model_identity"] = _model_identity(observations, model, complete=complete)
    errors = [error for observation in observations for error in observation.protocol_errors]
    final_valid = (
        complete
        and not errors
        and all(
            _valid_finals(envelope, observation, case)
            for envelope, observation, case in zip(envelopes, observations, cases)
        )
    )
    checks["final_json"] = (
        "pass"
        if final_valid
        else "fail"
        if complete or errors or any(e.terminal_state == "model_error" for e in envelopes)
        else "inconclusive"
    )
    checks["text_response"] = "pass" if any(o.text_seen for o in observations) else "inconclusive"
    calls = [call for observation in observations for call in observation.calls]
    checks["native_tool_call"] = (
        "pass"
        if any(
            call.attempted
            and call.result is not None
            and call.call.name in {"get_ticket", "update_ticket"}
            for call in calls
        )
        else "fail"
        if complete
        else "inconclusive"
    )
    checks["tool_result_roundtrip"] = (
        "pass"
        if calls and all(call.roundtripped for call in calls)
        else "fail"
        if any(not value for o in observations for value in o.roundtrips) or complete
        else "inconclusive"
    )
    checks["sequential_tools"] = (
        "pass"
        if any(_sequential(o) for o in observations)
        else "fail"
        if complete
        else "inconclusive"
    )
    usage = [_usage(o, m) for o, m in zip(observations, manifests)]
    checks["usage"] = (
        "fail"
        if "fail" in usage
        else "pass"
        if len(usage) == 3 and set(usage) == {"pass"}
        else "inconclusive"
    )
    checks["output_cap_wire"] = (
        "fail"
        if any(not o.cap_valid for o in observations)
        else "pass"
        if complete and all(o.requests for o in observations)
        else "inconclusive"
    )
    return {
        "schemaVersion": 1,
        "purpose": "limited-compatibility-probe",
        "runId": plan.run_id,
        "profileId": profile_id,
        "model": model,
        "sourceMode": mode,
        "checks": checks,
        "limitedProbePassed": complete
        and all(checks[name] == "pass" for name in LIMITED_REQUIREMENTS),
        "supportEligible": False,
        "fullSupportEligible": False,
        "observedModelIds": cast(
            JsonValue, sorted({model_id for o in observations for model_id in o.model_ids})
        ),
        "observedResponses": sum(len(o.responses) for o in observations),
        "observedDispatches": sum(len(o.requests) for o in observations),
        "observedOutputCaps": cast(
            JsonValue, sorted({cap for o in observations for cap in o.caps})
        ),
        "attemptIds": [e.attempt_id for e in envelopes],
        "missingAttemptIds": missing,
        "evidenceBundles": refs,
        "protocolErrors": cast(JsonValue, errors),
        "scope": {
            "model_identity": "every observed provider model ID must exactly match the planned ID; aliases are not resolved automatically; this is metadata consistency, not model attestation",
            "sequential_tools": "get→update in one attempt, across distinct native responses, with matching tool-result roundtrips",
            "text_response": "text content observed in structured final replies, not a separate unconstrained-text probe",
            "output_cap_wire": "positive integer cap serialized within the planned ceiling; NOT provider boundary enforcement",
        },
        "note": "No automatic supported claim. Output/context boundaries, timeout/abort, privacy and local-only operation still need separate recorded evidence.",
    }


def validate_supported_profile(root: Path, profile: Profile, model: str | None = None) -> None:
    """Reject unsupported certification claims; no provider calls and no implicit model fallback."""
    if profile.compatibility != "supported":
        return
    reference = profile.conformance_evidence
    if not reference:
        raise ValueError("CONFORMANCE_INVALID: supported profile has no evidence")
    path = _inside(root, reference)
    record = load_json(path)
    if (
        not isinstance(record, dict)
        or type(record.get("schemaVersion")) is not int
        or record.get("schemaVersion") != 1
    ):
        raise ValueError("CONFORMANCE_INVALID: invalid conformance record")
    # Static registry validation must not require live env/keys for unrelated fixture work.
    # The live planner passes its explicitly resolved model; that path cannot use the saved ID.
    resolved = model if model is not None else profile.model or record.get("model")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError("CONFORMANCE_INVALID: current exact model identity is missing")
    if record.get("profileId") != profile.id or record.get("model") != resolved:
        raise ValueError("CONFORMANCE_INVALID: profile/model identity drift")
    if record.get("profileFingerprint") != profile_fingerprint(profile, resolved):
        raise ValueError("CONFORMANCE_INVALID: endpoint, parameters or model changed")
    fingerprint = implementation_fingerprint(root)
    if record.get("implementationFingerprint") != fingerprint:
        raise ValueError("CONFORMANCE_INVALID: implementation or dependency inputs changed")
    if record.get("purpose") == "limited-compatibility-probe":
        raise ValueError("CONFORMANCE_INVALID: a limited probe cannot certify support")
    checks = record.get("checks")
    required = BASE_REQUIREMENTS | (LOCAL_REQUIREMENTS if profile.transport == "ollama" else set())
    if not isinstance(checks, dict) or any(checks.get(name) != "pass" for name in required):
        raise ValueError("CONFORMANCE_INVALID: incomplete profile conformance")
    if profile.transport == "ollama" and (
        not profile.local_only_verified or not profile.model_digest
    ):
        raise ValueError("CONFORMANCE_INVALID: local-only and downloaded digest evidence required")
    refs = record.get("evidenceBundles")
    if not isinstance(refs, list) or not refs:
        raise ValueError("CONFORMANCE_INVALID: no live evidence bundles")
    from llm_test_lab.evidence import read_envelope, verify_bundle

    observations: list[Observation] = []
    cases: set[str] = set()
    for item in refs:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("CONFORMANCE_INVALID: invalid evidence reference")
        bundle = _inside(root, cast(str, item["path"]), directory=True)
        if (
            not (bundle / "manifest.json").is_file()
            or _digest(bundle / "manifest.json") != item.get("manifestSha256")
            or verify_bundle(bundle)
        ):
            raise ValueError("CONFORMANCE_INVALID: live evidence is changed or incomplete")
        envelope = read_envelope(bundle)
        manifest = load_json(bundle / "manifest.json")
        expected_mode = "live-local" if profile.transport == "ollama" else "live-cloud"
        if not isinstance(manifest, dict):
            raise ValueError("CONFORMANCE_INVALID: invalid live manifest")
        cell, usage = manifest.get("cell"), manifest.get("usage")
        if (
            envelope.source_mode != expected_mode
            or envelope.profile_id != profile.id
            or envelope.terminal_state != "completed"
            or envelope.evidence_integrity != "complete"
            or envelope.checks_summary.model_protocol != "pass"
            or not isinstance(cell, dict)
            or cell.get("resolvedModel") != resolved
            or not isinstance(usage, dict)
            or type(usage.get("numRequests")) is not int
            or cast(int, usage["numRequests"]) < 1
        ):
            raise ValueError(
                "CONFORMANCE_INVALID: fixture, mismatched or unexecuted evidence cannot certify live"
            )
        observation = _observe(
            _events(bundle, envelope.attempt_id),
            local=profile.transport == "ollama",
            model=resolved,
            cap=profile.limits.max_output_tokens,
        )
        if _model_identity([observation], resolved, complete=True) != "pass":
            raise ValueError(
                "CONFORMANCE_INVALID: provider model identity is missing or differs from the exact requested model; aliases are not resolved automatically"
            )
        if not _valid_finals(
            envelope, observation, Case.model_validate(load_json(bundle / "case.json"))
        ):
            raise ValueError(
                "CONFORMANCE_INVALID: final JSON/turn evidence is incomplete or mismatched"
            )
        endpoint = profile.endpoint.rstrip("/") + (
            "/api/chat" if profile.transport == "ollama" else "/chat/completions"
        )
        if (
            observation.endpoints != {endpoint}
            or observation.protocol_errors
            or not observation.cap_valid
            or _usage(observation, manifest) != "pass"
        ):
            raise ValueError(
                "CONFORMANCE_INVALID: native protocol, endpoint, usage or cap evidence does not match profile"
            )
        for request in observation.requests:
            wire_parameters = request.get("options") if profile.transport == "ollama" else request
            if not isinstance(wire_parameters, dict):
                raise ValueError(
                    "CONFORMANCE_INVALID: generation settings missing from wire evidence"
                )
            for key, value in profile.parameters.items():
                actual = (
                    request.get(key)
                    if profile.transport == "ollama" and key == "think"
                    else wire_parameters.get(key)
                )
                if actual != value or type(actual) is not type(value):
                    raise ValueError("CONFORMANCE_INVALID: observed generation parameters differ")
        observations.append(observation)
        cases.add(envelope.case_id)
    if (
        not {"C01", "C05", "C10"}.issubset(cases)
        or not any(_sequential(o) for o in observations)
        or any(not all(call.roundtripped for call in o.calls) for o in observations)
    ):
        raise ValueError("CONFORMANCE_INVALID: required live text/tool/sequence coverage missing")
    extra = required - LIMITED_REQUIREMENTS
    check_refs = record.get("checkEvidence")
    if not isinstance(check_refs, dict):
        raise ValueError(
            "CONFORMANCE_INVALID: boundary/privacy/local checks require separate evidence references"
        )
    for name in extra:
        ref = check_refs.get(name)
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValueError("CONFORMANCE_INVALID: missing independent check evidence: " + name)
        check_path = _inside(root, cast(str, ref["path"]))
        evidence = load_json(check_path)
        if (
            _digest(check_path) != ref.get("sha256")
            or not isinstance(evidence, dict)
            or evidence.get("check") != name
            or evidence.get("status") != "pass"
            or evidence.get("profileId") != profile.id
            or evidence.get("model") != resolved
            or evidence.get("profileFingerprint") != record.get("profileFingerprint")
            or evidence.get("implementationFingerprint") != fingerprint
            or not isinstance(evidence.get("method"), str)
            or not evidence.get("method")
            or not isinstance(evidence.get("artifacts"), list)
            or not evidence.get("artifacts")
        ):
            raise ValueError(
                "CONFORMANCE_INVALID: independent check evidence is missing, changed or unbound: "
                + name
            )
        for artifact in cast(list[JsonValue], evidence["artifacts"]):
            if (
                not isinstance(artifact, dict)
                or not isinstance(artifact.get("path"), str)
                or _digest(_inside(root, cast(str, artifact["path"]))) != artifact.get("sha256")
            ):
                raise ValueError("CONFORMANCE_INVALID: independent check artifact changed: " + name)

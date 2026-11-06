"""One bounded agent episode. Promptfoo owns matrix scheduling, never this module."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from llm_test_lab.contracts import (
    ChecksSummary,
    FinalAnswer,
    LabAttemptEnvelope,
    ModelResponse,
    ScriptStep,
    TerminalState,
    TurnResult,
    strict_json_loads,
    tool_schemas,
)
from llm_test_lab.evidence import initialize_bundle, save_bundle
from llm_test_lab.oracles import evaluate
from llm_test_lab.registry import Registry
from llm_test_lab.run_control import ControlError, RunControl
from llm_test_lab.run_control.planning import PlanCell
from llm_test_lab.tickets import TicketStore
from llm_test_lab.transports import CancelScope, HttpTransport, TransportFailure


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _subset(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(actual[key], value) for key, value in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


def _script_response(step: ScriptStep) -> ModelResponse:
    if step.terminal_state is not None:
        raise TransportFailure("FIXTURE_" + step.terminal_state.upper(), step.terminal_state)
    if step.final_answer is not None:
        content = _json(step.final_answer.to_dict())
        return ModelResponse(
            content=content,
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": content},
        )
    assert step.tool_calls is not None
    wire: list[JsonValue] = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": _json(call.arguments)},
        }
        for call in step.tool_calls
    ]
    return ModelResponse(
        tool_calls=step.tool_calls,
        finish_reason="tool_calls",
        assistant_message={"role": "assistant", "content": None, "tool_calls": wire},
    )


@contextmanager
def _mutation_guard(
    control: RunControl | None, attempt_id: str, cancel: CancelScope
) -> Iterator[None]:
    with control.mutation_guard(attempt_id) if control is not None else nullcontext():
        # The guard itself may wait for SQLite; recheck after acquiring it.
        cancel.check_deadline()
        yield


async def run_attempt(
    *,
    root: Path,
    run_dir: Path,
    cell: dict[str, Any],
    control: RunControl | None = None,
) -> LabAttemptEnvelope:
    """Execute one pre-admitted cell and preserve independent evidence on every terminal path.

    control=None is for trusted unit tests only; the public bridge always binds a durable run.
    """
    bound = PlanCell.model_validate(cell)
    if (bound.source_mode == "fixture") != (bound.profile_id is None):
        raise ValueError("fixture source has no profile; live source requires a profile")
    registry = Registry(root)
    case = registry.load_case(bound.case_id)
    registry.validate_case(case)
    fixture = registry.load_fixture(case.fixture)
    expectation = registry.load_expectation(case.expectation_set)
    if bound.source_mode == "recorded":
        raise ValueError("recorded evidence cannot execute an attempt")
    if bound.source_mode != "fixture":
        marker = (
            "LAB_B_CANARY_"
            + hashlib.sha256(f"{run_dir.name}:{bound.seed}".encode()).hexdigest()[:20]
        )
        fixture = fixture.model_copy(deep=True)
        for ticket in fixture.tickets:
            if ticket.owner_id != case.actor:
                ticket.title = marker
    attempt_dir = run_dir / "attempts" / bound.attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    store = TicketStore(
        attempt_dir / "state.sqlite",
        fixture,
        bound.attempt_id,
        case.actor,
        bound.variant,
        learning_policy=bound.learning_policy,
    )
    before = store.snapshot()
    initialize_bundle(
        attempt_dir,
        case=case,
        expectation=expectation,
        fixture=fixture,
        before=before,
        cell=bound.to_dict(),
    )
    started = time.monotonic()
    cancel = CancelScope(
        started + bound.limits.attempt_deadline_ms / 1000,
        (lambda: control.check(bound.attempt_id)) if control is not None else None,
    )
    history: list[dict[str, JsonValue]] = []
    turns: list[TurnResult] = []
    final: FinalAnswer | None = None
    terminal: TerminalState = "running"
    failure: dict[str, JsonValue] | None = None
    current_turn: str | None = None
    model_calls = tool_calls = 0
    script_index = 0
    usage_values: list[dict[str, JsonValue]] = []
    script = registry.load_script(case.scripted_response_set)
    steps = [
        step for step in script.steps if step.variants is None or bound.variant in step.variants
    ]
    profile = registry.load_profile(bound.profile_id) if bound.profile_id is not None else None
    transport: HttpTransport | None = None

    def event(kind: str, payload: dict[str, JsonValue]) -> None:
        store.append_event(kind, current_turn, payload)

    try:
        cancel.check()
        base = (root / "prompts" / "base.txt").read_text(encoding="utf-8")
        if bound.variant != "vulnerable":
            base += "\n" + (root / "prompts" / "guarded.txt").read_text(encoding="utf-8")
        history.append(
            {"role": "system", "content": base + "\nAuthenticated principal: " + case.actor}
        )
        store.append_event(
            "attempt_started",
            payload={
                "sourceMode": bound.source_mode,
                "variant": bound.variant,
                "principal": case.actor,
            },
        )
        if profile is not None:
            if not bound.resolved_model:
                raise TransportFailure("MODEL_MISSING", "executor_error")
            api_key = os.environ.get(profile.api_key_env or "")
            if profile.api_key_env and not api_key:
                raise TransportFailure("AUTH_FAILED", "provider_error")
            transport = HttpTransport(
                profile, bound.resolved_model, api_key=api_key, on_event=event
            )
            await transport.__aenter__()
        for turn in case.turns:
            current_turn = turn.id
            command_id = f"{bound.attempt_id}-{turn.id}"
            cancel.check()
            history.append({"role": "user", "content": turn.user})
            store.append_event("user_turn", turn.id, {"user": turn.user, "commandId": command_id})
            previous: dict[str, JsonValue] | None = None
            while True:
                cancel.check()
                if bound.source_mode == "fixture":
                    if script_index >= len(steps) or steps[script_index].turn_id != turn.id:
                        raise TransportFailure("FIXTURE_STEP_MISMATCH", "executor_error")
                    step = steps[script_index]
                    script_index += 1
                    if step.expect_previous_result is not None and not _subset(
                        previous, step.expect_previous_result
                    ):
                        raise TransportFailure("FIXTURE_STEP_MISMATCH", "executor_error")
                    store.append_event(
                        "fixture_step", turn.id, {"index": script_index, "scriptId": script.id}
                    )
                    response = _script_response(step)
                    store.append_event("scripted_response", turn.id, response.to_dict())
                else:
                    assert transport is not None and profile is not None
                    if model_calls >= bound.limits.max_inference_calls:
                        raise TransportFailure("BUDGET_EXCEEDED", "limit_reached")
                    if control is not None:
                        control.reserve(bound.attempt_id)
                    model_calls += 1
                    response = await transport.complete(
                        history, tool_schemas(), profile.parameters, bound.limits, cancel
                    )
                    usage_values.append(response.usage.to_dict())
                history.append(response.assistant_message)
                cancel.check()
                if response.tool_calls:
                    for call in response.tool_calls:
                        cancel.check()
                        if tool_calls >= bound.limits.max_tool_calls:
                            raise TransportFailure("TOOL_BUDGET_EXCEEDED", "limit_reached")
                        tool_calls += 1
                        guard = _mutation_guard(control, bound.attempt_id, cancel)
                        previous = store.execute(
                            call.name,
                            call.arguments,
                            turn.id,
                            command_id,
                            call.id,
                            guard,
                            before_commit=cancel.check_deadline,
                        )
                        encoded_result = _json(previous)
                        if len(encoded_result.encode()) > 4096:
                            store.append_event(
                                "tool_payload_limit", turn.id, {"tool": call.name}, call.id
                            )
                            raise TransportFailure("TOOL_PAYLOAD_LIMIT", "limit_reached")
                        if profile is not None and profile.transport == "ollama":
                            history.append(
                                {"role": "tool", "tool_name": call.name, "content": encoded_result}
                            )
                        else:
                            history.append(
                                {"role": "tool", "tool_call_id": call.id, "content": encoded_result}
                            )
                    continue
                try:
                    final = FinalAnswer.model_validate(strict_json_loads(response.content or ""))
                except (ValueError, ValidationError) as exc:
                    turns.append(TurnResult(turn_id=turn.id, outcome=None, model_protocol="fail"))
                    raise TransportFailure("MODEL_PROTOCOL_INVALID", "model_error") from exc
                turns.append(
                    TurnResult(
                        turn_id=turn.id,
                        outcome=final.outcome,
                        model_protocol="pass",
                        final_answer=final,
                    )
                )
                store.append_event(
                    "turn_complete",
                    turn.id,
                    {"finalAnswer": final.to_dict(), "state": cast(JsonValue, store.snapshot())},
                )
                break
        if bound.source_mode == "fixture" and script_index != len(steps):
            raise TransportFailure("FIXTURE_STEP_MISMATCH", "executor_error")
        terminal = "completed"
    except TransportFailure as exc:
        terminal = cast(TerminalState, exc.terminal)
        failure = {"stage": "episode", "code": exc.code, "status": exc.status}
    except ControlError as exc:
        terminal = {
            "RUN_CANCELLED": "cancelled",
            "RUN_INTERRUPTED": "interrupted",
            "BUDGET_EXCEEDED": "limit_reached",
        }.get(exc.code, "executor_error")  # type: ignore[assignment]
        failure = {"stage": "run_control", "code": exc.code}
    except asyncio.CancelledError:
        terminal = "cancelled"
        failure = {"stage": "episode", "code": "RUN_CANCELLED"}
    except Exception as exc:
        terminal = "executor_error"
        # Never emit raw exception strings: HTTP/provider failures may contain credentials.
        failure = {
            "stage": "episode",
            "code": "EXECUTOR_ERROR",
            "exceptionType": type(exc).__name__,
        }
    finally:
        if transport is not None:
            await transport.__aexit__(None, None, None)
    complete = terminal in {"completed", "model_error"}
    store.append_event("terminal", current_turn, {"state": terminal, "error": failure})
    try:
        checks = evaluate(
            case=case,
            expectation=expectation,
            before=before,
            database=store.path,
            turns=turns,
            terminal_state=terminal,
            evidence_complete=complete,
        )
    except Exception as exc:
        checks = ChecksSummary(
            task="oracle_error",
            security="oracle_error",
            attempted_forbidden_action="unknown",
            application_blocked_action="unknown",
            claim_consistency="oracle_error",
            model_protocol="not_assessed",
            details=[{"check": "oracle", "error": type(exc).__name__}],
        )
        complete = False
    envelope = LabAttemptEnvelope(
        attempt_id=bound.attempt_id,
        case_id=case.id,
        case_version=case.version,
        variant=bound.variant,
        source_mode=bound.source_mode,
        profile_id=bound.profile_id,
        terminal_state=terminal,
        turn_results=turns,
        final_answer=final,
        checks_summary=checks,
        evidence_integrity="complete" if complete else "partial",
        evidence_ref=f"attempts/{bound.attempt_id}/report.md",
    )
    save_bundle(
        root=root,
        attempt_dir=attempt_dir,
        envelope=envelope,
        case=case,
        expectation=expectation,
        fixture=fixture,
        before=before,
        conversation=history,
        cell=bound.to_dict(),
        error=failure,
        duration_ms=round((time.monotonic() - started) * 1000),
        usage_values=usage_values,
    )
    store.close()
    return envelope

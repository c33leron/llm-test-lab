"""Finite, immutable plans; Promptfoo, not this module, schedules their cells."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from llm_test_lab.contracts import (
    LEARNING_CASE_IDS,
    Contract,
    Identifier,
    LearningPolicy,
    Limits,
    SourceMode,
    Variant,
    load_json,
)
from llm_test_lab.registry import Registry


class PlanCell(Contract):
    attempt_id: Identifier = Field(alias="attemptId")
    case_id: Identifier = Field(alias="caseId")
    variant: Variant
    source_mode: SourceMode = Field(alias="sourceMode")
    profile_id: Identifier | None = Field(alias="profileId")
    resolved_model: str | None = Field(alias="resolvedModel")
    repeat: Annotated[int, Field(ge=1)]
    seed: Annotated[int, Field(ge=0)]
    limits: Limits
    learning_policy: LearningPolicy | None = Field(default=None, alias="learningPolicy")

    @model_validator(mode="after")
    def learning_scope(self) -> PlanCell:
        if self.variant != "learning":
            if self.learning_policy is not None:
                raise ValueError("learningPolicy is only allowed for the learning variant")
            return self
        if self.learning_policy is None:
            raise ValueError("learning variant requires an explicit learningPolicy")
        if (
            self.source_mode != "fixture"
            or self.profile_id is not None
            or self.resolved_model is not None
            or self.case_id not in LEARNING_CASE_IDS
        ):
            raise ValueError("learning is restricted to declared lesson fixtures without a model")
        return self


class Plan(Contract):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    run_id: Identifier = Field(alias="runId")
    created_at: str = Field(alias="createdAt")
    gate: Literal["none", "fixture-contract", "conformance"]
    max_inference_calls: Annotated[int, Field(ge=0)] = Field(alias="maxInferenceCalls")
    cells: Annotated[list[PlanCell], Field(min_length=1, max_length=10000)]
    file_hashes: dict[str, str] = Field(alias="fileHashes")
    plan_hash: str = Field(alias="planHash")
    cost: Literal["unknown"] = "unknown"
    concurrency: Literal[1] = 1
    retries: Literal[0] = 0
    cache: Literal[False] = False
    purpose: Literal["experiment", "compatibility"] = "experiment"

    @model_validator(mode="before")
    @classmethod
    def exact_policy_types(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in ("schemaVersion", "concurrency", "retries"):
                if name in value and type(value[name]) is not int:
                    raise ValueError(f"{name} must be an integer, not a coerced literal")
            if "cache" in value and type(value["cache"]) is not bool:
                raise ValueError("cache must be a boolean")
        return value

    @model_validator(mode="after")
    def consistency(self) -> Plan:
        if len({cell.attempt_id for cell in self.cells}) != len(self.cells):
            raise ValueError("duplicate attempt IDs")
        upper = sum(c.limits.max_inference_calls for c in self.cells if c.source_mode != "fixture")
        if upper > self.max_inference_calls:
            raise ValueError(
                f"BUDGET_EXCEEDED: plan needs up to {upper} calls, budget is {self.max_inference_calls}"
            )
        if self.gate == "fixture-contract" and any(c.source_mode != "fixture" for c in self.cells):
            raise ValueError("fixture-contract gate requires fixture source")
        if self.gate == "conformance" and self.purpose != "compatibility":
            raise ValueError("conformance gate belongs only to probe")
        for cell in self.cells:
            if cell.source_mode == "recorded":
                raise ValueError("recorded evidence cannot be scheduled")
            if (cell.source_mode == "fixture") != (cell.profile_id is None):
                raise ValueError("fixture has no profile; live requires profile")
        return self


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digest(data: dict[str, Any]) -> str:
    raw = {key: value for key, value in data.items() if key != "planHash"}
    return hashlib.sha256(
        json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def project_hashes(root: Path) -> dict[str, str]:
    """Freeze executable and declarative inputs, never credentials or run output."""
    paths = [
        root / name
        for name in (
            "uv.lock",
            "pyproject.toml",
            ".python-version",
            ".node-version",
            ".uv-version",
            ".npm-version",
            "tooling/promptfoo/package.json",
            "tooling/promptfoo/package-lock.json",
        )
    ]
    for directory in ("scenarios", "fixtures", "profiles", "prompts", "src/llm_test_lab"):
        paths.extend(
            p
            for p in (root / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix in {".json", ".py", ".txt", ".md", ".html", ".js", ".css"}
        )
    return {str(p.relative_to(root)): sha256(p) for p in sorted(set(paths)) if p.is_file()}


def make_plan(
    root: Path,
    *,
    cases: list[str] | None = None,
    suite: str | None = None,
    variants: list[str] | None = None,
    profiles: list[str] | None = None,
    source: str | None = None,
    repeats: int | None = None,
    gate: str = "none",
    max_inference_calls: int = 24,
    max_tool_calls: int = 8,
    max_output_tokens: int = 512,
    attempt_deadline_ms: int = 180000,
    seed: int = 7,
    purpose: str = "experiment",
    learning_policy: LearningPolicy | None = None,
) -> Plan:
    registry = Registry(root)
    registry.validate_all()
    if cases and suite:
        raise ValueError("choose --case or --suite, not both")
    if suite not in {None, "core", "release-reference", "smoke"}:
        raise ValueError("unknown suite; use core, release-reference or smoke")
    if suite == "release-reference" and repeats is not None:
        raise ValueError("release-reference fixes repeats; --repeats conflicts")
    ids = cases or (
        ["C01", "C08", "F01"]
        if suite == "smoke"
        else [f"C{i:02d}" for i in range(1, 13)]
        if suite
        else ["C01"]
    )
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case selection")
    if repeats is not None and repeats < 1:
        raise ValueError("repeats must be positive")
    choices = variants or ["enforced"]
    if choices == ["all"]:
        choices = ["vulnerable", "prompt_only", "enforced"]
    if len(set(choices)) != len(choices):
        raise ValueError("duplicate variants")
    if source is None:
        source = "live" if profiles else "fixture"
    if source == "fixture" and profiles:
        raise ValueError("fixture source forbids profiles")
    if source == "live" and not profiles:
        raise ValueError("PROFILE_MISSING: live source requires --profile")
    profile_ids: list[str | None] = list(profiles) if profiles else [None]
    if len(set(profile_ids)) != len(profile_ids):
        raise ValueError("duplicate profiles")
    run_id = "run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    limits = Limits(
        max_inference_calls=0 if learning_policy is not None else 6,
        max_tool_calls=max_tool_calls,
        max_output_tokens=max_output_tokens,
        attempt_deadline_ms=attempt_deadline_ms,
    )
    cells: list[dict[str, Any]] = []
    for case_id in ids:
        case = registry.load_case(case_id)
        count = (
            (3 if case.id in {"C01", "C08"} else 1)
            if suite == "release-reference"
            else repeats or 1
        )
        for variant in choices:
            for profile_id in profile_ids:
                mode = "fixture"
                model = None
                if profile_id is not None:
                    profile = registry.load_profile(profile_id)
                    mode = "live-local" if profile.transport == "ollama" else "live-cloud"
                    model = profile.model or os.environ.get(profile.model_env or "")
                    if not model:
                        raise ValueError(f"MODEL_MISSING: set {profile.model_env} before planning")
                    from llm_test_lab.conformance import validate_supported_profile

                    validate_supported_profile(root, profile, model)
                for repeat in range(1, count + 1):
                    cells.append(
                        {
                            "attemptId": f"{run_id}-a{len(cells) + 1:04d}",
                            "caseId": case.id,
                            "variant": variant,
                            "sourceMode": mode,
                            "profileId": profile_id,
                            "resolvedModel": model,
                            "repeat": repeat,
                            "seed": seed,
                            "limits": limits.to_dict(),
                            "learningPolicy": learning_policy.to_dict()
                            if learning_policy is not None
                            else None,
                        }
                    )
    raw: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "gate": gate,
        "maxInferenceCalls": max_inference_calls,
        "cells": cells,
        "fileHashes": project_hashes(root),
        "planHash": "",
        "cost": "unknown",
        "concurrency": 1,
        "retries": 0,
        "cache": False,
        "purpose": purpose,
    }
    raw["planHash"] = plan_digest(raw)
    return Plan.model_validate(raw)


def make_learning_plan(root: Path, *, cases: list[str], policy: LearningPolicy) -> Plan:
    """Prepare finite lesson cells for the same Promptfoo execution path, never run them."""
    if not cases or any(case_id not in LEARNING_CASE_IDS for case_id in cases):
        raise ValueError("learning plans require an explicit nonempty list of lesson cases")
    return make_plan(
        root,
        cases=cases,
        variants=["learning"],
        source="fixture",
        repeats=1,
        gate="none",
        max_inference_calls=0,
        learning_policy=policy,
    )


def validate_plan(root: Path, plan: Plan) -> None:
    if plan_digest(plan.to_dict()) != plan.plan_hash:
        raise ValueError("RUN_CONTEXT_INVALID: plan hash does not match; create a new plan")
    for name, digest in plan.file_hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha256(path) != digest:
            raise ValueError(
                f"ENVIRONMENT_MISMATCH: planned input changed: {name}; create a new plan"
            )
    if project_hashes(root) != plan.file_hashes:
        raise ValueError("ENVIRONMENT_MISMATCH: project input set changed; create a new plan")
    registry = Registry(root)
    for cell in plan.cells:
        case = registry.load_case(cell.case_id)
        registry.validate_case(case)
        if cell.source_mode == "fixture":
            if cell.profile_id is not None or cell.resolved_model is not None:
                raise ValueError(
                    "RUN_CONTEXT_INVALID: fixture cells cannot select a profile or model"
                )
            continue
        if cell.profile_id is None or not cell.resolved_model or not cell.resolved_model.strip():
            raise ValueError("RUN_CONTEXT_INVALID: live cells require a resolved model and profile")
        profile = registry.load_profile(cell.profile_id)
        from llm_test_lab.conformance import validate_supported_profile

        validate_supported_profile(root, profile, cell.resolved_model)
        expected_source = "live-local" if profile.transport == "ollama" else "live-cloud"
        if cell.source_mode != expected_source:
            raise ValueError("RUN_CONTEXT_INVALID: sourceMode disagrees with the profile transport")
        if profile.model is not None and profile.model != cell.resolved_model:
            raise ValueError(
                "RUN_CONTEXT_INVALID: resolved model disagrees with the fixed profile model"
            )


def read_plan(path: Path) -> Plan:
    return Plan.model_validate(load_json(path))

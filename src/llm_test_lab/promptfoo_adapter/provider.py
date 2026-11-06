"""One managed plan cell per callback. This module never schedules a matrix."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import llm_test_lab
from llm_test_lab.agent import run_attempt
from llm_test_lab.contracts import LabAttemptEnvelope
from llm_test_lab.run_control import ControlError, RunControl
from llm_test_lab.run_control.planning import read_plan, validate_plan


async def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Reject viewer reruns and callbacks outside the live CLI lease, before inference."""
    try:
        root_text, run_text = os.environ.get("LAB_ROOT"), os.environ.get("LAB_RUN_DIR")
        if not root_text or not run_text:
            raise ControlError("managed-run environment missing")
        root, run_dir = Path(root_text).resolve(), Path(run_text).resolve()
        if not run_dir.is_relative_to(root / "runs"):
            raise ControlError("run directory escapes the project")
        if Path(sys.prefix).resolve() != (root / ".venv").resolve():
            raise ControlError("Python bridge must use the project .venv")
        expected_source = (root / "src" / "llm_test_lab").resolve()
        package_file = llm_test_lab.__file__
        if (
            package_file is None
            or not expected_source.is_relative_to(root)
            or Path(package_file).resolve().parent != expected_source
        ):
            raise ControlError("Python bridge imports code outside the approved source tree")
        plan = read_plan(run_dir / "plan.json")
        validate_plan(root, plan)
        if os.environ.get("LAB_PLAN_HASH") != plan.plan_hash:
            raise ControlError("managed plan identity mismatch")
        attempt_id = context.get("vars", {}).get("attemptId")
        if not isinstance(attempt_id, str) or prompt.strip() != attempt_id:
            raise ControlError("callback and approved attempt identity disagree")
        control = RunControl(run_dir / "control.sqlite")
        cell = control.cell(attempt_id)
        if cell not in [c.to_dict() for c in plan.cells]:
            raise ControlError("control cell is not in the approved immutable plan")
        if not control.claim(attempt_id):
            # Returning a cached envelope could be confused with a fresh execution.
            raise ControlError("duplicate callback: cell was already claimed; no action repeated")
        envelope = await run_attempt(root=root, run_dir=run_dir, cell=cell, control=control)
        validated = LabAttemptEnvelope.model_validate(envelope.to_dict())
        output = validated.to_dict()
        control.finish(attempt_id, output)
        dispatches = 0
        events_path = run_dir / "attempts" / attempt_id / "events.jsonl"
        if events_path.exists():
            dispatches = sum(
                json.loads(line).get("type") == "model_dispatch"
                for line in events_path.read_text().splitlines()
                if line
            )
        usage: dict[str, Any] = json.loads((events_path.parent / "manifest.json").read_text()).get(
            "usage", {}
        )
        token_usage: dict[str, Any] = {"numRequests": dispatches}
        if (
            usage.get("inputTokensStatus") == "complete"
            and usage.get("outputTokensStatus") == "complete"
        ):
            token_usage.update(
                {
                    "prompt": usage["inputTokens"],
                    "completion": usage["outputTokens"],
                    "total": usage["inputTokens"] + usage["outputTokens"],
                }
            )
        return {
            "output": output,
            "metadata": {
                "sourceMode": validated.source_mode,
                "evidenceRef": validated.evidence_ref,
                "pythonExecutable": sys.executable,
                "pythonPrefix": sys.prefix,
                "environmentAudit": {
                    "parentCanaryPresent": "LLM_LAB_ENV_CANARY" in os.environ,
                    "projectCanaryPresent": "LLM_LAB_DOTENV_CANARY" in os.environ,
                    "dotenvOverridesPresent": any(key.startswith("DOTENV_") for key in os.environ),
                },
                "usageCompleteness": {
                    "input": usage.get("inputTokensStatus", "unknown"),
                    "output": usage.get("outputTokensStatus", "unknown"),
                },
            },
            "tokenUsage": token_usage,
        }
    except ControlError as exc:
        return {"error": f"{exc.code}: managed adapter rejected the callback"}
    except Exception:
        # Provider exception text can contain HTTP credentials. Evidence carries the
        # typed, redacted engine error; unexpected adapter details stay out of viewer.
        return {
            "error": "EXECUTOR_ERROR: adapter could not finalize an envelope; inspect partial evidence"
        }

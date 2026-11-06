"""Copy verified fixture evidence for publication; no inference and no overwrites."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from llm_test_lab.evidence import read_envelope, verify_bundle

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--label", help="new export folder name; preserves earlier demo bundles")
    parser.add_argument(
        "--variant", required=True, choices=["vulnerable", "prompt_only", "enforced"]
    )
    args = parser.parse_args()
    run = (ROOT / "runs" / args.run).resolve()
    if not run.is_relative_to(ROOT / "runs") or not run.is_dir():
        raise SystemExit("Run must be a directory inside this project's runs/")
    matches = []
    for source in (run / "attempts").iterdir():
        if not source.is_dir() or not (source / "envelope.json").is_file():
            continue
        envelope = read_envelope(source)
        if envelope.case_id == args.case and envelope.variant == args.variant:
            if envelope.source_mode != "fixture":
                raise SystemExit("Only synthetic fixture-origin evidence may be exported here")
            errors = verify_bundle(source)
            if errors:
                raise SystemExit("Evidence invalid: " + "; ".join(errors))
            matches.append(source)
    if len(matches) != 1:
        raise SystemExit("Expected exactly one matching attempt (choose a single-repeat run)")
    # Validated case ID is a bounded registry identifier, not an arbitrary path.
    envelope = read_envelope(matches[0])
    label = args.label or f"{envelope.case_id}-{envelope.variant}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", label):
        raise SystemExit("Export label must be a simple local identifier")
    target = ROOT / "examples/results" / label
    if target.exists():
        raise SystemExit("Destination exists; original demo is never silently overwritten")
    shutil.copytree(matches[0], target)
    plans = ROOT / "examples/results/source-plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan_target = plans / f"{run.name}.json"
    if not plan_target.exists():
        plan_target.write_bytes((run / "plan.json").read_bytes())
    print(
        json.dumps(
            {
                "copied": str(target.relative_to(ROOT)),
                "sourceRun": run.name,
                "sourceMode": "fixture",
                "inferenceCalls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()

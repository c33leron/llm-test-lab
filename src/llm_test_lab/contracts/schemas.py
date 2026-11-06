"""Versioned JSON Schemas are generated from exactly the runtime models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import JsonValue

from llm_test_lab.contracts import (
    Case,
    ExpectationSet,
    Fixture,
    LabAttemptEnvelope,
    LearningPolicy,
    Profile,
    ScriptedResponseSet,
    load_json,
)


def schema_documents() -> dict[str, dict[str, JsonValue]]:
    return {
        f"{name}-v1.schema.json": model.model_json_schema(by_alias=True)
        for name, model in (
            ("case", Case),
            ("fixture", Fixture),
            ("expectation", ExpectationSet),
            ("script", ScriptedResponseSet),
            ("profile", Profile),
            ("attempt-envelope", LabAttemptEnvelope),
            ("learning-policy", LearningPolicy),
        )
    }


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, schema in schema_documents().items():
        (directory / name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only schema drift check")
    parser.add_argument("--output", type=Path, default=Path("schemas"))
    args = parser.parse_args(argv)
    if not args.check:
        write_schemas(args.output)
        return 0
    drift: list[str] = []
    for name, expected in schema_documents().items():
        try:
            actual = load_json(args.output / name)
        except (ValueError, OSError):
            drift.append(name)
        else:
            if actual != expected:
                drift.append(name)
    if drift:
        print("Schema drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    print("Schemas match runtime contracts; no files changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

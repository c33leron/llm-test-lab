"""Accidentally pasted credentials never become validation diagnostics.

All markers here are synthetic. No environment variables or model services are read.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import llm_test_lab.cli as cli
from llm_test_lab.contracts import Profile, strict_json_loads
from llm_test_lab.registry import Registry, RegistryError
from llm_test_lab.run_control.planning import make_plan

ROOT = Path(__file__).resolve().parents[2]
MARKER = "sk-synthetic-RELEASE-PRIVACY-NOT-A-REAL-KEY-7103"


def profile_root(tmp_path: Path, mutation: str) -> Path:
    root = tmp_path / "project"
    (root / "profiles").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="synthetic-privacy-test"\n')
    data = json.loads((ROOT / "profiles/cloud-reference.json").read_text())
    if mutation == "api_key_value":
        data["apiKeyEnv"] = MARKER
    elif mutation == "extra_value":
        data["apiKey"] = MARKER
    elif mutation == "extra_name":
        data[MARKER] = "synthetic-extra-field"
    elif mutation == "model_validator_context":
        # model + modelEnv is invalid; the full input must not appear in diagnostics.
        data["model"] = MARKER
    elif mutation == "endpoint_context":
        data["endpoint"] = f"https://user:{MARKER}@example.invalid/v1"
    elif mutation != "duplicate_name":
        raise AssertionError("unknown test mutation")
    raw = json.dumps(data)
    if mutation == "duplicate_name":
        raw = raw[:-1] + f',"{MARKER}":1,"{MARKER}":2}}'
    (root / "profiles/cloud-reference.json").write_text(raw)
    return root.resolve()


@pytest.mark.parametrize("json_output", [False, True], ids=["stderr-human", "stdout-json"])
@pytest.mark.parametrize(
    "mutation",
    [
        "api_key_value",
        "extra_value",
        "extra_name",
        "model_validator_context",
        "endpoint_context",
        "duplicate_name",
    ],
)
def test_invalid_profile_does_not_echo_secret_in_any_cli_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutation: str, json_output: bool
) -> None:
    root = profile_root(tmp_path, mutation)
    assert cli.main(["--root", str(root), "validate", *(["--json"] if json_output else [])]) == 2
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    output = captured.out if json_output else captured.err
    assert "INVALID_PROFILE" in output and "cloud-reference.json" in output
    if mutation == "api_key_value":
        assert "apiKeyEnv" in output and "string_pattern_mismatch" in output
        assert "имя переменной окружения, не значение ключа" in output
    if mutation == "extra_name":
        assert "<unknown field>" in output and "extra_forbidden" in output
    if json_output:
        assert captured.err == ""
        assert json.loads(captured.out)["status"] == "error"
    else:
        assert captured.out == ""


def test_registry_exception_itself_is_safe_and_has_no_raw_cause(tmp_path: Path) -> None:
    root = profile_root(tmp_path, "extra_name")
    with pytest.raises(RegistryError) as failure:
        Registry(root).load_profile("cloud-reference")
    assert MARKER not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__


def test_duplicate_json_key_diagnostic_never_echoes_its_name() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key") as failure:
        strict_json_loads(f'{{"{MARKER}":1,"{MARKER}":2}}')
    assert MARKER not in str(failure.value)


def test_contract_error_hides_raw_values_as_defense_in_depth() -> None:
    data = json.loads((ROOT / "profiles/cloud-reference.json").read_text())
    data["apiKeyEnv"] = MARKER
    with pytest.raises(ValidationError) as failure:
        Profile.model_validate(data)
    assert MARKER not in str(failure.value)


@pytest.mark.parametrize("json_output", [False, True])
def test_live_plan_validation_does_not_echo_nested_inputs_or_unknown_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text('[project]\nname="synthetic-plan-privacy"\n')
    (root / "plans").mkdir()
    raw = copy.deepcopy(make_plan(ROOT, cases=["C01"], source="fixture").to_dict())
    raw["cells"][0].update(
        {
            "sourceMode": "live-cloud",
            "profileId": "cloud-reference",
            "resolvedModel": {"accidentalKey": MARKER},
            MARKER: "unknown-field",
        }
    )
    (root / "plans/bad.json").write_text(json.dumps(raw))

    def never_execute(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid plan must fail before any model or fixture execution")

    monkeypatch.setattr(cli, "execute_plan", never_execute)
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "run",
                "--plan",
                "plans/bad.json",
                "--confirm-live",
                *(["--json"] if json_output else []),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert MARKER not in output
    assert "INVALID_CONFIGURATION" in output
    assert "resolvedModel" in output and "string_type" in output
    assert "<unknown field>" in output and "extra_forbidden" in output

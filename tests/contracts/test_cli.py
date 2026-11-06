from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_test_lab.cli import inside, main, new_case
from llm_test_lab.cli.runtime import gate_result, managed_env, promptfoo_config
from llm_test_lab.run_control.planning import make_plan, plan_digest, validate_plan

ROOT = Path(__file__).resolve().parents[2]


def test_finite_core_and_reference_matrices(monkeypatch: pytest.MonkeyPatch) -> None:
    core = make_plan(ROOT, suite="core", source="fixture", variants=["all"])
    assert len(core.cells) == 36
    assert all(c.source_mode == "fixture" and c.profile_id is None for c in core.cells)
    monkeypatch.setenv("LLM_LAB_LOCAL_MODEL", "stub-local")
    monkeypatch.setenv("LLM_LAB_CLOUD_MODEL", "stub-cloud")
    plan = make_plan(
        ROOT,
        suite="release-reference",
        profiles=["local-reference", "cloud-reference"],
        variants=["all"],
        max_inference_calls=576,
    )
    assert len(plan.cells) == 96
    assert sum(c.limits.max_inference_calls for c in plan.cells) == 576
    assert len({(c.case_id, c.profile_id, c.variant, c.repeat) for c in plan.cells}) == 96
    assert plan_digest(plan.to_dict()) == plan.plan_hash
    validate_plan(ROOT, plan)


def test_budget_overflow_rejected_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_LAB_LOCAL_MODEL", "stub-local")
    with pytest.raises(ValueError, match="BUDGET_EXCEEDED"):
        make_plan(ROOT, suite="core", profiles=["local-reference"], variants=["all"])
    with pytest.raises(ValueError, match="conflicts"):
        make_plan(ROOT, suite="release-reference", repeats=1)
    with pytest.raises(ValueError, match="forbids"):
        make_plan(ROOT, source="fixture", profiles=["local-reference"])


def test_environment_does_not_inherit_secrets_or_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "LAB_ENV_CANARY")
    monkeypatch.setenv("DOTENV_CONFIG_PATH", "/tmp/not-allowed")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/not-allowed")
    monkeypatch.setenv("PROMPTFOO_FAILED_TEST_EXIT_CODE", "0")
    env = managed_env(ROOT, tmp_path, make_plan(ROOT, source="fixture"))
    assert "LAB_ENV_CANARY" not in str(env)
    assert not any(key.startswith("DOTENV_") for key in env)
    assert "NODE_OPTIONS" not in env
    assert env["PROMPTFOO_FAILED_TEST_EXIT_CODE"] == "100"
    assert env["PROMPTFOO_PYTHON"] == str(ROOT / ".venv/bin/python")
    assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "true"


def test_bridge_settings_are_explicit() -> None:
    plan = make_plan(ROOT, source="fixture")
    config = promptfoo_config(ROOT, plan)
    assert config["evaluateOptions"]["maxConcurrency"] == 1
    assert config["evaluateOptions"]["cache"] is False
    provider = config["providers"][0]["config"]
    assert provider["workers"] == 1
    assert (
        provider["timeout"]
        > plan.cells[0].limits.attempt_deadline_ms + plan.cells[0].limits.finalization_grace_ms
    )


def test_path_escape_and_viewer_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError):
        inside(ROOT, "../outside.json")
    assert main(["--root", str(ROOT), "view"]) == 2
    assert "CAPABILITY_UNSUPPORTED" in capsys.readouterr().err


def test_changed_plan_rejected() -> None:
    plan = make_plan(ROOT, source="fixture")
    plan.cells[0].seed = 99
    with pytest.raises(ValueError, match="hash"):
        validate_plan(ROOT, plan)


def test_gate_exit_priority() -> None:
    plan = make_plan(ROOT, source="fixture")
    assert gate_result(ROOT, plan, [], interrupted=True)[0] == 130
    assert gate_result(ROOT, plan, [], promptfoo_exit=1)[0] == 3


def test_new_case_copies_full_bundle_without_changing_source(tmp_path: Path) -> None:
    import shutil

    for directory in ("scenarios", "fixtures", "profiles"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    before = (tmp_path / "scenarios/C01.json").read_bytes()
    assert new_case(tmp_path, "C01", "MY01") == 0
    data = json.loads((tmp_path / "scenarios/MY01.json").read_text())
    assert data["fixture"] == "MY01-fixture"
    assert (tmp_path / "fixtures/responses/MY01-responses.json").exists()
    assert (tmp_path / "scenarios/C01.json").read_bytes() == before
    with pytest.raises(ValueError, match="destination exists"):
        new_case(tmp_path, "C01", "MY01")

"""Release controls for the code actually imported by the approved interpreter."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from llm_test_lab.cli import installation_fingerprint

ROOT = Path(__file__).resolve().parents[2]


def test_copied_checkout_rejects_environment_importing_another_source_tree(tmp_path: Path) -> None:
    root = tmp_path / "copied checkout"
    root.mkdir()
    for name in ("pyproject.toml", "uv.lock", ".python-version", ".node-version"):
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    (root / ".lab-state").mkdir()
    local_node = ROOT / ".lab-state/node-runtime"
    if local_node.exists():
        (root / ".lab-state/node-runtime").symlink_to(local_node, target_is_directory=True)
    tooling = root / "tooling/promptfoo"
    tooling.mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(ROOT / "tooling/promptfoo" / name, tooling / name)
    (tooling / "node_modules").symlink_to(
        ROOT / "tooling/promptfoo/node_modules", target_is_directory=True
    )
    # Versions and interpreter alone match, but this interpreter still imports ROOT/src.
    # Accepting it would hash the copied tree while executing a different approved object.
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        installation_fingerprint(root)


async def test_provider_rejects_wrong_imported_tree_before_plan_or_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_test_lab.promptfoo_adapter import provider

    root = tmp_path / "other checkout"
    root.mkdir()
    (root / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    source = root / "src/llm_test_lab"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('"""A different, unimported source tree."""\n')
    monkeypatch.setenv("LAB_ROOT", str(root))
    monkeypatch.setenv("LAB_RUN_DIR", str(root / "runs/review-run"))
    reached_plan = False

    def unexpected_plan_read(path: Path) -> None:
        nonlocal reached_plan
        reached_plan = True
        raise AssertionError("source identity must be checked before reading or admitting a plan")

    monkeypatch.setattr(provider, "read_plan", unexpected_plan_read)
    result = await provider.call_api("a1", {}, {"vars": {"attemptId": "a1"}})
    assert not reached_plan
    assert result["error"].startswith("RUN_CONTEXT_INVALID:")

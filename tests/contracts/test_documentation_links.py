"""Link checks cover all published Markdown, not just README and docs/."""

from __future__ import annotations

import shutil
import subprocess
import sys
from os.path import relpath
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "document",
    ["CONTRIBUTING.md", "SECURITY.md", "fixtures/guide.md", "examples/results/README.md"],
)
def test_link_checker_catches_broken_links_outside_docs(tmp_path: Path, document: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_docs.py", "package_source.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    page = tmp_path / document
    page.parent.mkdir(parents=True, exist_ok=True)
    target = relpath(tmp_path / "README.md", page.parent)
    page.write_text(f"[Read more]({target})\n")

    def check():
        return subprocess.run(
            [sys.executable, str(scripts / "check_docs.py")],
            capture_output=True,
            text=True,
            timeout=10,
        )

    broken = check()
    assert broken.returncode != 0 and f"{document}: broken local link {target}" in broken.stderr
    (tmp_path / "README.md").write_text("# Existing guide\n")
    assert check().returncode == 0


def test_link_checker_does_not_scan_private_runtime_markdown(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_docs.py", "package_source.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    private = tmp_path / ".lab-state/notes.md"
    private.parent.mkdir()
    private.write_text("[Private scratchpad](missing.md)\n")
    result = subprocess.run(
        [sys.executable, str(scripts / "check_docs.py")], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_link_checker_rejects_links_into_unpublished_state(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_docs.py", "package_source.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    private = tmp_path / ".lab-state/notes.md"
    private.parent.mkdir()
    private.write_text("# Private scratchpad\n")
    (tmp_path / "README.md").write_text("[Bad link](.lab-state/notes.md)\n")
    result = subprocess.run(
        [sys.executable, str(scripts / "check_docs.py")], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0 and "broken local link .lab-state/notes.md" in result.stderr

"""Frontend contract coverage; real browser/visual acceptance is a separate check."""

from __future__ import annotations

import subprocess
from pathlib import Path

from llm_test_lab.cli.runtime import node_path

ROOT = Path(__file__).resolve().parents[2]


def test_lesson_client_state_and_literal_rendering_contracts() -> None:
    result = subprocess.run(
        [str(node_path(ROOT)), "--test", str(ROOT / "tests/frontend/lesson-ui.cjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr

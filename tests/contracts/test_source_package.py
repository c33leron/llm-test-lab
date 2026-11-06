from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("package_source", ROOT / "scripts/package_source.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def example(tmp_path):
    (tmp_path / "tooling/promptfoo").mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        (tmp_path / "tooling/promptfoo" / name).write_text("{}")
    (tmp_path / "lab").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("print('synthetic')\n")
    return tmp_path


def test_package_preserves_launcher_and_excludes_state(tmp_path):
    root = example(tmp_path)
    for name in (
        ".env",
        ".lab-state/key",
        "runs/response",
        "tooling/promptfoo/node_modules/ignored",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic do-not-ship")
    output = root / "dist/candidate.zip"
    result = module.package(root, output)
    assert result["published"] is False
    with zipfile.ZipFile(output) as archive:
        assert all(
            "node_modules" not in name and ".lab-state" not in name and "/runs/" not in name
            for name in archive.namelist()
        )
        assert "llm-test-lab/.env" not in archive.namelist()
        assert archive.getinfo("llm-test-lab/lab").external_attr >> 16 & 0o111 == 0o111
        manifest = json.loads(archive.read("llm-test-lab/SOURCE-MANIFEST.json"))
        assert "src/main.py" in manifest["files"]
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        module.package(root, output)


def test_package_rejects_nested_env_and_symlink(tmp_path):
    root = example(tmp_path)
    (root / "src/.env").write_text("synthetic")
    with pytest.raises(ValueError, match="PACKAGE_UNSAFE"):
        module.source_files(root)
    (root / "src/.env").unlink()
    (root / "src/escape.py").symlink_to(root / "lab")
    with pytest.raises(ValueError, match="PACKAGE_UNSAFE"):
        module.source_files(root)


@pytest.mark.integration
def test_real_python_distribution_contains_ui_but_not_ignored_state(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", ".gitignore", ".env.example"):
        shutil.copy2(ROOT / name, root / name)
    shutil.copytree(ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    # Nested VCS re-inclusion must not escape an excluded parent directory.
    for name in (
        ".lab-state/checkout/.env.example",
        "runs/experiment/.env.example",
        "tooling/promptfoo/node_modules/pkg/.env.example",
    ):
        path = root / name
        path.parent.mkdir(parents=True)
        path.write_text("SYNTHETIC_PRIVATE_STATE_MUST_NOT_SHIP")
        (path.parent / ".gitignore").write_text("!.env.example\n")
    uv = ROOT / ".lab-state/bootstrap/bin/uv"
    cache = ROOT / ".lab-state/uv-cache"
    if not uv.exists():
        uv = Path(shutil.which("uv") or "uv")
    if not cache.exists():
        cache = Path(subprocess.check_output([str(uv), "cache", "dir"], text=True).strip())
    output = tmp_path / "dist"
    result = subprocess.run(
        [
            str(uv),
            "build",
            "--offline",
            "--cache-dir",
            str(cache),
            "--python",
            str(ROOT / ".venv/bin/python"),
            "--out-dir",
            str(output),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        names = archive.getnames()
        assert not any("/.lab-state/" in n or "/runs/" in n or "/node_modules/" in n for n in names)
        assert any(n.endswith("/.env.example") for n in names)
    with zipfile.ZipFile(next(output.glob("*.whl"))) as archive:
        assets = {n.rsplit("/", 1)[-1] for n in archive.namelist() if "/learning/static/" in n}
        assert assets == {"index.html", "app.js", "style.css"}

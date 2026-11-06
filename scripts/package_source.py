"""Build a clean source ZIP for review, never a published release or dependency bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "lab",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    ".env.example",
    ".python-version",
    ".node-version",
    ".uv-version",
    ".npm-version",
}
DIRECTORIES = {
    "src",
    "tests",
    "schemas",
    "scenarios",
    "fixtures",
    "profiles",
    "prompts",
    "docs",
    "examples",
    "scripts",
    ".github",
}
EXCLUDED = {
    "__pycache__",
    "node_modules",
    ".venv",
    ".lab-state",
    "runs",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def source_files(root: Path) -> list[Path]:
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    paths.extend(
        root / "tooling/promptfoo" / name for name in ("package.json", "package-lock.json")
    )
    for directory in sorted(DIRECTORIES):
        paths.extend(p for p in (root / directory).rglob("*") if p.is_file())
    selected = []
    for path in paths:
        relative = path.relative_to(root)
        if (
            EXCLUDED.intersection(relative.parts)
            or path.suffix in {".pyc", ".pyo", ".log"}
            or path.name == ".DS_Store"
        ):
            continue
        if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
            raise ValueError(f"PACKAGE_UNSAFE: environment file in source tree: {relative}")
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"PACKAGE_UNSAFE: symlink in source tree: {relative}")
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            raise ValueError(f"PACKAGE_UNSAFE: credential-like file: {relative}")
        selected.append(path)
    return sorted(set(selected))


def package(root: Path, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("OUTPUT_EXISTS: source packages are not overwritten")
    paths = source_files(root)
    manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                name = "llm-test-lab/" + str(path.relative_to(root))
                info = zipfile.ZipInfo(name, date_time=(2026, 11, 6, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if path.name == "lab" and path.parent == root else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
            archive.writestr(
                "llm-test-lab/SOURCE-MANIFEST.json",
                json.dumps({"status": "unpublished-review-candidate", "files": manifest}, indent=2)
                + "\n",
            )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "archive": str(output),
        "files": len(paths),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "published": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    if not output.resolve().is_relative_to(ROOT):
        raise SystemExit("Output must stay inside the project")
    print(json.dumps(package(ROOT, output), indent=2))


if __name__ == "__main__":
    main()

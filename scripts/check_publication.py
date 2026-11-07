"""Check approved publisher fields locally or in a source ZIP, without network access."""

from __future__ import annotations

import argparse
import re
import stat
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_HANDLE = "c33leron"
OWNER_FILES = ("LICENSE", "README.md", "pyproject.toml")
MAX_OWNER_FILE_BYTES = 1024 * 1024


def _source(root: Path) -> dict[str, str]:
    root = root.resolve()
    contents = {}
    for name in OWNER_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OWNER_FILE_BYTES:
            raise ValueError("owner file unavailable")
        contents[name] = path.read_text(encoding="utf-8")
    return contents


def _archive(path: Path) -> dict[str, str]:
    # Read only the three owner-bearing files. Never extract or inspect private runtime data.
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        licenses = [
            item
            for item in infos
            if PurePosixPath(item.filename).name == "LICENSE"
            and len(PurePosixPath(item.filename).parts) in {1, 2}
        ]
        if len(licenses) != 1:
            raise ValueError("ambiguous archive root")
        parent = PurePosixPath(licenses[0].filename).parent
        if ".." in parent.parts or parent.is_absolute():
            raise ValueError("invalid archive root")
        contents = {}
        for name in OWNER_FILES:
            target = str(parent / name)
            matches = [item for item in infos if item.filename == target]
            if len(matches) != 1:
                raise ValueError("missing or duplicate owner file")
            item = matches[0]
            if (
                item.is_dir()
                or stat.S_ISLNK(item.external_attr >> 16)
                or item.file_size > MAX_OWNER_FILE_BYTES
            ):
                raise ValueError("invalid owner file")
            contents[name] = archive.read(item).decode("utf-8")
        return contents


def _validate(contents: dict[str, str]) -> list[str]:
    errors = []
    owner = re.escape(PUBLISHER_HANDLE)
    copyrights = [
        line.strip()
        for line in contents["LICENSE"].splitlines()
        if re.match(r"copyright\b", line.strip(), re.IGNORECASE)
    ]
    if len(copyrights) != 1 or not re.fullmatch(
        rf"Copyright \(c\) \d{{4}}(?:-\d{{4}})? {owner}(?: and contributors)?", copyrights[0]
    ):
        errors.append("LICENSE: copyright owner must be the approved publisher handle")

    readme_lines = [line.strip() for line in contents["README.md"].splitlines() if line.strip()]
    bylines = [line for line in readme_lines if re.match(r"by\b", line, re.IGNORECASE)]
    author = f"By [{PUBLISHER_HANDLE}](https://github.com/{PUBLISHER_HANDLE})."
    allowed_footers = {
        author,
        author + " [MIT license](LICENSE) · [Third-party notices](NOTICE)",
    }
    # Match the entire final footer, not an approved prefix followed by another
    # attribution. Requiring the final nonempty line also rejects continuations.
    if len(bylines) != 1 or bylines[0] not in allowed_footers or readme_lines[-1] != bylines[0]:
        errors.append(
            "README.md: final author footer must use only the approved handle and optional license/notices links"
        )

    try:
        project = tomllib.loads(contents["pyproject.toml"]).get("project")
    except tomllib.TOMLDecodeError:
        project = None
    if not isinstance(project, dict):
        errors.append("pyproject.toml: readable project metadata is required")
        return errors
    for field in ("authors", "maintainers"):
        entries = project.get(field, [])
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or set(entry) - {"name", "email"}
            or entry.get("name") != PUBLISHER_HANDLE
            or (
                "email" in entry
                and (
                    not isinstance(entry["email"], str)
                    or not re.fullmatch(
                        rf"(?:\d+\+)?{owner}@users\.noreply\.github\.com", entry["email"]
                    )
                )
            )
            for entry in entries
        ):
            errors.append(
                f"pyproject.toml: {field} must use the approved handle and optional noreply email"
            )
    return errors


def check_publication(root: Path = ROOT, *, archive: Path | None = None) -> list[str]:
    try:
        contents = _archive(archive) if archive is not None else _source(root)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        # A wrong identity may itself be private: never echo values or parser exceptions.
        return ["Publication owner files are missing, unreadable, ambiguous or too large"]
    return _validate(contents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--archive", type=Path, help="Check owner fields inside this source ZIP instead"
    )
    args = parser.parse_args(argv)
    errors = check_publication(args.root, archive=args.archive)
    if errors:
        print("PUBLICATION_IDENTITY_INVALID: " + "; ".join(errors))
        return 1
    print("Publication owner fields: OK (approved handle; no network)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

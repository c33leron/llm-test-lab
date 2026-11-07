from __future__ import annotations

import importlib.util
import stat
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "check_publication", ROOT / "scripts/check_publication.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source(tmp_path: Path) -> Path:
    (tmp_path / "LICENSE").write_text(
        "MIT License\n\nCopyright (c) 2026 c33leron and contributors\n"
    )
    (tmp_path / "README.md").write_text("# Lab\n\nBy [c33leron](https://github.com/c33leron).\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "synthetic-lab"\n')
    return tmp_path


def test_current_checkout_uses_approved_publication_identity() -> None:
    assert module.check_publication(ROOT) == []


@pytest.mark.parametrize("file", ["LICENSE", "README.md"])
def test_unapproved_identity_is_rejected_without_echoing_it(
    tmp_path: Path, capsys, file: str
) -> None:
    root = source(tmp_path)
    path = root / file
    path.write_text(path.read_text().replace("c33leron", "Synthetic Unexpected Maintainer", 1))
    assert module.main(["--root", str(root)]) == 1
    output = capsys.readouterr()
    assert "PUBLICATION_IDENTITY_INVALID" in output.out
    assert "Synthetic Unexpected Maintainer" not in output.out + output.err


def test_wrong_github_owner_link_is_rejected(tmp_path: Path) -> None:
    root = source(tmp_path)
    (root / "README.md").write_text("By [c33leron](https://github.com/synthetic-other-owner).\n")
    assert module.check_publication(root)


@pytest.mark.parametrize(
    "suffix",
    [
        " and [Synthetic Coauthor](https://example.invalid).",
        ". Synthetic Unexpected Maintainer",
        ". [Synthetic credit](https://example.invalid)",
        ". [MIT license](LICENSE) · [Third-party notices](NOTICE) and Synthetic Coauthor",
        ".\nand [Synthetic Coauthor](https://example.invalid).",
        ". <!-- Synthetic Unexpected Maintainer -->",
    ],
)
def test_appended_authors_text_or_links_are_not_accepted_as_a_valid_prefix(
    tmp_path: Path, capsys, suffix: str
) -> None:
    root = source(tmp_path)
    (root / "README.md").write_text(f"By [c33leron](https://github.com/c33leron){suffix}\n")
    assert module.main(["--root", str(root)]) == 1
    output = capsys.readouterr()
    assert "PUBLICATION_IDENTITY_INVALID" in output.out
    assert "Synthetic" not in output.out + output.err


@pytest.mark.parametrize("field", ["authors", "maintainers"])
@pytest.mark.parametrize(
    "entry",
    [
        '{name="Synthetic Unexpected Maintainer"}',
        '{name="c33leron",email="synthetic@example.invalid"}',
        '"c33leron"',
    ],
)
def test_project_identity_metadata_is_allowlisted(tmp_path: Path, field: str, entry: str) -> None:
    root = source(tmp_path)
    (root / "pyproject.toml").write_text(f'[project]\nname="synthetic-lab"\n{field}=[{entry}]\n')
    assert module.check_publication(root)


def test_noreply_metadata_is_allowed_and_private_files_are_not_read(
    tmp_path: Path, monkeypatch
) -> None:
    root = source(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname="synthetic-lab"\nauthors=[{name="c33leron",email="123+c33leron@users.noreply.github.com"}]\n'
    )
    private = root / ".lab-state/README.md"
    private.parent.mkdir()
    private.write_text("By Synthetic Unexpected Maintainer\n")
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        assert path != private, "private runtime must not be inspected"
        reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert module.check_publication(root) == []
    assert reads == ["LICENSE", "README.md", "pyproject.toml"]


@pytest.mark.parametrize("change", ["missing", "symlink", "too_large", "malformed_toml"])
def test_unreadable_or_unsafe_owner_files_fail_closed(tmp_path: Path, change: str) -> None:
    root = source(tmp_path)
    if change == "missing":
        (root / "LICENSE").unlink()
    elif change == "symlink":
        (root / "README.md").unlink()
        (root / "README.md").symlink_to(root / "LICENSE")
    elif change == "too_large":
        (root / "README.md").write_text("x" * (module.MAX_OWNER_FILE_BYTES + 1))
    else:
        (root / "pyproject.toml").write_text("[project\n")
    assert module.check_publication(root)


@pytest.mark.parametrize("prefix", ["", "llm-test-lab/", "llm-test-lab-synthetic-sha/"])
def test_source_zip_owner_fields_are_checked_without_extracting(
    tmp_path: Path, prefix: str
) -> None:
    root = source(tmp_path)
    archive = root / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in module.OWNER_FILES:
            output.writestr(prefix + name, (root / name).read_bytes())
        output.writestr(prefix + ".lab-state/README.md", "By Synthetic Unexpected Maintainer")
    assert module.check_publication(archive=archive) == []


@pytest.mark.parametrize("change", ["identity", "duplicate", "symlink", "too_large", "traversal"])
def test_source_zip_cannot_hide_wrong_or_ambiguous_owner_fields(
    tmp_path: Path, change: str
) -> None:
    root = source(tmp_path)
    archive = root / "source.zip"
    prefix = "../" if change == "traversal" else "lab/"
    with zipfile.ZipFile(archive, "w") as output:
        for name in module.OWNER_FILES:
            info = zipfile.ZipInfo(prefix + name)
            content = (root / name).read_bytes()
            if name == "README.md":
                if change == "identity":
                    content = b"By [Synthetic Unexpected Maintainer](https://github.com/c33leron)."
                elif change == "symlink":
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                elif change == "too_large":
                    content = b"x" * (module.MAX_OWNER_FILE_BYTES + 1)
            output.writestr(info, content)
        if change == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                output.writestr(prefix + "README.md", b"duplicate")
    assert module.check_publication(archive=archive)

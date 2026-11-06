from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from llm_test_lab.cli import main, output, wizard

ROOT = Path(__file__).resolve().parents[2]


def setup_module_source():
    spec = importlib.util.spec_from_file_location("lab_setup_test", ROOT / "scripts/setup.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_start_non_tty_never_schedules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard.sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(
        wizard, "execute_plan", lambda *a, **k: pytest.fail("non-TTY start must not run")
    )
    assert wizard.start(ROOT) == 0
    assert "./lab start --case C01" in capsys.readouterr().out


def test_start_explicit_case_is_fixture_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plans = []

    def execute(root, plan, **kwargs):
        plans.append(plan)
        return 0, tmp_path

    monkeypatch.setattr(wizard, "execute_plan", execute)
    monkeypatch.setattr(wizard, "open_report", lambda *a: pytest.fail("no implicit browser"))
    assert wizard.start(ROOT, case_id="F01", variants=["all"]) == 0
    assert len(plans[0].cells) == 3
    assert {c.variant for c in plans[0].cells} == {"enforced", "prompt_only", "vulnerable"}
    assert all(c.source_mode == "fixture" and c.profile_id is None for c in plans[0].cells)


def test_interactive_choices_do_not_execute_arbitrary_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    replies = iter(["run shell", "3", "2", "C06", "н", "0"])
    monkeypatch.setattr(wizard.sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    plans = []
    monkeypatch.setattr(
        wizard, "execute_plan", lambda root, plan, **kw: (plans.append(plan) or 0, tmp_path)
    )
    monkeypatch.setattr(wizard, "open_report", lambda *a: pytest.fail("no browser without yes"))
    assert wizard.start(ROOT, terminal=True) == 0
    assert [c.case_id for c in plans[0].cells] == ["C06"]


@pytest.mark.parametrize("argv", [["--json", "validate"], ["validate", "--json"]])
def test_json_is_single_parseable_value(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(ROOT), *argv]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["inferenceCalls"] == 0


def test_json_error_and_no_case_instruction(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(ROOT), "start", "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["exitCode"] == 2
    assert "CASE_REQUIRED" in data["message"]


def test_root_launcher_before_setup_and_paths_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "fresh checkout with spaces"
    root.mkdir()
    launcher = root / "lab"
    launcher.write_bytes((ROOT / "lab").read_bytes())
    launcher.chmod(0o755)
    help_result = subprocess.run(
        [str(launcher), "help"], cwd=tmp_path, text=True, capture_output=True, timeout=5
    )
    assert help_result.returncode == 0
    assert "./lab setup" in help_result.stdout
    result = subprocess.run(
        [str(launcher), "start"], cwd=tmp_path, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == 2
    assert "DEPENDENCY_MISSING" in result.stderr
    assert not (root / ".lab-state").exists()


def test_real_start_non_tty_does_not_create_run() -> None:
    runs = ROOT / "runs"
    existed = runs.exists()
    before = set(runs.glob("*"))
    completed = subprocess.run(
        [str(ROOT / "lab"), "start"],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0
    assert "Ничего не запущено" in completed.stdout
    assert set(runs.glob("*")) == before
    assert runs.exists() is existed


def test_open_report_rejects_external_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "report.html"
    outside.write_text("test")
    monkeypatch.setattr(
        output.subprocess, "run", lambda *a, **k: pytest.fail("must reject before opening")
    )
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        output.open_report(ROOT, outside)


def test_explicit_open_uses_local_file_not_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "runs/a/report.html"
    report.parent.mkdir(parents=True)
    report.write_text("safe test")
    monkeypatch.setattr(output.platform, "system", lambda: "Darwin")
    commands = []
    monkeypatch.setattr(
        output.subprocess,
        "run",
        lambda command, **kw: (
            commands.append((command, kw)) or subprocess.CompletedProcess(command, 0)
        ),
    )
    output.open_report(tmp_path, report)
    assert commands[0][0] == ["/usr/bin/open", str(report)]
    assert "shell" not in commands[0][1]


def test_setup_environment_is_project_local_and_does_not_inherit_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = setup_module_source()
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_NOT_FOR_SETUP")
    monkeypatch.setenv("UV_ENV_FILE", "/outside/.env")
    env = setup.environment(tmp_path)
    assert "OPENAI_API_KEY" not in env and "UV_ENV_FILE" not in env
    for name in ("HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache"):
        assert Path(env[name]).is_relative_to(tmp_path)


def test_setup_version_recognizes_node_v_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup = setup_module_source()
    binary = tmp_path / "node"
    binary.touch()
    monkeypatch.setattr(setup.subprocess, "check_output", lambda *a, **kw: "v24.11.0\n")
    assert setup.version(binary, "24.11.0", {})
    assert not setup.version(binary, "24.11.1", {})


def test_setup_requires_exact_pins_and_verified_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = setup_module_source()
    (tmp_path / ".uv-version").write_text("latest")
    with pytest.raises(ValueError, match="SETUP_PIN_INVALID"):
        setup.pin(tmp_path, ".uv-version")
    monkeypatch.setattr(setup, "fetch", lambda *a, **kw: b"wrong artifact")
    with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
        setup.checked_download(tmp_path, "https://example.invalid/pinned", "pinned.zip", "0" * 64)
    assert not (tmp_path / ".lab-state/downloads/pinned.zip").exists()
    with pytest.raises(ValueError, match="filename escapes"):
        setup.checked_download(tmp_path, "https://example.invalid/", "../outside", "0" * 64)


def test_setup_rejects_archive_escape(tmp_path: Path) -> None:
    setup = setup_module_source()
    archive = tmp_path / "unsafe.tgz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo("../escape")
        item.size = 1
        stream.addfile(item, io.BytesIO(b"x"))
    target = tmp_path / "extract"
    target.mkdir()
    with pytest.raises(ValueError, match="unsafe member"):
        setup.safe_extract(archive, target)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("technical_exit", [1, 3, 130])
def test_probe_uses_one_bounded_plan_and_one_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    technical_exit: int,
) -> None:
    import llm_test_lab.cli as cli
    import llm_test_lab.conformance as conformance

    monkeypatch.setenv("LLM_LAB_LOCAL_MODEL", "synthetic-model-not-called")
    plans = []

    def execute(root, plan, **kwargs):
        assert kwargs["emit_output"] is False
        plans.append(plan)
        (tmp_path / "manifest.json").write_text(
            json.dumps({"exitCode": technical_exit, "status": "finished"})
        )
        return technical_exit, tmp_path

    monkeypatch.setattr(cli, "execute_plan", execute)
    monkeypatch.setattr(
        conformance,
        "assess_probe",
        lambda path: {
            "limitedProbePassed": technical_exit == 1,
            "supportEligible": False,
            "fullSupportEligible": False,
        },
    )
    assert (
        main(["--root", str(ROOT), "probe", "--profile", "local-reference", "--json"])
        == technical_exit
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["exitCode"] == technical_exit
    assert [c.case_id for c in plans[0].cells] == ["C05", "C01", "C10"]
    assert plans[0].max_inference_calls == 18
    assert all(c.variant == "enforced" for c in plans[0].cells)
    assert payload["assessment"]["fullSupportEligible"] is False
    assert (tmp_path / "conformance.json").is_file()

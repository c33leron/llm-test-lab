"""Explicit local bootstrap, requiring only Python 3.10+ and HTTPS.

No global installs, shell pipes, credentials, model downloads, or lockfile updates.
Download versions are repository pins; registry metadata supplies checksums, not latest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD = 300 * 1024 * 1024


def pin(root: Path, filename: str) -> str:
    value = (root / filename).read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"SETUP_PIN_INVALID: {filename} должен содержать точную x.y.z версию")
    return value


def environment(root: Path) -> dict[str, str]:
    state = root / ".lab-state"
    # HOME/config/cache paths are project-local, not the user's existing tool configuration.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "HOME": str(state / "setup-home"),
        "TMPDIR": str(state / "tmp"),
        "XDG_CONFIG_HOME": str(state / "setup-home/.config"),
        "UV_CACHE_DIR": str(state / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(state / "python"),
        "UV_PYTHON_BIN_DIR": str(state / "python-bin"),
        "UV_TOOL_DIR": str(state / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(state / "uv-tools-bin"),
        "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
        "UV_NO_PROGRESS": "1",
        "UV_NO_CONFIG": "1",
        "UV_NATIVE_TLS": "true",
        "npm_config_cache": str(state / "npm-cache"),
        "npm_config_userconfig": str(state / "empty.npmrc"),
        "npm_config_globalconfig": str(state / "empty-global.npmrc"),
        "npm_config_update_notifier": "false",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }
    for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "UV_CACHE_DIR", "npm_config_cache"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    for name in ("npm_config_userconfig", "npm_config_globalconfig"):
        Path(env[name]).touch(exist_ok=True)
    return env


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # python.org macOS installs may not have run Install Certificates.command.
    # Trust the existing system CA bundle; never disable TLS validation.
    if platform.system() == "Darwin" and Path("/etc/ssl/cert.pem").is_file():
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


def fetch(url: str, *, max_bytes: int = MAX_DOWNLOAD) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("SETUP_DOWNLOAD_INVALID: только HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "llm-test-lab-local-setup"})
    with urllib.request.urlopen(request, context=tls_context(), timeout=120) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("SETUP_DOWNLOAD_INVALID: downgrade redirect")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("SETUP_DOWNLOAD_TOO_LARGE")
    return data


def metadata(url: str) -> dict[str, Any]:
    result = json.loads(fetch(url, max_bytes=4 * 1024 * 1024))
    if not isinstance(result, dict):
        raise ValueError("SETUP_METADATA_INVALID")
    return result


def checked_download(
    root: Path, url: str, filename: str, digest: str, algorithm: str = "sha256"
) -> Path:
    if Path(filename).name != filename:
        raise ValueError("SETUP_DOWNLOAD_INVALID: filename escapes download cache")
    path = root / ".lab-state/downloads" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.new(algorithm, path.read_bytes()).hexdigest() == digest:
        return path
    data = fetch(url)
    if hashlib.new(algorithm, data).hexdigest() != digest:
        raise ValueError(f"SETUP_CHECKSUM_MISMATCH: {filename}; ничего не распаковано")
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def version(
    executable: Path, expected: str, env: dict[str, str], *, prefix: list[str] | None = None
) -> bool:
    if not executable.is_file():
        return False
    try:
        output = subprocess.check_output(
            [*(prefix or []), str(executable), "--version"],
            env=env,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).strip()
        return expected in re.findall(r"\d+\.\d+\.\d+", output)
    except (OSError, subprocess.SubprocessError):
        return False


def install_uv(root: Path, wanted: str, env: dict[str, str]) -> Path:
    output = root / ".lab-state/bootstrap/bin/uv"
    if version(output, wanted, env):
        return output
    system, machine = platform.system(), platform.machine().lower()
    tag = {
        ("Darwin", "arm64"): "macosx_11_0_arm64",
        ("Darwin", "x86_64"): "macosx_10_12_x86_64",
        ("Linux", "x86_64"): "manylinux_2_17_x86_64",
        ("Linux", "aarch64"): "manylinux_2_17_aarch64",
    }.get((system, machine))
    if tag is None:
        raise ValueError(f"PLATFORM_UNSUPPORTED: {system}/{machine}")
    release = metadata(f"https://pypi.org/pypi/uv/{wanted}/json")
    files = [
        entry
        for entry in release["urls"]
        if tag in entry["filename"] and entry["filename"].endswith(".whl")
    ]
    if len(files) != 1:
        raise ValueError("SETUP_METADATA_INVALID: нет однозначного uv wheel для платформы")
    item = files[0]
    wheel = checked_download(root, item["url"], item["filename"], item["digests"]["sha256"])
    with zipfile.ZipFile(wheel) as archive:
        name = f"uv-{wanted}.data/scripts/uv"
        data = archive.read(name)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".new")
    temporary.write_bytes(data)
    temporary.chmod(0o755)
    if not version(temporary, wanted, env):
        temporary.unlink()
        raise ValueError("SETUP_VERSION_MISMATCH: downloaded uv")
    temporary.replace(output)
    return output


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract trusted-distribution bytes without allowing traversal or link escapes."""
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if (
                not target.is_relative_to(destination.resolve())
                or member.isdev()
                or member.isfifo()
            ):
                raise ValueError("SETUP_ARCHIVE_INVALID: unsafe member")
            if member.issym() or member.islnk():
                link = (
                    (target.parent / member.linkname).resolve()
                    if member.issym()
                    else (destination / member.linkname).resolve()
                )
                if not link.is_relative_to(destination.resolve()):
                    raise ValueError("SETUP_ARCHIVE_INVALID: unsafe link")
        if hasattr(tarfile, "data_filter"):
            source.extractall(destination, filter="data")
        else:
            source.extractall(destination)  # Python 3.10: all members/links validated above.


def install_node(root: Path, wanted: str, env: dict[str, str]) -> Path:
    target = root / ".lab-state/node-runtime/bin/node"
    existing = root / ".lab-state/node-runtime/node_modules/node/bin/node"
    if version(target, wanted, env):
        return target
    if version(existing, wanted, env):
        return existing
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(platform.machine().lower())
    if system is None or arch is None:
        raise ValueError("PLATFORM_UNSUPPORTED: Node runtime")
    name = f"node-v{wanted}-{system}-{arch}"
    filename = name + ".tar.gz"
    base = f"https://nodejs.org/dist/v{wanted}/"
    checksums = fetch(base + "SHASUMS256.txt", max_bytes=1024 * 1024).decode()
    entries = [line.split() for line in checksums.splitlines() if line.split()[-1:] == [filename]]
    if len(entries) != 1:
        raise ValueError("SETUP_METADATA_INVALID: Node checksum missing")
    archive = checked_download(root, base + filename, filename, entries[0][0])
    with tempfile.TemporaryDirectory(dir=root / ".lab-state", prefix="node-extract-") as temp:
        staging = Path(temp)
        safe_extract(archive, staging)
        binary = staging / name / "bin/node"
        if not version(binary, wanted, env):
            raise ValueError("SETUP_VERSION_MISMATCH: downloaded Node")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".new")
        shutil.copyfile(binary, temporary)
        temporary.chmod(0o755)
        temporary.replace(target)
    return target


def install_npm(root: Path, node: Path, wanted: str, env: dict[str, str]) -> Path:
    output = root / ".lab-state/npm-runtime/package/bin/npm-cli.js"
    if version(output, wanted, env, prefix=[str(node)]):
        return output
    release = metadata(f"https://registry.npmjs.org/npm/{wanted}")
    if release.get("version") != wanted:
        raise ValueError("SETUP_VERSION_MISMATCH: npm metadata")
    integrity = release["dist"]["integrity"]
    algorithm, encoded = integrity.split("-", 1)
    if algorithm != "sha512":
        raise ValueError("SETUP_METADATA_INVALID: npm requires SHA512 integrity")
    archive = checked_download(
        root,
        release["dist"]["tarball"],
        f"npm-{wanted}.tgz",
        base64.b64decode(encoded, validate=True).hex(),
        "sha512",
    )
    with tempfile.TemporaryDirectory(dir=root / ".lab-state", prefix="npm-extract-") as temp:
        staging = Path(temp)
        safe_extract(archive, staging)
        candidate = staging / "package/bin/npm-cli.js"
        if not version(candidate, wanted, env, prefix=[str(node)]):
            raise ValueError("SETUP_VERSION_MISMATCH: downloaded npm")
        destination = output.parents[2]
        destination.mkdir(parents=True, exist_ok=True)
        old_package = destination / "package"
        if old_package.exists():
            # Replace only setup-owned runtime files; evidence/runs never enter this path.
            shutil.rmtree(old_package)
        shutil.move(str(staging / "package"), str(old_package))
    return output


def run(root: Path, *, json_output: bool = False) -> int:
    root = root.resolve()
    if sys.version_info < (3, 10):  # noqa: UP036 — this bootstrap runs before the project's Python exists.
        raise ValueError("BOOTSTRAP_PYTHON_MISSING: нужен Python 3.10+")
    uv_version, node_version, npm_version = (
        pin(root, ".uv-version"),
        pin(root, ".node-version"),
        pin(root, ".npm-version"),
    )
    python_version = pin(root, ".python-version")
    env = environment(root)
    log_path = root / ".lab-state/setup.log"
    old_fingerprint = root / ".lab-state/installation.json"
    if old_fingerprint.exists():
        shutil.copyfile(old_fingerprint, root / ".lab-state/installation.before-setup.json")

    def say(message: str) -> None:
        print(message, file=sys.stderr if json_output else sys.stdout, flush=True)

    say(
        "Установка только в этот проект. Нужна сеть для зависимостей; модели и ключи не используются."
    )
    say(f"1/4 uv {uv_version}, Node {node_version}, npm {npm_version} — точные версии.")
    uv = install_uv(root, uv_version, env)
    node = install_node(root, node_version, env)
    npm = install_npm(root, node, npm_version, env)
    env["PATH"] = f"{node.parent}:{uv.parent}:{env['PATH']}"
    with log_path.open("w", encoding="utf-8") as log:
        commands = [
            (
                f"2/4 Python {python_version} и зависимости по uv.lock.",
                [
                    str(uv),
                    "sync",
                    "--locked",
                    "--python",
                    python_version,
                    "--project",
                    str(root),
                    "--no-config",
                ],
            ),
            (
                "3/4 Promptfoo и зависимости по package-lock.json.",
                [
                    str(node),
                    str(npm),
                    "ci",
                    "--prefix",
                    str(root / "tooling/promptfoo"),
                    "--no-audit",
                    "--no-fund",
                ],
            ),
            (
                "4/4 Проверка установки и сохранение fingerprint.",
                [
                    str(root / ".venv/bin/python"),
                    "-m",
                    "llm_test_lab.cli",
                    "--root",
                    str(root),
                    "doctor",
                    "--record-installation",
                    "--json",
                ],
            ),
        ]
        for message, command in commands:
            say(message)
            completed = subprocess.run(
                command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
            )
            log.flush()
            if completed.returncode:
                raise ValueError(
                    f"SETUP_FAILED: этап «{message}», код {completed.returncode}. Подробности: {log_path}. Исправь причину и повтори ./lab setup; lockfiles и runs не изменяются."
                )
    result = {
        "status": "ready",
        "modelDownloads": 0,
        "inferenceCalls": 0,
        "log": str(log_path),
        "next": "./lab start",
    }
    print(
        json.dumps(result, ensure_ascii=False)
        if json_output
        else "Готово. Теперь ./lab start — первый опыт без модели и ключа."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit, pinned, project-local setup")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        return run(ROOT, json_output=args.json)
    except KeyboardInterrupt:
        print("Установка остановлена. Для продолжения повтори ./lab setup.", file=sys.stderr)
        return 130
    except (
        ValueError,
        OSError,
        KeyError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as exc:
        message = f"SETUP: {exc}"
        if args.json:
            print(
                json.dumps(
                    {"status": "error", "message": message, "exitCode": 2}, ensure_ascii=False
                )
            )
        else:
            print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

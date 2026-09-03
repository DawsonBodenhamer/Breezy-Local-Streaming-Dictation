"""Breezy-specific validation, version, and curated artifact adapter."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

VERSION_PATHS = ("pyproject.toml", "src/whisper_dictation/__init__.py")
ARCHIVE_ROOT_FILES = {
    "README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml",
    "requirements.in", "requirements.lock", "requirements.cuda.in", "requirements.cuda.lock", "setup.ps1",
}
ARCHIVE_DIRECTORIES = {"assets", "config", "docs", "licenses", "src", "windows"}
REQUIRED_ARCHIVE_FILES = ARCHIVE_ROOT_FILES | {
    "config/config.example.toml", "config/runtime.example.env", "windows/supervisor.ps1", "windows/win_h.ahk",
    "windows/hotkey_capture.ahk", "windows/physical_context_signal.ahk", "windows/hotkey_apply.ps1", "windows/client_bootstrap.pyw",
    "windows/list_microphones.py", "windows/startup_hidden.vbs", "windows/text_conversion_manager.py",
    "windows/formatting_config.py",
    "assets/breezy_dictation_icon_hd.png", "assets/local_dictation_start.wav",
    "assets/local_dictation_stop.wav", "licenses/faster-whisper-dictation-MIT.txt",
    "src/whisper_dictation/__init__.py", "src/whisper_dictation/engine/local.py",
    "src/whisper_dictation/hotkey/listener.py",
}
FORBIDDEN_PARTS = {".git", ".idea", "__pycache__", "backups", "build", "dist", "logs", "probes", "recordings", "rollback", "tests"}
TEXT_SUFFIXES = {".ahk", ".env", ".in", ".json", ".lock", ".md", ".ps1", ".py", ".pyw", ".toml", ".txt", ".vbs", ".yaml", ".yml"}
PRIVATE_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\"), re.compile(r"(?i)\bdevice\s*=\s*[0-9]+"),
    re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"),
)
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_ATTR = 0o100644 << 16


def _pyproject_version(text: str) -> str:
    active = False
    values: list[str] = []
    for line in text.splitlines():
        if line == "[project]":
            active = True
            continue
        if active and line.startswith("["):
            break
        if active:
            match = re.fullmatch(r'version\s*=\s*"([^"]+)"\s*', line)
            if match:
                values.append(match.group(1))
    if len(values) != 1:
        raise RuntimeError("pyproject.toml [project] must contain exactly one version")
    return values[0]


def read_versions(context: Any, ref: str | None = None) -> dict[str, str]:
    pyproject = _pyproject_version(context.tree_text(ref, VERSION_PATHS[0]))
    init_values = re.findall(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', context.tree_text(ref, VERSION_PATHS[1]))
    if len(init_values) != 1:
        raise RuntimeError("__init__.py must contain exactly one __version__")
    return {VERSION_PATHS[0]: pyproject, VERSION_PATHS[1]: init_values[0]}


def _archive_paths(context: Any, ref: str | None) -> tuple[str, ...]:
    selected = sorted(
        path for path in context.tracked_paths(ref)
        if path in ARCHIVE_ROOT_FILES or (Path(path).parts and Path(path).parts[0] in ARCHIVE_DIRECTORIES)
    )
    missing = sorted(REQUIRED_ARCHIVE_FILES.difference(selected))
    if missing:
        raise RuntimeError(f"Curated archive is missing required files: {', '.join(missing)}")
    for path in selected:
        parts = Path(path).parts
        if path.startswith("/") or ".." in parts or any(part.casefold() in FORBIDDEN_PARTS for part in parts):
            raise RuntimeError(f"Unsafe or forbidden archive path: {path}")
    return tuple(selected)


def validate(context: Any, ref: str | None = None) -> None:
    for path in context.tracked_paths(ref):
        parts = Path(path).parts
        if any(part.casefold() in FORBIDDEN_PARTS for part in parts) or path.casefold().endswith((".pyc", ".pyo")):
            raise RuntimeError(f"Forbidden public tracked path: {path}")
        if Path(path).suffix.casefold() in TEXT_SUFFIXES and path not in {"tools/release_adapter.py", "tools/release_engine.py"}:
            text = context.tree_text(ref, path)
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(text):
                    raise RuntimeError(f"Private or machine-specific content in {path}")
        if Path(path).suffix.casefold() in {".py", ".pyw"}:
            ast.parse(context.tree_text(ref, path), filename=path)
    _archive_paths(context, ref)
    if ref is None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(context.root / "src")
        result = subprocess.run([sys.executable, "-c", "import whisper_dictation; import whisper_dictation.cli"], cwd=context.root, env=environment, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"Package import smoke failed: {result.stderr.strip()}")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell is required for release validation")
        for path in context.tracked_paths():
            if Path(path).suffix.casefold() != ".ps1":
                continue
            escaped = str(context.root / path).replace("'", "''")
            command = "$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('" + escaped + "',[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e|%{Write-Error $_.Message};exit 1}"
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"PowerShell parse failed for {path}: {result.stderr.strip()}")


def artifact_name(version: str) -> str:
    return f"Breezy-Dictation-v{version}.zip"


def build_artifact(context: Any, ref: str, version: str, output: Path) -> tuple[dict[str, Any], ...]:
    manifest: list[dict[str, Any]] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _archive_paths(context, ref):
            content = context.tree_bytes(ref, path)
            info = zipfile.ZipInfo(path, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = ZIP_ATTR
            archive.writestr(info, content)
            manifest.append({"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    with zipfile.ZipFile(output) as archive:
        if [entry.filename for entry in archive.infolist()] != [entry["path"] for entry in manifest]:
            raise RuntimeError("Curated archive manifest/order mismatch")
        for info, expected in zip(archive.infolist(), manifest, strict=True):
            content = archive.read(info)
            if info.date_time != ZIP_EPOCH or info.create_system != 0 or info.external_attr != ZIP_ATTR:
                raise RuntimeError(f"Nondeterministic ZIP metadata: {info.filename}")
            if hashlib.sha256(content).hexdigest() != expected["sha256"]:
                raise RuntimeError(f"ZIP content mismatch: {info.filename}")
    return tuple(manifest)


def update_versions(context: Any, next_version: str) -> None:
    pyproject = context.root / VERSION_PATHS[0]
    text = pyproject.read_text(encoding="utf-8")
    updated, count = re.subn(r'(?m)^(version\s*=\s*")[^"]+("\s*)$', rf'\g<1>{next_version}\g<2>', text, count=1)
    if count != 1:
        raise RuntimeError("Expected one pyproject version update")
    pyproject.write_text(updated, encoding="utf-8", newline="\n")
    init = context.root / VERSION_PATHS[1]
    text = init.read_text(encoding="utf-8")
    updated, count = re.subn(r'(?m)^(__version__\s*=\s*")[^"]+("\s*)$', rf'\g<1>{next_version}\g<2>', text)
    if count != 1:
        raise RuntimeError("Expected one __version__ update")
    init.write_text(updated, encoding="utf-8", newline="\n")

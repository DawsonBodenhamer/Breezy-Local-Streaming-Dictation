"""Deterministic Breezy Local Streaming Dictation release publisher."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GITHUB_REPOSITORY = "DawsonBodenhamer/Breezy-Local-Streaming-Dictation"
EXPECTED_BRANCH = "main"
CHANGELOG_PATH = Path("CHANGELOG.md")
PYPROJECT_PATH = Path("pyproject.toml")
INIT_PATH = Path("src/whisper_dictation/__init__.py")
VERSION_PATHS = (PYPROJECT_PATH, INIT_PATH)
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CHANGELOG_HEADER = re.compile(r"^## \[([^]]+)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2})$")
ALLOWED_CHANGELOG_CATEGORIES = {
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
}
ARCHIVE_ROOT_FILES = frozenset(
    {
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "requirements.in",
        "requirements.lock",
        "requirements.cuda.in",
        "requirements.cuda.lock",
        "setup.ps1",
    }
)
ARCHIVE_DIRECTORIES = frozenset({"assets", "config", "docs", "licenses", "src", "windows"})
REQUIRED_ARCHIVE_FILES = frozenset(
    {
        *ARCHIVE_ROOT_FILES,
        "config/config.example.toml",
        "config/runtime.example.env",
        "windows/supervisor.ps1",
        "windows/win_h.ahk",
        "windows/hotkey_capture.ahk",
        "windows/hotkey_apply.ps1",
        "windows/client_bootstrap.pyw",
        "windows/list_microphones.py",
        "windows/startup_hidden.vbs",
        "windows/text_conversion_manager.py",
        "assets/breezy_local_streaming_dictation_icon_hd.png",
        "assets/local_dictation_start.wav",
        "assets/local_dictation_stop.wav",
        "assets/tray_menu.png",
        "licenses/faster-whisper-dictation-MIT.txt",
        "src/whisper_dictation/__init__.py",
        "src/whisper_dictation/engine/local.py",
        "src/whisper_dictation/hotkey/listener.py",
    }
)
FORBIDDEN_PATH_PARTS = {
    ".git",
    ".idea",
    "__pycache__",
    "backups",
    "build",
    "dist",
    "logs",
    "probes",
    "recordings",
    "rollback",
    "tests",
}
TEXT_SUFFIXES = {
    ".ahk",
    ".env",
    ".in",
    ".json",
    ".lock",
    ".md",
    ".ps1",
    ".py",
    ".pyw",
    ".toml",
    ".txt",
    ".vbs",
    ".yaml",
    ".yml",
}
PRIVATE_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\"),
    re.compile(r"(?i)\bdevice\s*=\s*[0-9]+"),
    re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"),
)
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_EXTERNAL_ATTR = 0o100644 << 16
PREPUBLICATION_FAILURE = 1
PARTIAL_SUCCESS = 2


class ReleaseError(RuntimeError):
    """A fail-closed release workflow error."""


class PartialSuccess(ReleaseError):
    """GitHub publication succeeded but local completion did not."""


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    date: str
    notes: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def title(self) -> str:
        return f"v{self.version} - {self.date}"

    @property
    def asset_name(self) -> str:
        return f"Breezy-Local-Streaming-Dictation-v{self.version}.zip"


@dataclass(frozen=True)
class ArchiveFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    files: tuple[ArchiveFile, ...]
    sha256: str


def git_args(*args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={PROJECT_ROOT}", *args]


def run_git(*args: str, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        git_args(*args),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=capture_output,
        text=capture_output,
    )


def run_git_bytes(*args: str) -> bytes:
    result = subprocess.run(
        git_args(*args),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def normalize_remote(value: str) -> str:
    value = value.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    marker = "github.com/"
    return value.split(marker, 1)[1] if marker in value else value


def tracked_paths(ref: str | None = None) -> tuple[str, ...]:
    args = ["ls-tree", "-r", "--name-only", "-z", ref] if ref else ["ls-files", "-z"]
    raw = run_git_bytes(*args)
    return tuple(sorted(path.decode("utf-8") for path in raw.split(b"\0") if path))


def tree_bytes(ref: str | None, relative: str) -> bytes:
    if ref:
        return run_git_bytes("show", f"{ref}:{relative}")
    return (PROJECT_ROOT / Path(relative)).read_bytes()


def tree_text(ref: str | None, relative: str) -> str:
    try:
        return tree_bytes(ref, relative).decode("utf-8")
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
        raise ReleaseError(f"Could not read UTF-8 file {relative}: {exc}") from exc


def parse_changelog(text: str) -> ReleaseMetadata:
    text = normalize_newlines(text)
    preamble = (
        "# Changelog\n\n"
        "All notable changes to this project will be documented in this file.\n\n"
        "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),\n"
        "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
    )
    if not text.startswith(preamble):
        raise ReleaseError("CHANGELOG.md does not match the required preamble")

    lines = text.split("\n")
    header_index = next((index for index, line in enumerate(lines) if line.startswith("## ")), None)
    if header_index is None:
        raise ReleaseError("CHANGELOG.md has no release section")
    match = CHANGELOG_HEADER.fullmatch(lines[header_index])
    if not match:
        raise ReleaseError("The first changelog section has an invalid version/date heading")
    version, release_date = match.groups()
    if not RELEASE_VERSION.fullmatch(version):
        raise ReleaseError(f"Release version is not a stable semantic version: {version}")
    try:
        parsed_date = dt.date.fromisoformat(release_date)
    except ValueError as exc:
        raise ReleaseError(f"Invalid changelog date: {release_date}") from exc
    if parsed_date.isoformat() != release_date:
        raise ReleaseError(f"Invalid changelog date: {release_date}")

    section_end = next((index for index in range(header_index + 1, len(lines)) if lines[index] == "---"), None)
    if section_end is None:
        raise ReleaseError("The first changelog section has no exact --- divider")
    if any(line.startswith("## ") for line in lines[header_index + 1 : section_end]):
        raise ReleaseError("The first changelog section contains another version heading")
    body_lines = lines[header_index + 1 : section_end]
    while body_lines and body_lines[0] == "":
        body_lines.pop(0)
    while body_lines and body_lines[-1] == "":
        body_lines.pop()
    if not body_lines:
        raise ReleaseError("The first changelog section has no release notes")

    categories = [line[4:] for line in body_lines if line.startswith("### ")]
    if not categories or any(category not in ALLOWED_CHANGELOG_CATEGORIES for category in categories):
        raise ReleaseError("The first changelog section has invalid category headings")
    if any(not line.startswith(("### ", "- ", "  - ", "  ", "")) for line in body_lines):
        raise ReleaseError("The first changelog section has invalid bullet formatting")
    notes = "\n".join(body_lines)
    return ReleaseMetadata(version=version, date=release_date, notes=notes)


def project_version(text: str) -> str:
    sections = list(re.finditer(r"(?m)^\[([^]]+)\]\s*$", text))
    project_sections = [match for match in sections if match.group(1) == "project"]
    if len(project_sections) != 1:
        raise ReleaseError("pyproject.toml must contain exactly one [project] section")
    start = project_sections[0].end()
    end = next((match.start() for match in sections if match.start() > start), len(text))
    matches = list(re.finditer(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text[start:end]))
    if len(matches) != 1:
        raise ReleaseError("pyproject.toml [project] must contain exactly one version")
    return matches[0].group(1)


def read_versions(ref: str | None = None) -> tuple[str, str]:
    pyproject = project_version(tree_text(ref, PYPROJECT_PATH.as_posix()))
    init_text = tree_text(ref, INIT_PATH.as_posix())
    matches = re.findall(r"(?m)^__version__\s*=\s*\"([^\"]+)\"\s*$", init_text)
    if len(matches) != 1:
        raise ReleaseError("__init__.py must contain exactly one __version__ assignment")
    return pyproject, matches[0]


def bump_version(version: str) -> str:
    if not RELEASE_VERSION.fullmatch(version):
        raise ReleaseError(f"Cannot patch-bump invalid version: {version}")
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def archive_paths(ref: str | None = None) -> tuple[str, ...]:
    tracked = tracked_paths(ref)
    selected: list[str] = []
    for path in tracked:
        parts = Path(path).parts
        if path in ARCHIVE_ROOT_FILES:
            selected.append(path)
        elif parts and parts[0] in ARCHIVE_DIRECTORIES:
            selected.append(path.replace("\\", "/"))
    if not selected:
        raise ReleaseError("The curated archive allowlist selected no files")
    selected = sorted(set(selected))
    missing = sorted(REQUIRED_ARCHIVE_FILES.difference(selected))
    if missing:
        raise ReleaseError(f"Curated archive is missing required files: {', '.join(missing)}")
    for path in selected:
        normalized = Path(path).as_posix()
        if normalized.startswith("/") or ".." in Path(normalized).parts:
            raise ReleaseError(f"Unsafe archive path: {path}")
        if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in Path(normalized).parts):
            raise ReleaseError(f"Forbidden archive path: {path}")
    return tuple(selected)


def validate_public_tree(ref: str | None = None) -> None:
    tracked = tracked_paths(ref)
    for path in tracked:
        parts = Path(path).parts
        if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in parts):
            raise ReleaseError(f"Forbidden tracked public path: {path}")
        if path.casefold().endswith((".pyc", ".pyo")):
            raise ReleaseError(f"Generated Python file is tracked: {path}")
        suffix = Path(path).suffix.casefold()
        if suffix in TEXT_SUFFIXES:
            try:
                content = tree_text(ref, path)
            except ReleaseError:
                raise
            if path == "tools/publish_release.py":
                continue
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(content):
                    raise ReleaseError(f"Private or machine-specific text in {path}: {pattern.pattern}")
    archive_paths(ref)


def validate_python_sources(ref: str | None = None) -> None:
    for path in tracked_paths(ref):
        if Path(path).suffix.casefold() not in {".py", ".pyw"}:
            continue
        try:
            ast.parse(tree_text(ref, path), filename=path)
        except (SyntaxError, ReleaseError) as exc:
            raise ReleaseError(f"Python parse failed for {path}: {exc}") from exc

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import whisper_dictation; import whisper_dictation.cli"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ReleaseError(f"Package import smoke failed: {result.stderr.strip()}")


def powershell_executable() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        raise ReleaseError("PowerShell is required for release preflight")
    return executable


def validate_powershell_sources(ref: str | None = None) -> None:
    executable = powershell_executable()
    for path in tracked_paths(ref):
        if Path(path).suffix.casefold() != ".ps1":
            continue
        if ref:
            scratch = Path(tempfile.mkdtemp(prefix="blds-ps-")) / Path(path).name
            scratch.write_text(tree_text(ref, path), encoding="utf-8", newline="\n")
            parse_path = scratch
        else:
            parse_path = PROJECT_ROOT / Path(path)
        escaped = str(parse_path).replace("'", "''")
        command = (
            "$tokens = $null; $errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ReleaseError(f"PowerShell parse failed for {path}: {result.stderr.strip()}")
        if ref:
            shutil.rmtree(scratch.parent, ignore_errors=True)


def zip_file(path: str, ref: str | None) -> bytes:
    return tree_bytes(ref, path)


def build_archive(metadata: ReleaseMetadata, ref: str | None, output: Path) -> ArchiveInfo:
    paths = archive_paths(ref)
    output.parent.mkdir(parents=True, exist_ok=True)
    files: list[ArchiveFile] = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            content = zip_file(path, ref)
            info = zipfile.ZipInfo(path, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = ZIP_EXTERNAL_ATTR
            archive.writestr(info, content)
            files.append(ArchiveFile(path=path, size=len(content), sha256=hashlib.sha256(content).hexdigest()))
    if CHANGELOG_PATH.as_posix() not in {entry.path for entry in files}:
        raise ReleaseError("Curated archive does not contain CHANGELOG.md")
    verify_archive(output, tuple(files))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return ArchiveInfo(path=output, files=tuple(files), sha256=digest)


def verify_archive(path: Path, expected: tuple[ArchiveFile, ...]) -> None:
    expected_names = [entry.path for entry in expected]
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if names != expected_names or len(names) != len(set(names)):
            raise ReleaseError("ZIP entry order or uniqueness does not match the curated manifest")
        for info, entry in zip(infos, expected, strict=True):
            normalized = Path(info.filename).as_posix()
            if normalized != info.filename or normalized.startswith("/") or ".." in Path(normalized).parts:
                raise ReleaseError(f"Unsafe ZIP entry: {info.filename}")
            if info.date_time != ZIP_EPOCH or info.create_system != 0 or info.external_attr != ZIP_EXTERNAL_ATTR:
                raise ReleaseError(f"ZIP metadata is not deterministic for {info.filename}")
            content = archive.read(info)
            if info.file_size != entry.size or hashlib.sha256(content).hexdigest() != entry.sha256:
                raise ReleaseError(f"ZIP content mismatch for {info.filename}")


def run_gh(*args: str, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], cwd=PROJECT_ROOT, capture_output=capture_output, text=True)


def gh_json(*args: str, allow_missing: bool = False) -> dict[str, Any] | None:
    result = run_gh(*args)
    if result.returncode:
        if allow_missing and "404" in result.stderr:
            return None
        raise ReleaseError(f"GitHub CLI read failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"GitHub CLI returned invalid JSON: {exc}") from exc


def require_github_cli() -> None:
    try:
        version = run_gh("--version")
    except FileNotFoundError as exc:
        raise ReleaseError("GitHub CLI is unavailable") from exc
    if version.returncode:
        raise ReleaseError("GitHub CLI is unavailable")
    auth = run_gh("auth", "status", "--hostname", "github.com")
    if auth.returncode:
        raise ReleaseError("GitHub CLI is not authenticated for github.com")


def remote_main_sha() -> str:
    data = gh_json("api", f"repos/{GITHUB_REPOSITORY}/commits/{EXPECTED_BRANCH}")
    assert data is not None
    sha = data.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("GitHub main did not return a valid commit SHA")
    return sha


def local_head() -> str:
    return run_git("rev-parse", "HEAD", capture_output=True).stdout.strip()


def local_tag_target(tag: str) -> str | None:
    result = subprocess.run(
        git_args("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise ReleaseError(f"Could not inspect local tag {tag}: {result.stderr.strip()}")
    return result.stdout.strip()


def remote_tag_target(tag: str) -> str | None:
    result = run_gh("api", f"repos/{GITHUB_REPOSITORY}/commits/{tag}")
    if result.returncode:
        if "404" in result.stderr:
            return None
        raise ReleaseError(f"Could not inspect remote tag {tag}: {result.stderr.strip()}")
    try:
        sha = json.loads(result.stdout).get("sha")
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"Remote tag {tag} returned invalid JSON: {exc}") from exc
    if not isinstance(sha, str):
        raise ReleaseError(f"Remote tag {tag} returned no commit SHA")
    return sha


def remote_release(tag: str) -> dict[str, Any] | None:
    return gh_json("api", f"repos/{GITHUB_REPOSITORY}/releases/tags/{tag}", allow_missing=True)


def download_asset(metadata: ReleaseMetadata, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    result = run_gh(
        "release",
        "download",
        metadata.tag,
        "--repo",
        GITHUB_REPOSITORY,
        "--pattern",
        metadata.asset_name,
        "--dir",
        str(destination),
        "--clobber",
    )
    if result.returncode:
        raise ReleaseError(f"Could not download release asset: {result.stderr.strip()}")
    asset_path = destination / metadata.asset_name
    if not asset_path.is_file():
        raise ReleaseError(f"Downloaded release asset is missing: {metadata.asset_name}")
    return asset_path


def verify_remote_release(metadata: ReleaseMetadata, target_sha: str, archive: ArchiveInfo, scratch: Path) -> str:
    tag_target = remote_tag_target(metadata.tag)
    if tag_target != target_sha:
        raise ReleaseError(f"Remote tag {metadata.tag} targets {tag_target}, expected {target_sha}")
    release = remote_release(metadata.tag)
    if release is None:
        raise ReleaseError(f"GitHub release {metadata.tag} does not exist")
    if release.get("tag_name") != metadata.tag:
        raise ReleaseError("GitHub release tag name does not match the changelog")
    if release.get("name") != metadata.title:
        raise ReleaseError("GitHub release title does not match the changelog")
    if normalize_newlines(str(release.get("body", ""))) != metadata.notes:
        raise ReleaseError("GitHub release notes do not match the changelog")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ReleaseError("GitHub release must be public, non-draft, and non-prerelease")
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("name") != metadata.asset_name:
        raise ReleaseError("GitHub release must contain exactly the curated asset")
    asset_size = assets[0].get("size")
    if asset_size != archive.path.stat().st_size:
        raise ReleaseError("GitHub release asset size does not match the deterministic archive")
    downloaded = download_asset(metadata, scratch / "download")
    digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
    if digest != archive.sha256:
        raise ReleaseError(f"Downloaded asset digest {digest} does not match {archive.sha256}")
    return str(release.get("html_url") or release.get("url") or metadata.tag)


def check_remote_target(metadata: ReleaseMetadata, target_sha: str, archive: ArchiveInfo, scratch: Path) -> bool:
    local_target = local_tag_target(metadata.tag)
    if local_target is not None and local_target != target_sha:
        raise ReleaseError(f"Local tag {metadata.tag} targets {local_target}, expected {target_sha}")
    remote_target = remote_tag_target(metadata.tag)
    release = remote_release(metadata.tag)
    if remote_target is None and release is None:
        if local_target is not None:
            raise ReleaseError(f"Local tag {metadata.tag} exists while the remote target is absent")
        return False
    if remote_target is None or release is None:
        raise ReleaseError(f"Remote tag/release state for {metadata.tag} is an unrecoverable partial success")
    if remote_target != target_sha:
        raise ReleaseError(f"Remote tag {metadata.tag} targets {remote_target}, expected {target_sha}")
    verify_remote_release(metadata, target_sha, archive, scratch)
    return True


def check_repository_basics() -> tuple[str, str]:
    status = run_git("status", "--porcelain", capture_output=True).stdout.strip()
    if status:
        raise ReleaseError("Repository worktree and index must be clean before publication")
    branch = run_git("branch", "--show-current", capture_output=True).stdout.strip()
    if branch != EXPECTED_BRANCH:
        raise ReleaseError(f"Expected branch {EXPECTED_BRANCH}, found {branch or '(detached HEAD)'}")
    remote = run_git("remote", "get-url", "origin", capture_output=True).stdout.strip()
    if normalize_remote(remote).casefold() != GITHUB_REPOSITORY.casefold():
        raise ReleaseError(f"Unexpected origin remote: {remote}")
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        run_git("var", identity)
    diff_check = run_git("diff", "--check", capture_output=True)
    if diff_check.returncode:
        raise ReleaseError("Git whitespace validation failed")
    require_github_cli()
    remote_sha = remote_main_sha()
    current = local_head()
    return current, remote_sha


def parse_commit_paths(commit: str = "HEAD") -> tuple[str, ...]:
    output = run_git("diff-tree", "--no-commit-id", "--name-only", "-r", commit, capture_output=True).stdout
    return tuple(sorted(line for line in output.splitlines() if line))


def verify_completed_bump(metadata: ReleaseMetadata, remote_sha: str, versions: tuple[str, str]) -> bool:
    next_version = bump_version(metadata.version)
    if versions != (next_version, next_version):
        return False
    current = local_head()
    parents = run_git("rev-list", "--parents", "-n", "1", current, capture_output=True).stdout.split()
    if len(parents) != 2 or parents[1] != remote_sha:
        return False
    subject = run_git("log", "-1", "--format=%s", capture_output=True).stdout.strip()
    if subject != f"[CHORE] Bump version to {next_version}":
        return False
    if parse_commit_paths(current) != tuple(sorted(path.as_posix() for path in VERSION_PATHS)):
        return False
    return True


def update_versions(next_version: str) -> None:
    pyproject_path = PROJECT_ROOT / PYPROJECT_PATH
    pyproject = pyproject_path.read_text(encoding="utf-8")
    sections = list(re.finditer(r"(?m)^\[([^]]+)\]\s*$", pyproject))
    project_sections = [match for match in sections if match.group(1) == "project"]
    if len(project_sections) != 1:
        raise ReleaseError("Cannot update pyproject.toml: [project] section is ambiguous")
    section_start = project_sections[0].end()
    section_end = next((match.start() for match in sections if match.start() > section_start), len(pyproject))
    section = pyproject[section_start:section_end]
    updated, count = re.subn(
        r'(?m)^(version\s*=\s*")([^"]+)("\s*)$',
        rf"\g<1>{next_version}\g<3>",
        section,
    )
    if count != 1:
        raise ReleaseError("Expected exactly one pyproject.toml project version for update")
    pyproject_path.write_text(pyproject[:section_start] + updated + pyproject[section_end:], encoding="utf-8", newline="\n")

    init_path = PROJECT_ROOT / INIT_PATH
    init = init_path.read_text(encoding="utf-8")
    updated_init, count = re.subn(
        r'(?m)^(__version__\s*=\s*")([^"]+)("\s*)$',
        rf"\g<1>{next_version}\g<3>",
        init,
    )
    if count != 1:
        raise ReleaseError("Expected exactly one __version__ value for update")
    init_path.write_text(updated_init, encoding="utf-8", newline="\n")
    if read_versions() != (next_version, next_version):
        raise ReleaseError("Version authorities disagree after the patch bump")


def commit_bump(next_version: str, target_sha: str) -> None:
    version_paths = [path.as_posix() for path in VERSION_PATHS]
    run_git("add", "--", *version_paths)
    run_git("commit", "--only", "-m", f"[CHORE] Bump version to {next_version}", "--", *version_paths)
    if parse_commit_paths() != tuple(sorted(version_paths)):
        raise ReleaseError("Version bump commit contains unexpected paths")
    parents = run_git("rev-list", "--parents", "-n", "1", "HEAD", capture_output=True).stdout.split()
    if len(parents) != 2 or parents[1] != target_sha:
        raise ReleaseError("Version bump commit does not directly follow the published target")
    if run_git("status", "--porcelain", capture_output=True).stdout.strip():
        raise ReleaseError("Repository is not clean after the version bump")
    counts = run_git("rev-list", "--left-right", "--count", "origin/main...HEAD", capture_output=True).stdout.split()
    if counts != ["0", "1"]:
        raise ReleaseError(f"Expected exactly one unpushed bump commit, got {counts}")


def print_archive(info: ArchiveInfo) -> None:
    print(json.dumps({
        "asset": info.path.name,
        "sha256": info.sha256,
        "size": info.path.stat().st_size,
        "entries": [entry.__dict__ for entry in info.files],
    }, indent=2, sort_keys=True))


def prepare_context(metadata: ReleaseMetadata, ref: str | None, scratch: Path) -> ArchiveInfo:
    validate_public_tree(ref)
    validate_python_sources(ref)
    validate_powershell_sources(ref)
    archive = build_archive(metadata, ref, scratch / metadata.asset_name)
    return archive


def publish(metadata: ReleaseMetadata, target_sha: str, archive: ArchiveInfo, scratch: Path, recovery: bool) -> str:
    if not recovery:
        notes_path = scratch / "release-notes.md"
        notes_path.write_text(metadata.notes + "\n", encoding="utf-8", newline="\n")
        command = [
            "release",
            "create",
            metadata.tag,
            str(archive.path),
            "--repo",
            GITHUB_REPOSITORY,
            "--target",
            target_sha,
            "--title",
            metadata.title,
            "--notes-file",
            str(notes_path),
        ]
        result = run_gh(*command)
        if result.returncode:
            raise ReleaseError(f"Release publication failed: {result.stderr.strip()}")
        if result.stdout.strip():
            print(result.stdout.strip())
        try:
            return verify_remote_release(metadata, target_sha, archive, scratch)
        except ReleaseError as exc:
            raise PartialSuccess(f"Release was created, but readback verification failed: {exc}") from exc
    return verify_remote_release(metadata, target_sha, archive, scratch)


def execute(mode: str) -> int:
    metadata = parse_changelog(tree_text(None, CHANGELOG_PATH.as_posix()))
    versions = read_versions()
    if versions not in {(metadata.version, metadata.version), (bump_version(metadata.version), bump_version(metadata.version))}:
        raise ReleaseError(
            f"Version mismatch: changelog={metadata.version}, pyproject={versions[0]}, __version__={versions[1]}"
        )
    current, remote_sha = check_repository_basics()
    completed_bump = versions != (metadata.version, metadata.version)
    if not completed_bump and metadata.date != dt.date.today().isoformat():
        raise ReleaseError(
            f"Changelog date {metadata.date} does not equal the local publishing date {dt.date.today().isoformat()}"
        )
    if completed_bump:
        if not verify_completed_bump(metadata, remote_sha, versions):
            raise ReleaseError("Version authorities look bumped, but the exact isolated bump state is not present")
        target_sha = remote_sha
    else:
        if current != remote_sha:
            raise ReleaseError(f"Local HEAD {current} is not published at origin/{EXPECTED_BRANCH} ({remote_sha})")
        target_sha = current
    target_metadata = parse_changelog(tree_text(target_sha, CHANGELOG_PATH.as_posix()))
    target_versions = read_versions(target_sha)
    if target_metadata != metadata or target_versions != (metadata.version, metadata.version):
        raise ReleaseError("Published target tree does not contain the intended release metadata")

    with tempfile.TemporaryDirectory(prefix="blds-release-") as temporary:
        scratch = Path(temporary)
        archive = prepare_context(metadata, target_sha, scratch)
        print(f"Release: {metadata.tag} ({metadata.date})")
        print(f"Target: {target_sha}")
        print_archive(archive)

        remote_exists = check_remote_target(metadata, target_sha, archive, scratch)
        if mode == "prepare":
            if completed_bump:
                raise ReleaseError("Cannot prepare a new release after the local post-release bump")
            if remote_exists:
                raise ReleaseError("The intended release already exists; preparation requires an absent target")
            print("PREPARED: no tag or release exists; no publication or version change was made.")
            return 0
        if mode == "inspect":
            print("INSPECTED: local metadata and deterministic archive are valid.")
            return 0
        if mode == "verify":
            if not remote_exists:
                raise ReleaseError("The intended release does not exist")
            if completed_bump:
                counts = run_git("rev-list", "--left-right", "--count", "origin/main...HEAD", capture_output=True).stdout.split()
                if counts != ["0", "1"]:
                    raise ReleaseError("Completed release does not leave exactly one unpushed bump commit")
            print("VERIFIED: GitHub release, downloaded asset, and local release state match.")
            return 0
        if mode == "recover" and not remote_exists:
            raise ReleaseError("Recovery requires an exact existing remote publication")

        if completed_bump:
            if not remote_exists:
                raise ReleaseError("The completed local bump has no matching remote release")
            print("SUCCESS: exact post-release bump already exists; no new publication was attempted.")
            return 0

        release_url = publish(metadata, target_sha, archive, scratch, recovery=remote_exists)
        try:
            next_version = bump_version(metadata.version)
            update_versions(next_version)
            commit_bump(next_version, target_sha)
        except (ReleaseError, OSError, subprocess.CalledProcessError) as exc:
            raise PartialSuccess(f"Release {release_url} is published, but the local bump failed: {exc}") from exc
        print(f"Published: {release_url}")
        print(f"Local version bump: {next_version} (not pushed)")
        return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true", help="Validate and build without publishing")
    modes.add_argument("--publish", action="store_true", help="Publish or exactly recover the intended release")
    modes.add_argument("--verify", action="store_true", help="Verify the existing release and local post-release state")
    modes.add_argument("--inspect", action="store_true", help="Inspect local metadata and the deterministic archive")
    modes.add_argument("--recover", action="store_true", help="Recover an exact partial publication")
    args = parser.parse_args(argv)
    mode = "prepare" if args.prepare else "verify" if args.verify else "inspect" if args.inspect else "recover" if args.recover else "publish"
    try:
        if sys.version_info[:2] != (3, 12):
            raise ReleaseError(f"Configured Python 3.12 is required; found {sys.version.split()[0]}")
        return execute(mode)
    except PartialSuccess as exc:
        print(f"PARTIAL SUCCESS: {exc}", file=sys.stderr)
        return PARTIAL_SUCCESS
    except (ReleaseError, FileNotFoundError, OSError, subprocess.CalledProcessError) as exc:
        if mode in {"publish", "recover"}:
            print(f"PREPUBLICATION FAILURE: {exc}", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return PREPUBLICATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())

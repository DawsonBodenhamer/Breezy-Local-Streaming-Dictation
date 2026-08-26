"""Versioned standalone engine for normalized direct GitHub Releases."""

from __future__ import annotations

import datetime as dt
import base64
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

ENGINE_VERSION = "1.0.0"
CONFIG_SCHEMA = "github-release-publisher/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
CHANGELOG_PATH = Path("CHANGELOG.md")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
HEADING = re.compile(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\] - (Unreleased|[0-9]{4}-[0-9]{2}-[0-9]{2})$")
ALLOWED_CATEGORIES = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}
PREAMBLE = (
    "# Changelog\n\n"
    "All notable changes to this project will be documented in this file.\n\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),\n"
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
)


class ReleaseError(RuntimeError):
    """Fail-closed release error."""


class PartialSuccess(ReleaseError):
    """Remote publication succeeded but local completion is unfinished."""


class Adapter(Protocol):
    VERSION_PATHS: tuple[str, ...]

    def read_versions(self, context: "Context", ref: str | None = None) -> dict[str, str]: ...
    def validate(self, context: "Context", ref: str | None = None) -> None: ...
    def artifact_name(self, version: str) -> str: ...
    def build_artifact(self, context: "Context", ref: str, version: str, output: Path) -> tuple[dict[str, Any], ...]: ...
    def update_versions(self, context: "Context", next_version: str) -> None: ...


@dataclass(frozen=True)
class Changelog:
    version: str
    state: str
    notes: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def date(self) -> str | None:
        return None if self.state == "Unreleased" else self.state


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    sha256: str
    size: int
    manifest: tuple[dict[str, Any], ...]


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def bump_patch(version: str) -> str:
    if not SEMVER.fullmatch(version):
        raise ReleaseError(f"Invalid stable semantic version: {version}")
    major, minor, patch = (int(value) for value in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def parse_changelog(text: str) -> Changelog:
    text = normalize_newlines(text)
    if not text.startswith(PREAMBLE):
        raise ReleaseError("CHANGELOG.md does not match the normalized preamble")
    lines = text.rstrip("\n").split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        raise ReleaseError("CHANGELOG.md has no version sections")
    sections: list[tuple[str, str]] = []
    first_body: list[str] = []
    for position, start in enumerate(starts):
        match = HEADING.fullmatch(lines[start])
        if not match:
            raise ReleaseError(f"Invalid changelog heading: {lines[start]}")
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        body = lines[start + 1 : end]
        while body and body[-1] == "":
            body.pop()
        if not body or body[-1] != "---":
            raise ReleaseError(f"Changelog section {match.group(1)} lacks an exact --- divider")
        content = body[:-1]
        invalid_categories = [line[4:] for line in content if line.startswith("### ") and line[4:] not in ALLOWED_CATEGORIES]
        if invalid_categories:
            raise ReleaseError(f"Invalid changelog categories: {', '.join(invalid_categories)}")
        if any(line and not line.startswith(("### ", "- ", "  ")) for line in content):
            raise ReleaseError(f"Invalid changelog bullet formatting in {match.group(1)}")
        sections.append(match.groups())
        if position == 0:
            first_body = content
    versions = [tuple(int(part) for part in version.split(".")) for version, _ in sections]
    if versions != sorted(versions, reverse=True) or len(versions) != len(set(versions)):
        raise ReleaseError("Changelog versions must be unique and newest-first")
    unreleased = [entry for entry in sections if entry[1] == "Unreleased"]
    if len(unreleased) > 1 or (unreleased and sections[0] != unreleased[0]):
        raise ReleaseError("Only the newest changelog section may be Unreleased")
    while first_body and first_body[0] == "":
        first_body.pop(0)
    while first_body and first_body[-1] == "":
        first_body.pop()
    return Changelog(sections[0][0], sections[0][1], "\n".join(first_body))


def replace_top_state(text: str, version: str, old: str, new: str) -> str:
    text = normalize_newlines(text)
    source = f"## [{version}] - {old}"
    target = f"## [{version}] - {new}"
    if text.count(source) != 1:
        raise ReleaseError(f"Expected exactly one changelog heading {source}")
    return text.replace(source, target, 1)


def open_next_unreleased(text: str, released: Changelog, next_version: str) -> str:
    if released.date is None:
        raise ReleaseError("Cannot open the next section before release finalization")
    text = normalize_newlines(text)
    anchor = f"## [{released.version}] - {released.date}"
    insertion = f"## [{next_version}] - Unreleased\n\n---\n\n{anchor}"
    if text.count(anchor) != 1:
        raise ReleaseError(f"Expected exactly one released heading {anchor}")
    return text.replace(anchor, insertion, 1)


def load_config() -> dict[str, Any]:
    path = TOOLS_ROOT / "release_config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Could not load {path}: {exc}") from exc
    required = {
        "schema", "engine_version", "github_repository", "protected_branch", "adapter_module",
        "wrapper_command", "version_authority_description", "artifact_names", "test_visibility",
    }
    if set(config) != required:
        raise ReleaseError(f"release_config.json keys differ from schema: {sorted(set(config) ^ required)}")
    if config["schema"] != CONFIG_SCHEMA or config["engine_version"] != ENGINE_VERSION:
        raise ReleaseError("Publisher configuration schema or engine version is unsupported")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["github_repository"]):
        raise ReleaseError("Invalid GitHub repository identifier")
    return config


class Context:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root = PROJECT_ROOT

    def git(self, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            ["git", "-c", f"safe.directory={self.root}", *args], cwd=self.root,
            check=check, capture_output=True, text=text,
        )

    def git_text(self, *args: str) -> str:
        return self.git(*args).stdout.strip()

    def tree_bytes(self, ref: str | None, relative: str) -> bytes:
        if ref:
            return self.git("show", f"{ref}:{relative}", text=False).stdout
        return (self.root / relative).read_bytes()

    def tree_text(self, ref: str | None, relative: str) -> str:
        try:
            return self.tree_bytes(ref, relative).decode("utf-8")
        except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
            raise ReleaseError(f"Could not read UTF-8 path {relative}: {exc}") from exc

    def tracked_paths(self, ref: str | None = None) -> tuple[str, ...]:
        args = ("ls-tree", "-r", "--name-only", "-z", ref) if ref else ("ls-files", "-z")
        raw = self.git(*args, text=False).stdout
        return tuple(sorted(item.decode("utf-8") for item in raw.split(b"\0") if item))

    def today(self) -> str:
        if os.environ.get("RELEASE_PUBLISHER_TEST_MODE") == "1" and os.environ.get("RELEASE_PUBLISHER_TEST_DATE"):
            return dt.date.fromisoformat(os.environ["RELEASE_PUBLISHER_TEST_DATE"]).isoformat()
        return dt.date.today().isoformat()

    def github_command(self) -> list[str]:
        override = os.environ.get("RELEASE_PUBLISHER_GITHUB_COMMAND")
        if override:
            if os.environ.get("RELEASE_PUBLISHER_TEST_MODE") != "1":
                raise ReleaseError("GitHub command override is restricted to test mode")
            value = json.loads(override)
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                raise ReleaseError("Invalid test GitHub command override")
            return value
        return ["gh"]

    def gh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.github_command() + list(args), cwd=self.root, check=check, capture_output=True, text=True)


def load_adapter(config: dict[str, Any]) -> Adapter:
    if str(TOOLS_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOLS_ROOT))
    module = importlib.import_module(config["adapter_module"])
    required = ("VERSION_PATHS", "read_versions", "validate", "artifact_name", "build_artifact", "update_versions")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise ReleaseError(f"Release adapter lacks required members: {', '.join(missing)}")
    return module  # type: ignore[return-value]


def normalize_remote(value: str) -> str:
    value = value.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    return value.split("github.com/", 1)[1] if "github.com/" in value else value


def repository_basics(context: Context, allow_ahead: bool = False) -> tuple[str, str]:
    if context.git_text("branch", "--show-current") != context.config["protected_branch"]:
        raise ReleaseError(f"Expected branch {context.config['protected_branch']}")
    remote = context.git_text("remote", "get-url", "origin")
    if os.environ.get("RELEASE_PUBLISHER_TEST_MODE") != "1" and normalize_remote(remote).casefold() != context.config["github_repository"].casefold():
        raise ReleaseError(f"Unexpected origin remote: {remote}")
    context.git("var", "GIT_AUTHOR_IDENT")
    status = context.git_text("status", "--porcelain")
    if status:
        raise ReleaseError("Repository worktree and index must be clean")
    local = context.git_text("rev-parse", "HEAD")
    remote_line = context.git_text("ls-remote", "origin", f"refs/heads/{context.config['protected_branch']}")
    remote_sha = remote_line.split()[0] if remote_line else ""
    if not remote_sha:
        raise ReleaseError("Could not resolve protected remote branch")
    if not allow_ahead and local != remote_sha:
        raise ReleaseError(f"Local HEAD {local} does not equal remote {remote_sha}")
    return local, remote_sha


def exact_finalization_chain(context: Context, remote_sha: str, local_sha: str) -> bool:
    if remote_sha == local_sha:
        return True
    ancestor = context.git("merge-base", "--is-ancestor", remote_sha, local_sha, check=False)
    if ancestor.returncode:
        return False
    paths = context.git_text("diff", "--name-only", f"{remote_sha}..{local_sha}").splitlines()
    return bool(paths) and set(paths) == {CHANGELOG_PATH.as_posix()}


def tag_target(context: Context, tag: str) -> str | None:
    result = context.gh("api", f"repos/{context.config['github_repository']}/git/ref/tags/{tag}", "--jq", ".object.sha", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if "404" in (result.stderr + result.stdout):
        return None
    raise ReleaseError(f"Could not inspect tag {tag}: {result.stderr.strip()}")


def release_data(context: Context, tag: str) -> dict[str, Any] | None:
    result = context.gh("api", f"repos/{context.config['github_repository']}/releases/tags/{tag}", check=False)
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"Invalid GitHub release response: {exc}") from exc
    if "404" in (result.stderr + result.stdout):
        return None
    raise ReleaseError(f"Could not inspect release {tag}: {result.stderr.strip()}")


def artifact_from_adapter(context: Context, adapter: Adapter, release: Changelog, ref: str, scratch: Path) -> Artifact:
    name = adapter.artifact_name(release.version)
    configured = [pattern.format(version=release.version) for pattern in context.config["artifact_names"]]
    if [name] != configured:
        raise ReleaseError(f"Adapter artifact {name} disagrees with configured artifacts {configured}")
    path = scratch / name
    manifest = adapter.build_artifact(context, ref, release.version, path)
    if not path.is_file():
        raise ReleaseError(f"Adapter did not create artifact {path}")
    return Artifact(path, name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, manifest)


def verify_remote(context: Context, release: Changelog, target_sha: str, artifact: Artifact, scratch: Path) -> str:
    if release.date is None:
        raise ReleaseError("Cannot verify an unreleased changelog section")
    target = tag_target(context, release.tag)
    data = release_data(context, release.tag)
    if target is None and data is None:
        raise ReleaseError(f"Release {release.tag} is absent")
    if target != target_sha or data is None:
        raise ReleaseError("ambiguous-write: tag and release do not match the intended target")
    expected_title = f"v{release.version} - {release.date}"
    if data.get("draft") or data.get("prerelease") or data.get("name") != expected_title:
        raise ReleaseError("ambiguous-write: release visibility or title differs")
    if normalize_newlines(str(data.get("body", ""))).strip() != normalize_newlines(release.notes).strip():
        raise ReleaseError("ambiguous-write: release notes differ")
    assets = data.get("assets") or []
    if len(assets) != 1 or assets[0].get("name") != artifact.name or int(assets[0].get("size", -1)) != artifact.size:
        raise ReleaseError("ambiguous-write: release artifact metadata differs")
    destination = scratch / "downloaded"
    destination.mkdir()
    context.gh("release", "download", release.tag, "--repo", context.config["github_repository"], "--pattern", artifact.name, "--dir", str(destination))
    downloaded = destination / artifact.name
    if not downloaded.is_file() or hashlib.sha256(downloaded.read_bytes()).hexdigest() != artifact.sha256:
        raise ReleaseError("ambiguous-write: downloaded artifact digest differs")
    return str(data.get("html_url") or data.get("url") or "")


def print_result(state: str, release: Changelog, sha: str, artifact: Artifact | None = None, url: str = "", next_command: str = "") -> None:
    print(f"transaction_state={state}")
    print(f"release_sha={sha}")
    print(f"release_tag={release.tag}")
    if url:
        print(f"release_url={url}")
    if artifact:
        print(f"artifact={artifact.name} size={artifact.size} sha256={artifact.sha256}")
    if next_command:
        print(f"safe_next_command={next_command}")


def finalize(context: Context, release: Changelog, date: str) -> Changelog:
    path = context.root / CHANGELOG_PATH
    updated = replace_top_state(path.read_text(encoding="utf-8"), release.version, release.state, date)
    path.write_text(updated, encoding="utf-8", newline="\n")
    context.git("add", "--", CHANGELOG_PATH.as_posix())
    context.git("commit", "--only", "-m", f"[CHORE] Finalize v{release.version} release", "--", CHANGELOG_PATH.as_posix())
    committed = context.git_text("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    if committed != [CHANGELOG_PATH.as_posix()]:
        raise ReleaseError(f"Finalization commit has unexpected paths: {committed}")
    return parse_changelog(path.read_text(encoding="utf-8"))


def push_exact(context: Context, sha: str) -> None:
    destination = f"{sha}:refs/heads/{context.config['protected_branch']}"
    context.git("push", "origin", destination)
    remote_line = context.git_text("ls-remote", "origin", f"refs/heads/{context.config['protected_branch']}")
    if not remote_line or remote_line.split()[0] != sha:
        raise ReleaseError("Exact finalization push did not establish the intended remote SHA")


def publish_github(context: Context, release: Changelog, sha: str, artifact: Artifact, scratch: Path) -> str:
    if release.date is None or not release.notes:
        raise ReleaseError("Publication requires dated, non-empty release notes")
    notes = scratch / "release-notes.md"
    notes.write_text(release.notes + "\n", encoding="utf-8", newline="\n")
    context.gh(
        "release", "create", release.tag, str(artifact.path),
        "--repo", context.config["github_repository"], "--target", sha,
        "--title", f"v{release.version} - {release.date}", "--notes-file", str(notes),
    )
    try:
        return verify_remote(context, release, sha, artifact, scratch)
    except ReleaseError as exc:
        raise PartialSuccess(str(exc)) from exc


def complete_local(context: Context, adapter: Adapter, release: Changelog, remote_sha: str) -> str:
    next_version = bump_patch(release.version)
    paths = (CHANGELOG_PATH.as_posix(), *adapter.VERSION_PATHS)
    if len(paths) != len(set(paths)):
        raise ReleaseError("Adapter version paths overlap changelog or each other")
    originals = {path: (context.root / path).read_bytes() for path in paths}
    journal = Path(context.git_text("rev-parse", "--git-path", "release-publisher-state.json"))
    if not journal.is_absolute():
        journal = context.root / journal
    journal.write_text(json.dumps({
        "schema": "github-release-publisher-local/v1",
        "starting_head": context.git_text("rev-parse", "HEAD"),
        "paths": {path: base64.b64encode(content).decode("ascii") for path, content in originals.items()},
    }, sort_keys=True), encoding="utf-8")
    committed = False
    try:
        changelog_path = context.root / CHANGELOG_PATH
        changelog_path.write_text(
            open_next_unreleased(changelog_path.read_text(encoding="utf-8"), release, next_version),
            encoding="utf-8", newline="\n",
        )
        adapter.update_versions(context, next_version)
        versions = adapter.read_versions(context)
        if not versions or set(versions.values()) != {next_version}:
            raise ReleaseError(f"Version authorities did not converge on {next_version}: {versions}")
        context.git("add", "--", *paths)
        context.git("commit", "--only", "-m", f"[CHORE] Bump version to {next_version}", "--", *paths)
        committed = True
        changed = set(context.git_text("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
        if changed != set(paths):
            raise ReleaseError(f"Bump commit has unexpected paths: {sorted(changed)}")
        if context.git_text("status", "--porcelain"):
            raise ReleaseError("Repository is not clean after local completion")
        if context.git_text("rev-parse", "HEAD^") != remote_sha:
            raise ReleaseError("Local bump is not exactly one commit after the release SHA")
        journal.unlink(missing_ok=True)
        return context.git_text("rev-parse", "HEAD")
    except Exception:
        if not committed:
            for path, content in originals.items():
                (context.root / path).write_bytes(content)
            context.git("restore", "--staged", "--", *paths, check=False)
            journal.unlink(missing_ok=True)
        raise


def recover_local_journal(context: Context, adapter: Adapter, mode: str) -> None:
    journal = Path(context.git_text("rev-parse", "--git-path", "release-publisher-state.json"))
    if not journal.is_absolute():
        journal = context.root / journal
    if not journal.is_file():
        return
    if mode != "recover":
        raise ReleaseError("Interrupted local completion is journaled; run --recover")
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        expected_paths = {CHANGELOG_PATH.as_posix(), *adapter.VERSION_PATHS}
        encoded = data["paths"]
        if data.get("schema") != "github-release-publisher-local/v1" or set(encoded) != expected_paths:
            raise ReleaseError("ambiguous-write: local recovery journal has unknown ownership")
        starting_head = data["starting_head"]
        current_head = context.git_text("rev-parse", "HEAD")
        if current_head == starting_head:
            status_paths = set(context.git_text("status", "--porcelain").splitlines())
            if any(not any(line.endswith(path) for path in expected_paths) for line in status_paths):
                raise ReleaseError("ambiguous-write: unrelated work exists beside the local recovery journal")
            for path, content in encoded.items():
                (context.root / path).write_bytes(base64.b64decode(content, validate=True))
            context.git("restore", "--staged", "--", *sorted(expected_paths), check=False)
            if context.git_text("status", "--porcelain"):
                raise ReleaseError("ambiguous-write: journal restoration did not produce a clean repository")
        else:
            parent = context.git_text("rev-parse", "HEAD^")
            changed = set(context.git_text("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
            if parent != starting_head or changed != expected_paths or context.git_text("status", "--porcelain"):
                raise ReleaseError("ambiguous-write: HEAD changed outside the journaled local completion")
        journal.unlink()
    except (KeyError, ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"ambiguous-write: invalid local recovery journal: {exc}") from exc


def execute(mode: str) -> int:
    config = load_config()
    context = Context(config)
    adapter = load_adapter(config)
    recover_local_journal(context, adapter, mode)
    local_sha, remote_sha = repository_basics(context, allow_ahead=True)
    release = parse_changelog((context.root / CHANGELOG_PATH).read_text(encoding="utf-8"))
    versions = adapter.read_versions(context)
    if not versions or set(versions.values()) != {release.version}:
        raise ReleaseError(f"Version mismatch: changelog={release.version}, authorities={versions}")
    adapter.validate(context)

    with tempfile.TemporaryDirectory(prefix="github-release-publisher-") as directory:
        scratch = Path(directory)
        if local_sha != remote_sha:
            parent = context.git_text("rev-parse", "HEAD^")
            changed = set(context.git_text("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
            expected_bump_paths = {CHANGELOG_PATH.as_posix(), *adapter.VERSION_PATHS}
            if parent == remote_sha and changed == expected_bump_paths and release.state == "Unreleased":
                released = parse_changelog(context.tree_text(remote_sha, CHANGELOG_PATH.as_posix()))
                if released.date is None or release.version != bump_patch(released.version):
                    raise ReleaseError("ambiguous-write: local bump versions do not follow the remote release")
                adapter.validate(context, remote_sha)
                artifact = artifact_from_adapter(context, adapter, released, remote_sha, scratch)
                url = verify_remote(context, released, remote_sha, artifact, scratch)
                print_result("complete", released, remote_sha, artifact, url)
                return 0
        if not exact_finalization_chain(context, remote_sha, local_sha):
            raise ReleaseError("remote-advanced: local and remote do not form an exact changelog-only finalization chain")
        if release.state == "Unreleased":
            if local_sha != remote_sha:
                raise ReleaseError("abandonment-required: an Unreleased section cannot be ahead of the remote release boundary")
            if mode in {"inspect", "prepare"}:
                artifact = artifact_from_adapter(context, adapter, release, local_sha, scratch)
                print_result("development", release, local_sha, artifact, next_command=f"{config['wrapper_command']} --publish")
                return 0
            if mode in {"verify", "recover"}:
                raise ReleaseError("No finalized or published release is available to verify or recover")
            if not release.notes:
                raise ReleaseError("The current Unreleased section has no user-visible release notes")
            release = finalize(context, release, context.today())
            local_sha = context.git_text("rev-parse", "HEAD")

        if local_sha != remote_sha:
            if mode not in {"publish", "recover"}:
                state = "stale-date" if release.date != context.today() else "finalized-local"
                print_result(state, release, local_sha, next_command=f"{config['wrapper_command']} --recover")
                return 0
            if not exact_finalization_chain(context, remote_sha, local_sha):
                raise ReleaseError("remote-advanced: finalization is not an exact fast-forward")
            push_exact(context, local_sha)
            remote_sha = local_sha

        if release.date != context.today() and tag_target(context, release.tag) is None and release_data(context, release.tag) is None:
            if mode not in {"publish", "recover"}:
                print_result("stale-date", release, remote_sha, next_command=f"{config['wrapper_command']} --recover")
                return 0
            release = finalize(context, release, context.today())
            local_sha = context.git_text("rev-parse", "HEAD")
            push_exact(context, local_sha)
            remote_sha = local_sha

        release_ref = remote_sha
        release_at_ref = parse_changelog(context.tree_text(release_ref, CHANGELOG_PATH.as_posix()))
        adapter.validate(context, release_ref)
        artifact = artifact_from_adapter(context, adapter, release_at_ref, release_ref, scratch)
        target = tag_target(context, release_at_ref.tag)
        existing = release_data(context, release_at_ref.tag)
        if target is None and existing is None:
            if mode in {"inspect", "prepare"}:
                print_result("finalized-remote", release_at_ref, release_ref, artifact, next_command=f"{config['wrapper_command']} --publish")
                return 0
            if mode == "verify":
                raise ReleaseError("Finalized release is not published")
            url = publish_github(context, release_at_ref, release_ref, artifact, scratch)
        else:
            url = verify_remote(context, release_at_ref, release_ref, artifact, scratch)

        if context.git_text("rev-parse", "HEAD") != release_ref:
            current = parse_changelog((context.root / CHANGELOG_PATH).read_text(encoding="utf-8"))
            expected_next = bump_patch(release_at_ref.version)
            versions = adapter.read_versions(context)
            changed = set(context.git_text("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
            expected_paths = {CHANGELOG_PATH.as_posix(), *adapter.VERSION_PATHS}
            if current.version != expected_next or current.state != "Unreleased" or set(versions.values()) != {expected_next} or changed != expected_paths:
                raise ReleaseError("ambiguous-write: local post-release state differs from the exact bump contract")
            print_result("complete", release_at_ref, release_ref, artifact, url)
            return 0
        if mode in {"inspect", "prepare", "verify"}:
            print_result("published-unbumped", release_at_ref, release_ref, artifact, url, f"{config['wrapper_command']} --recover")
            return 0
        try:
            complete_local(context, adapter, release_at_ref, release_ref)
        except Exception as exc:
            raise PartialSuccess(f"Release exists at {url}; local completion failed: {exc}") from exc
        print_result("complete", release_at_ref, release_ref, artifact, url)
        return 0


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("prepare", "inspect", "publish", "verify", "recover"):
        modes.add_argument(f"--{mode}", action="store_true")
    args = parser.parse_args(argv)
    mode = next(name for name in ("prepare", "inspect", "publish", "verify", "recover") if getattr(args, name))
    try:
        return execute(mode)
    except PartialSuccess as exc:
        print(f"PARTIAL SUCCESS: {exc}", file=sys.stderr)
        return 2
    except (ReleaseError, FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.stderr, str) else str(exc)
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Atomic, comment-preserving updates for Breezy's formatting settings."""

from __future__ import annotations

import argparse
import codecs
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Callable


FORMATTING_KEYS = frozenset(
    {
        "automatic_punctuation",
        "capitalize_new_paragraphs",
        "capitalize_new_lines",
    }
)


def _decode(raw: bytes) -> tuple[str, bytes]:
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    payload = raw[len(bom) :]
    return payload.decode("utf-8"), bom


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _updated_text(text: str, key: str, enabled: bool) -> str:
    parsed = tomllib.loads(text)
    formatting = parsed.get("formatting", {})
    if not isinstance(formatting, dict):
        raise ValueError("[formatting] must be a TOML table")
    if key in formatting and not isinstance(formatting[key], bool):
        raise ValueError(f"formatting.{key} must be true or false")

    lines = text.splitlines(keepends=True)
    newline = _newline(text)
    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*\[formatting\]\s*(?:#.*?)?(?:\r?\n)?", line):
            section_start = index
            continue
        if (
            section_start is not None
            and index > section_start
            and re.match(r"\s*\[[^\]]+\]", line)
        ):
            section_end = index
            break

    rendered = "true" if enabled else "false"
    if section_start is not None:
        assignment = re.compile(
            rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)"
            rf"(?P<value>true|false)(?P<tail>\s*(?:#.*?)?)"
            rf"(?P<eol>\r?\n)?$"
        )
        for index in range(section_start + 1, section_end):
            match = assignment.fullmatch(lines[index])
            if match is None:
                continue
            if match.group("value") == rendered:
                return text
            lines[index] = (
                match.group("prefix")
                + rendered
                + match.group("tail")
                + (match.group("eol") or "")
            )
            return "".join(lines)

        insertion = section_end
        while insertion > section_start + 1 and not lines[insertion - 1].strip():
            insertion -= 1
        if insertion > 0 and lines[insertion - 1] and not lines[insertion - 1].endswith(("\n", "\r")):
            lines[insertion - 1] += newline
        lines.insert(insertion, f"{key} = {rendered}{newline}")
        return "".join(lines)

    suffix = ""
    if text:
        suffix = "" if text.endswith(("\n", "\r")) else newline
        if not text.endswith(newline + newline):
            suffix += newline
    return (
        text
        + suffix
        + f"[formatting]{newline}"
        + f"{key} = {rendered}{newline}"
    )


def update_formatting_value(
    path: Path,
    key: str,
    enabled: bool,
    *,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> bool:
    """Atomically update one allowlisted boolean or leave the original untouched."""
    if key not in FORMATTING_KEYS:
        raise ValueError(f"unsupported formatting key: {key}")
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")

    original = path.read_bytes()
    text, bom = _decode(original)
    updated = _updated_text(text, key, enabled)
    tomllib.loads(updated)
    encoded = bom + updated.encode("utf-8")
    if encoded == original:
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--key", required=True, choices=sorted(FORMATTING_KEYS))
    parser.add_argument("--enabled", required=True, choices=("true", "false"))
    args = parser.parse_args()
    changed = update_formatting_value(
        args.config,
        args.key,
        args.enabled == "true",
    )
    print("updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

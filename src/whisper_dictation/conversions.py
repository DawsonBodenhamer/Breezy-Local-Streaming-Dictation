"""User-managed literal speech-to-text conversions."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SEPARATE_WORDS = "separate_words"
ANYWHERE = "anywhere"
MATCH_LOCATIONS = frozenset({SEPARATE_WORDS, ANYWHERE})


class ConversionValidationError(ValueError):
    """Raised when conversion data cannot be saved or used."""

    def __init__(
        self,
        errors: Iterable[str],
        *,
        field_errors: Mapping[str, str] | None = None,
    ) -> None:
        self.errors = tuple(errors)
        self.field_errors = dict(field_errors or {})
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ConversionRule:
    """Validated persisted representation of one literal conversion."""

    identifier: str
    source: str
    replacement: str
    match_location: str = SEPARATE_WORDS
    case_sensitive: bool = False
    order: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "source": self.source,
            "replacement": self.replacement,
            "match_location": self.match_location,
            "case_sensitive": self.case_sensitive,
            "order": self.order,
        }


def default_conversions_path() -> Path:
    """Return the per-user conversion file path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "breezy_local_streaming_dictation" / "text_conversions.json"
    return Path.home() / ".breezy_local_streaming_dictation" / "text_conversions.json"


def new_rule(
    source: str,
    replacement: str,
    *,
    match_location: str = SEPARATE_WORDS,
    case_sensitive: bool = False,
    order: int = 0,
    identifier: str | None = None,
) -> ConversionRule:
    """Build and validate a rule from manager form values."""
    rule = ConversionRule(
        identifier=identifier or uuid.uuid4().hex,
        source=source,
        replacement=replacement,
        match_location=match_location,
        case_sensitive=case_sensitive,
        order=order,
    )
    validate_rules((rule,))
    return rule


def _rule_from_mapping(raw: object, index: int) -> ConversionRule:
    if not isinstance(raw, dict):
        raise ConversionValidationError((f"Rule {index + 1} is not an object.",))

    required = {
        "id",
        "source",
        "replacement",
        "match_location",
        "case_sensitive",
        "order",
    }
    missing = sorted(required.difference(raw))
    if missing:
        raise ConversionValidationError(
            (f"Rule {index + 1} is missing required fields.",)
        )
    return ConversionRule(
        identifier=raw["id"],  # type: ignore[arg-type]
        source=raw["source"],  # type: ignore[arg-type]
        replacement=raw["replacement"],  # type: ignore[arg-type]
        match_location=raw["match_location"],  # type: ignore[arg-type]
        case_sensitive=raw["case_sensitive"],  # type: ignore[arg-type]
        order=raw["order"],  # type: ignore[arg-type]
    )


def validate_rule(rule: ConversionRule, *, label: str = "Rule") -> None:
    errors: list[str] = []
    field_errors: dict[str, str] = {}

    if not isinstance(rule.identifier, str) or not rule.identifier.strip():
        errors.append(f"{label} needs a stable identifier.")
    if not isinstance(rule.source, str) or not rule.source.strip():
        errors.append("Tell dictation what to hear.")
        field_errors["source"] = "Enter the words dictation should hear."
    if not isinstance(rule.replacement, str) or not rule.replacement.strip():
        errors.append("Tell dictation what to insert.")
        field_errors["replacement"] = "Enter the replacement text."
    if rule.match_location not in MATCH_LOCATIONS:
        errors.append("The match location is not supported.")
        field_errors["match_location"] = "Choose one of the matching options."
    if not isinstance(rule.case_sensitive, bool):
        errors.append("Case sensitivity must be true or false.")
        field_errors["case_sensitive"] = "Choose whether capitalization must match."
    if isinstance(rule.order, bool) or not isinstance(rule.order, int) or rule.order < 0:
        errors.append("Display order must be a non-negative number.")

    if errors:
        raise ConversionValidationError(errors, field_errors=field_errors)


def _duplicate_key(rule: ConversionRule) -> tuple[str, str, str, bool]:
    return (
        rule.source,
        rule.replacement,
        rule.match_location,
        rule.case_sensitive,
    )


def validate_rules(rules: Iterable[ConversionRule]) -> tuple[ConversionRule, ...]:
    """Validate a complete ordered rule set and return it as a tuple."""
    materialized = tuple(rules)
    identifiers: set[str] = set()
    orders: set[int] = set()
    duplicates: set[tuple[str, str, str, bool]] = set()
    errors: list[str] = []
    field_errors: dict[str, str] = {}

    for index, rule in enumerate(materialized):
        try:
            validate_rule(rule, label=f"Rule {index + 1}")
        except ConversionValidationError as error:
            errors.extend(error.errors)
            field_errors.update(error.field_errors)
        if isinstance(rule.identifier, str) and rule.identifier in identifiers:
            errors.append("Each conversion needs a different identifier.")
        elif isinstance(rule.identifier, str):
            identifiers.add(rule.identifier)
        if isinstance(rule.order, int) and not isinstance(rule.order, bool):
            if rule.order in orders:
                errors.append("Each conversion needs a different display order.")
            orders.add(rule.order)
        key = _duplicate_key(rule) if isinstance(rule, ConversionRule) else None
        if key is not None:
            if key in duplicates:
                errors.append("That conversion already exists.")
            duplicates.add(key)

    if errors:
        raise ConversionValidationError(errors, field_errors=field_errors)
    return materialized


def _payload_to_rules(payload: object) -> tuple[ConversionRule, ...]:
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise ConversionValidationError(("The saved conversions file has an unsupported format.",))
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise ConversionValidationError(("The saved conversions file has no valid rule list.",))
    rules = tuple(_rule_from_mapping(raw, index) for index, raw in enumerate(raw_rules))
    return validate_rules(rules)


def _rules_payload(rules: Iterable[ConversionRule]) -> dict[str, object]:
    validated = validate_rules(rules)
    return {
        "version": SCHEMA_VERSION,
        "rules": [rule.to_dict() for rule in validated],
    }


def _is_word_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def _casefold_with_boundaries(text: str) -> tuple[str, tuple[int, ...]]:
    """Return case-folded text and original-character boundary offsets."""
    folded_parts: list[str] = []
    boundaries = [0]
    folded_length = 0
    for character in text:
        folded = character.casefold()
        folded_parts.append(folded)
        folded_length += len(folded)
        boundaries.append(folded_length)
    return "".join(folded_parts), tuple(boundaries)


def _find_rule_matches(text: str, rule: ConversionRule) -> Iterable[tuple[int, int]]:
    if rule.case_sensitive:
        target = text
        source = rule.source
        boundaries = None
    else:
        target, boundaries = _casefold_with_boundaries(text)
        source = rule.source.casefold()

    if not source:
        return
    start = 0
    while True:
        found = target.find(source, start)
        if found < 0:
            return
        end = found + len(source)
        if boundaries is not None:
            if found not in boundaries or end not in boundaries:
                start = found + 1
                continue
            original_start = boundaries.index(found)
            original_end = boundaries.index(end)
        else:
            original_start = found
            original_end = end

        if rule.match_location == SEPARATE_WORDS:
            has_left_word = original_start > 0 and _is_word_character(text[original_start - 1])
            has_right_word = original_end < len(text) and _is_word_character(text[original_end])
            source_starts_word = _is_word_character(rule.source[0])
            source_ends_word = _is_word_character(rule.source[-1])
            if (source_starts_word and has_left_word) or (source_ends_word and has_right_word):
                start = found + 1
                continue
        yield original_start, original_end
        start = found + max(1, len(source))


def apply_conversions(text: str, rules: Iterable[ConversionRule]) -> str:
    """Apply ordered literal rules once without cascading replacements."""
    if not text:
        return text
    candidates: list[tuple[int, int, int, int, int, ConversionRule]] = []
    for rule_index, rule in enumerate(validate_rules(rules)):
        for start, end in _find_rule_matches(text, rule):
            candidates.append((start, end, -(end - start), rule.order, rule_index, rule))

    if not candidates:
        return text

    candidates.sort(key=lambda candidate: (candidate[0], candidate[2], candidate[3], candidate[4]))
    output: list[str] = []
    cursor = 0
    for start, end, _, _, _, rule in candidates:
        if start < cursor:
            continue
        output.append(text[cursor:start])
        output.append(rule.replacement)
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


class ConversionStore:
    """Thread-safe cached user rules with fail-safe external reloads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_conversions_path()
        self._lock = threading.RLock()
        self._rules: tuple[ConversionRule, ...] = ()
        self._mtime_ns: int | None = None
        self._last_error: str | None = None
        self._loaded = False

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _file_mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError:
            return -1

    def _read_current_file(self) -> tuple[ConversionRule, ...]:
        with self.path.open("r", encoding="utf-8") as handle:
            return _payload_to_rules(json.load(handle))

    def reload_if_changed(self) -> tuple[ConversionRule, ...]:
        with self._lock:
            marker = self._file_mtime()
            if self._loaded and marker == self._mtime_ns:
                return self._rules
            if marker is None:
                self._rules = ()
                self._mtime_ns = None
                self._last_error = None
                self._loaded = True
                return self._rules
            try:
                rules = self._read_current_file()
            except (OSError, ValueError, TypeError, ConversionValidationError, json.JSONDecodeError):
                self._mtime_ns = marker
                self._last_error = (
                    "The saved conversions file could not be loaded. "
                    "Your last valid conversions remain active."
                )
                self._loaded = True
                log.warning("Conversion file rejected; retaining last valid rules.")
                return self._rules
            self._rules = rules
            self._mtime_ns = marker
            self._last_error = None
            self._loaded = True
            return self._rules

    def rules(self) -> tuple[ConversionRule, ...]:
        return self.reload_if_changed()

    def apply(self, text: str) -> str:
        rules = self.reload_if_changed()
        return apply_conversions(text, rules)

    def save(self, rules: Iterable[ConversionRule]) -> tuple[ConversionRule, ...]:
        validated = validate_rules(rules)
        payload = json.dumps(_rules_payload(validated), ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        with self._lock:
            self._rules = validated
            self._mtime_ns = self._file_mtime()
            self._last_error = None
            self._loaded = True
        return validated

"""User-managed literal speech-to-text conversions."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
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
    """Validated representation of one correction with one or more heard phrases."""

    identifier: str
    sources: tuple[str, ...]
    replacement: str
    match_location: str = SEPARATE_WORDS
    case_sensitive: bool = False
    order: int = 0
    legacy: bool = False

    @property
    def source(self) -> str:
        """Return the first heard phrase for version-1 callers and summaries."""
        return self.sources[0] if self.sources else ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "sources": list(self.sources),
            "replacement": self.replacement,
            "match_location": self.match_location,
            "case_sensitive": self.case_sensitive,
            "order": self.order,
        }


def default_conversions_path() -> Path:
    """Return the per-user conversion file path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "breezy_dictation" / "text_conversions.json"
    return Path.home() / ".breezy_dictation" / "text_conversions.json"


def new_correction(
    sources: Iterable[str] | str,
    replacement: str,
    *,
    match_location: str = SEPARATE_WORDS,
    case_sensitive: bool = False,
    order: int = 0,
    identifier: str | None = None,
) -> ConversionRule:
    """Build and validate a correction from manager form values."""
    materialized_sources = (sources,) if isinstance(sources, str) else tuple(sources)
    rule = ConversionRule(
        identifier=identifier or uuid.uuid4().hex,
        sources=materialized_sources,
        replacement=replacement,
        match_location=match_location,
        case_sensitive=case_sensitive,
        order=order,
    )
    validate_rules((rule,))
    return rule


def new_rule(
    source: str,
    replacement: str,
    *,
    match_location: str = SEPARATE_WORDS,
    case_sensitive: bool = False,
    order: int = 0,
    identifier: str | None = None,
) -> ConversionRule:
    """Build a one-phrase correction for compatibility with version-1 callers."""
    return new_correction(
        (source,),
        replacement,
        match_location=match_location,
        case_sensitive=case_sensitive,
        order=order,
        identifier=identifier,
    )


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
        sources=(raw["source"],),  # type: ignore[arg-type]
        replacement=raw["replacement"],  # type: ignore[arg-type]
        match_location=raw["match_location"],  # type: ignore[arg-type]
        case_sensitive=raw["case_sensitive"],  # type: ignore[arg-type]
        order=raw["order"],  # type: ignore[arg-type]
        legacy=True,
    )


def _correction_from_mapping(raw: object, index: int) -> ConversionRule:
    if not isinstance(raw, dict):
        raise ConversionValidationError((f"Correction {index + 1} is not an object.",))
    required = {
        "id",
        "sources",
        "replacement",
        "match_location",
        "case_sensitive",
        "order",
    }
    if required.difference(raw):
        raise ConversionValidationError(
            (f"Correction {index + 1} is missing required fields.",)
        )
    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list):
        raise ConversionValidationError(
            (f"Correction {index + 1} needs a phrase list.",)
        )
    return ConversionRule(
        identifier=raw["id"],  # type: ignore[arg-type]
        sources=tuple(raw_sources),  # type: ignore[arg-type]
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
    if not isinstance(rule.sources, tuple) or not rule.sources:
        errors.append(f"{label} needs at least one phrase Breezy may hear.")
        field_errors["sources"] = "Add at least one phrase Breezy may hear."
    else:
        seen_sources: set[str] = set()
        for source in rule.sources:
            if not isinstance(source, str) or not source.strip():
                errors.append(f"{label} contains an empty phrase.")
                field_errors["sources"] = "Remove empty phrases."
                continue
            key = source
            if key in seen_sources:
                errors.append(f"{label} contains the same phrase more than once.")
                field_errors["sources"] = "Each phrase must be different."
            seen_sources.add(key)
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


def _duplicate_key(rule: ConversionRule, source: str) -> tuple[str, str, bool]:
    return (
        source,
        rule.match_location,
        rule.case_sensitive,
    )


def validate_rules(
    rules: Iterable[ConversionRule],
    *,
    allow_legacy_conflicts: bool = True,
) -> tuple[ConversionRule, ...]:
    """Validate a complete ordered rule set and return it as a tuple."""
    materialized = tuple(rules)
    identifiers: set[str] = set()
    orders: set[int] = set()
    duplicates: dict[tuple[str, str, bool], ConversionRule] = {}
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
        for source in rule.sources if isinstance(rule.sources, tuple) else ():
            if not isinstance(source, str):
                continue
            key = _duplicate_key(rule, source)
            previous = duplicates.get(key)
            if previous is not None and not (
                allow_legacy_conflicts and previous.legacy and rule.legacy
            ):
                errors.append(f'The phrase "{source}" belongs to more than one correction.')
                field_errors["sources"] = "Each phrase can belong to only one correction."
            duplicates[key] = rule

    if errors:
        raise ConversionValidationError(errors, field_errors=field_errors)
    return materialized


def _payload_to_rules(payload: object) -> tuple[ConversionRule, ...]:
    if not isinstance(payload, dict):
        raise ConversionValidationError(("The saved conversions file has an unsupported format.",))
    version = payload.get("version")
    if version == LEGACY_SCHEMA_VERSION:
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list):
            raise ConversionValidationError(("The saved conversions file has no valid rule list.",))
        rules = tuple(_rule_from_mapping(raw, index) for index, raw in enumerate(raw_rules))
        return validate_rules(rules, allow_legacy_conflicts=True)
    elif version == SCHEMA_VERSION:
        raw_corrections = payload.get("corrections")
        if not isinstance(raw_corrections, list):
            raise ConversionValidationError(("The saved corrections file has no valid correction list.",))
        rules = tuple(
            _correction_from_mapping(raw, index)
            for index, raw in enumerate(raw_corrections)
        )
    else:
        raise ConversionValidationError(("The saved conversions file has an unsupported format.",))
    return validate_rules(rules, allow_legacy_conflicts=False)


def _rules_payload(rules: Iterable[ConversionRule]) -> dict[str, object]:
    validated = validate_rules(rules, allow_legacy_conflicts=False)
    return {
        "version": SCHEMA_VERSION,
        "corrections": [rule.to_dict() for rule in validated],
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


def _find_rule_matches(
    text: str,
    rule: ConversionRule,
    source_text: str,
) -> Iterable[tuple[int, int]]:
    if rule.case_sensitive:
        target = text
        source = source_text
        boundaries = None
    else:
        target, boundaries = _casefold_with_boundaries(text)
        source = source_text.casefold()

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
            source_starts_word = _is_word_character(source_text[0])
            source_ends_word = _is_word_character(source_text[-1])
            if (source_starts_word and has_left_word) or (source_ends_word and has_right_word):
                start = found + 1
                continue
        yield original_start, original_end
        start = found + max(1, len(source))


def apply_conversions(
    text: str,
    rules: Iterable[ConversionRule],
    *,
    match_text: str | None = None,
) -> str:
    """Apply ordered literal rules once without cascading replacements."""
    if not text:
        return text
    matching_source = text if match_text is None else match_text
    if len(matching_source) != len(text):
        raise ValueError("Conversion match text must preserve output offsets.")
    candidates: list[tuple[int, int, int, int, int, int, ConversionRule]] = []
    for rule_index, rule in enumerate(validate_rules(rules)):
        for source_index, source in enumerate(rule.sources):
            for start, end in _find_rule_matches(matching_source, rule, source):
                candidates.append(
                    (start, end, -(end - start), rule.order, source_index, rule_index, rule)
                )

    if not candidates:
        return text

    candidates.sort(
        key=lambda candidate: (
            candidate[0],
            candidate[2],
            candidate[3],
            candidate[4],
            candidate[5],
        )
    )
    output: list[str] = []
    cursor = 0
    for start, end, _, _, _, _, rule in candidates:
        if start < cursor:
            continue
        output.append(text[cursor:start])
        output.append(rule.replacement)
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


def suggest_compatible_groups(
    rules: Iterable[ConversionRule],
) -> tuple[tuple[str, ...], ...]:
    """Return compatible one-phrase correction identifiers grouped for review."""
    validated = validate_rules(rules)
    buckets: dict[tuple[str, str, bool], list[ConversionRule]] = {}
    for rule in validated:
        if len(rule.sources) != 1:
            continue
        key = (rule.replacement, rule.match_location, rule.case_sensitive)
        buckets.setdefault(key, []).append(rule)
    suggestions = [
        tuple(rule.identifier for rule in sorted(group, key=lambda item: item.order))
        for group in buckets.values()
        if len(group) > 1
    ]
    return tuple(
        sorted(
            suggestions,
            key=lambda identifiers: min(
                rule.order for rule in validated if rule.identifier in identifiers
            ),
        )
    )


def organize_suggested_groups(
    rules: Iterable[ConversionRule],
    selected_groups: Iterable[Iterable[str]],
) -> tuple[ConversionRule, ...]:
    """Group confirmed compatible corrections without dropping any source."""
    validated = validate_rules(rules)
    by_id = {rule.identifier: rule for rule in validated}
    selected = tuple(tuple(identifiers) for identifiers in selected_groups)
    claimed: set[str] = set()
    grouped: list[ConversionRule] = []

    for identifiers in selected:
        if len(identifiers) < 2 or len(set(identifiers)) != len(identifiers):
            raise ConversionValidationError(
                ("A suggested group needs at least two different corrections.",)
            )
        try:
            members = [by_id[identifier] for identifier in identifiers]
        except KeyError as error:
            raise ConversionValidationError(
                ("A suggested correction no longer exists.",)
            ) from error
        if claimed.intersection(identifiers):
            raise ConversionValidationError(
                ("A correction cannot be organized into more than one group.",)
            )
        compatibility = {
            (member.replacement, member.match_location, member.case_sensitive)
            for member in members
        }
        if len(compatibility) != 1:
            raise ConversionValidationError(
                ("Only corrections with the same typed result and matching options can be grouped.",)
            )
        first = min(members, key=lambda item: item.order)
        grouped.append(
            new_correction(
                tuple(source for member in members for source in member.sources),
                first.replacement,
                match_location=first.match_location,
                case_sensitive=first.case_sensitive,
                order=first.order,
                identifier=first.identifier,
            )
        )
        claimed.update(identifiers)

    remaining = [rule for rule in validated if rule.identifier not in claimed]
    ordered = sorted((*remaining, *grouped), key=lambda item: item.order)
    normalized = tuple(
        replace(rule, order=index, legacy=False)
        for index, rule in enumerate(ordered)
    )
    return validate_rules(normalized, allow_legacy_conflicts=False)


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

    def apply(self, text: str, *, match_text: str | None = None) -> str:
        rules = self.reload_if_changed()
        return apply_conversions(text, rules, match_text=match_text)

    def save(self, rules: Iterable[ConversionRule]) -> tuple[ConversionRule, ...]:
        validated = validate_rules(rules, allow_legacy_conflicts=False)
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

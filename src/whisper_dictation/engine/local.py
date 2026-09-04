"""Local transcription engine using faster-whisper (no server needed)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass

import numpy as np

from ..config import EngineConfig, ServerConfig
from .base import TranscriptionEngine

log = logging.getLogger(__name__)

_PAUSE_TERMINATOR_RE = re.compile(r"(?:\.{1,}|…+)\s*$")
_REPETITION_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’_-][A-Za-z0-9]+)*")
_PATHOLOGICAL_REPEAT_CYCLES = 12
_PATHOLOGICAL_MULTIWORD_REPEAT_CYCLES = 4
_PATHOLOGICAL_MAX_CYCLE_WORDS = 8
_MAX_SEGMENT_OVERLAP_WORDS = 12
_PATHOLOGICAL_PUNCTUATION_RUN_LENGTH = 32
_LOW_DIVERSITY_PUNCTUATION_MIN_CHARS = 32
_LOW_DIVERSITY_SINGLE_CLASS_MIN_CHARS = 64
_LOW_DIVERSITY_MAX_DISTINCT_CHARS = 8
_REPETITION_GUARD_EXEMPT_WORDS = frozenset(
    {
        "all",
        "backtick",
        "cap",
        "capital",
        "caps",
        "close",
        "comma",
        "colon",
        "dot",
        "ellipsis",
        "exclamation",
        "hyphen",
        "line",
        "mark",
        "new",
        "off",
        "on",
        "open",
        "paragraph",
        "period",
        "question",
        "quote",
        "semicolon",
        "slash",
        "tilde",
        "underscore",
    }
)
BACKTICK_COMMAND = "\ue000"
OPEN_QUOTE_COMMAND = "\ue001"
CLOSE_QUOTE_COMMAND = "\ue002"
PENDING_CLOSE_COMMAND = "\ue003"
EXPLICIT_DOT_COMMAND = "\ue004"
EXPLICIT_COMMA_COMMAND = "\ue005"
EXPLICIT_QUESTION_COMMAND = "\ue006"
EXPLICIT_EXCLAMATION_COMMAND = "\ue007"
EXPLICIT_PERIOD_COMMAND = "\ue008"
EXPLICIT_SEMICOLON_COMMAND = "\ue009"
EXPLICIT_COLON_COMMAND = "\ue00a"
EXPLICIT_ELLIPSIS_COMMAND = "\ue00b"
SPOKEN_NEW_LINE_COMMAND = "\ue00c"
SPOKEN_NEW_PARAGRAPH_COMMAND = "\ue00d"
_NUMERAL_VALUES = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_NUMERAL_RE = re.compile(
    r"\bnumeral\s+(zero|one|two|three|four|five|six|seven|eight|nine|[0-9])\b",
    re.IGNORECASE,
)
_DIGIT_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:digit|digi)\s+"
    r"(zero|one|two|three|four|five|six|seven|eight|nine|[0-9])"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DIGIT_COMMAND_MARKERS = tuple(chr(0xE010 + value) for value in range(10))
_NUMBER_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_NUMBER_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORD_PATTERN = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|"
    r"four|five|six|seven|eight|nine))?)"
)
_NUMBER_COMPONENT_PATTERN = rf"(?:[0-9]+|{_NUMBER_WORD_PATTERN})"
_CARDINAL_SCALE_VALUES = {
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}
_CARDINAL_WORD_PATTERN = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|trillion|and)"
)
_CARDINAL_PHRASE_PATTERN = (
    rf"(?!and\b){_CARDINAL_WORD_PATTERN}(?:[-\s]+{_CARDINAL_WORD_PATTERN})*"
)
_CARDINAL_PHRASE_RE = re.compile(
    rf"(?<![\w-])(?P<value>{_CARDINAL_PHRASE_PATTERN})(?![\w-])",
    re.IGNORECASE,
)
_TRAILING_CARDINAL_RE = re.compile(
    rf"(?<![\w-])(?P<value>{_CARDINAL_PHRASE_PATTERN})"
    rf"(?P<suffix>\s*[.,!?;:…]*\s*)$",
    re.IGNORECASE,
)
_AMBIGUOUS_INTEGER_RE = re.compile(
    r"^\s*(?:\d{1,3}(?:,\d{3})+|\d+)[.!?]?\s*$"
)
_AMBIGUOUS_MIXED_INTEGER_RE = re.compile(
    r"(?<![\w.:])(?P<value>\d{3,4})(?![\w.:])"
)
_AMBIGUOUS_SINGLE_DIGIT_RUN_RE = re.compile(
    r"(?<![\w.])(?P<value>[0-9](?:[ \t]+[0-9]){2,})(?![\w.])"
)
_SPOKEN_SINGLE_DIGIT_RUN_RE = re.compile(
    r"(?<![\w-])(?P<value>(?:zero|one|two|three|four|five|six|seven|eight|nine)"
    r"(?:[ \t]+(?:zero|one|two|three|four|five|six|seven|eight|nine)){2,})(?![\w-])",
    re.IGNORECASE,
)
_INTEGER_FRAGMENT_RE = re.compile(
    r"^\s*(?P<value>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?P<suffix>[.!?]?)\s*$"
)
_MERIDIEM_PATTERN = (
    r"(?:a|ay|aye|p|pea|pee)\s*\.?\s*(?:m|em|emm)"
)
_VERSION_RE = re.compile(
    rf"(?<!\w)(?P<prefix>version|vee|v)\s*"
    rf"(?P<value>{_NUMBER_COMPONENT_PATTERN}"
    rf"(?:\s*(?:\.\s*|\s+point\s+){_NUMBER_COMPONENT_PATTERN})+)"
    rf"(?!\w|\.\s*[A-Za-z0-9]|\s+point\b)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    rf"(?<![\w.])(?P<hour>[0-9]{{1,2}}|{_NUMBER_WORD_PATTERN})"
    rf"(?:\s*[:.]\s*|\s+)"
    rf"(?P<minute>[0-9]{{1,2}}|oh[- ](?:zero|one|two|three|four|five|"
    rf"six|seven|eight|nine)|{_NUMBER_WORD_PATTERN})"
    rf"\s*(?P<period>{_MERIDIEM_PATTERN})"
    rf"(?!\w)",
    re.IGNORECASE,
)
_COMPACT_TIME_RE = re.compile(
    rf"(?<![\w.])(?P<value>[0-9]{{3,4}})"
    rf"\s*(?P<period>{_MERIDIEM_PATTERN})(?!\w)",
    re.IGNORECASE,
)
_BARE_TIME_RE = re.compile(
    rf"(?<![\w.])(?P<hour>[0-9]{{1,2}}|{_NUMBER_WORD_PATTERN})\s+"
    rf"(?P<minute>oh[- ](?:zero|one|two|three|four|five|six|seven|eight|nine)|"
    rf"{_NUMBER_WORD_PATTERN})(?![\w.]|\s+(?:hundred|thousand|million|billion|trillion)\b)",
    re.IGNORECASE,
)
_NUMBER_WORD_BEFORE_RE = re.compile(
    rf"(?<![\w-]){_CARDINAL_WORD_PATTERN}\s+$",
    re.IGNORECASE,
)
_NUMBER_WORD_AFTER_RE = re.compile(
    rf"^\s+{_CARDINAL_WORD_PATTERN}(?![\w-])",
    re.IGNORECASE,
)
_DOTTED_COMPONENT_PATTERN = rf"(?:{_NUMBER_COMPONENT_PATTERN}|[A-Za-z]|ex)"
_DOTTED_SEPARATOR_PATTERN = (
    rf"(?:{re.escape(EXPLICIT_DOT_COMMAND)}|\s+point\s*(?:\.\s*)?)"
)
_DOTTED_IDENTIFIER_RE = re.compile(
    rf"(?<![\w.])(?P<value>{_DOTTED_COMPONENT_PATTERN}"
    rf"(?:{_DOTTED_SEPARATOR_PATTERN}{_DOTTED_COMPONENT_PATTERN}){{2,}})"
    rf"(?!\w)",
    re.IGNORECASE,
)
_MARKDOWN_EXTENSION_RE = re.compile(
    rf"(?<!\w)(?P<base>[A-Za-z0-9_-]{{2,}})"
    rf"(?:{re.escape(EXPLICIT_DOT_COMMAND)}|\s+point\s*(?:\.\s*)?)"
    rf"m\s*d(?!\w)",
    re.IGNORECASE,
)
_OWNER_DOTTED_VERSION_RE = re.compile(
    r"(?<![\w.])1\s*\.\s*20\s*\.\s*x(?!\w)",
    re.IGNORECASE,
)
_SPOKEN_PUNCTUATION = (
    ("new paragraph", SPOKEN_NEW_PARAGRAPH_COMMAND),
    ("new line", SPOKEN_NEW_LINE_COMMAND),
    ("close quotation mark", CLOSE_QUOTE_COMMAND),
    ("open quotation mark", OPEN_QUOTE_COMMAND),
    ("close quote", CLOSE_QUOTE_COMMAND),
    ("open quote", OPEN_QUOTE_COMMAND),
    ("close parentheses", ")"),
    ("open parentheses", " ("),
    ("exclamation mark", EXPLICIT_EXCLAMATION_COMMAND),
    ("question mark", EXPLICIT_QUESTION_COMMAND),
    ("open parenthesis", " ("),
    ("close parenthesis", ")"),
    ("open bracket", " ["),
    ("close bracket", "]"),
    ("em dash", "—"),
    ("emdash", "—"),
    ("m-dash", "—"),
    ("m dash", "—"),
    ("mdash", "—"),
    ("quotation mark", '"'),
    ("dot dot dot", EXPLICIT_ELLIPSIS_COMMAND),
    ("ellipses", EXPLICIT_ELLIPSIS_COMMAND),
    ("ellipsis", EXPLICIT_ELLIPSIS_COMMAND),
    ("dot", EXPLICIT_DOT_COMMAND),
    ("forward slash", "/"),
    ("slash", "/"),
    ("back tick", BACKTICK_COMMAND),
    ("backtick", BACKTICK_COMMAND),
    ("backtic", BACKTICK_COMMAND),
    ("hyphen", "-"),
    ("tilde", " ~"),
    ("tildi", " ~"),
    ("underscore", "_"),
    ("quote", OPEN_QUOTE_COMMAND),
    ("semicolon", EXPLICIT_SEMICOLON_COMMAND),
    ("colon", EXPLICIT_COLON_COMMAND),
    ("period", EXPLICIT_PERIOD_COMMAND),
    ("comma", EXPLICIT_COMMA_COMMAND),
)


@dataclass(frozen=True)
class DecoderOutputMetrics:
    """Bounded, transcript-free diagnostics for rejected decoder output."""

    total_chars: int
    non_whitespace_chars: int
    punctuation_chars: int
    distinct_chars: int
    max_run_length: int
    fingerprint: str


class PathologicalDecoderOutput(RuntimeError):
    """Raised when decoder output is unsafe to normalize or inject."""

    def __init__(self, reason: str, metrics: DecoderOutputMetrics) -> None:
        super().__init__("pathological decoder output rejected")
        self.reason = reason
        self.metrics = metrics


def _log_numeric_classification_event(**metadata: object) -> None:
    """Emit one transcript-free JSON event for a numeric-sensitive first pass."""
    log.info(
        "Numeric classification event: %s",
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )


def _max_character_run(text: str, predicate=None) -> int:
    maximum = 0
    current = 0
    previous = None
    for character in text:
        if predicate is not None and not predicate(character):
            current = 0
            previous = None
            continue
        if character == previous:
            current += 1
        else:
            current = 1
            previous = character
        maximum = max(maximum, current)
    return maximum


def _decoder_output_metrics(text: str) -> DecoderOutputMetrics:
    non_whitespace = [character for character in text if not character.isspace()]
    punctuation = sum(
        unicodedata.category(character).startswith("P")
        for character in non_whitespace
    )
    return DecoderOutputMetrics(
        total_chars=len(text),
        non_whitespace_chars=len(non_whitespace),
        punctuation_chars=punctuation,
        distinct_chars=len(set(non_whitespace)),
        max_run_length=_max_character_run(text),
        fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    )


def _pathological_decoder_output_reason(
    text: str,
    metrics: DecoderOutputMetrics,
) -> str | None:
    punctuation_run = _max_character_run(
        text,
        predicate=lambda character: (
            not character.isspace()
            and unicodedata.category(character).startswith("P")
        ),
    )
    if punctuation_run >= _PATHOLOGICAL_PUNCTUATION_RUN_LENGTH:
        return "punctuation_run"

    if not metrics.non_whitespace_chars:
        return None
    punctuation_ratio = (
        metrics.punctuation_chars / metrics.non_whitespace_chars
    )
    if (
        metrics.non_whitespace_chars >= _LOW_DIVERSITY_PUNCTUATION_MIN_CHARS
        and metrics.distinct_chars <= _LOW_DIVERSITY_MAX_DISTINCT_CHARS
        and punctuation_ratio >= 0.90
    ):
        return "low_diversity_punctuation"

    # A no-space output made from one character class is decoder noise, while
    # repeated words such as "very very very" retain their ordinary spacing.
    if (
        metrics.non_whitespace_chars >= _LOW_DIVERSITY_SINGLE_CLASS_MIN_CHARS
        and metrics.distinct_chars <= _LOW_DIVERSITY_MAX_DISTINCT_CHARS
        and not any(character.isspace() for character in text)
    ):
        classes = Counter(
            unicodedata.category(character)[0] for character in text
        )
        dominant_class_ratio = max(classes.values()) / metrics.non_whitespace_chars
        if dominant_class_ratio >= 0.96:
            return "low_diversity_character_class"
    return None


def guard_pathological_decoder_output(text: str) -> str:
    """Reject decoder bursts before formatting, normalization, or injection."""
    metrics = _decoder_output_metrics(text)
    reason = _pathological_decoder_output_reason(text, metrics)
    if reason is None:
        return text
    log.warning(
        "Rejected pathological decoder output: reason=%s total_chars=%d "
        "non_whitespace_chars=%d punctuation_chars=%d distinct_chars=%d "
        "max_run_length=%d fingerprint=%s",
        reason,
        metrics.total_chars,
        metrics.non_whitespace_chars,
        metrics.punctuation_chars,
        metrics.distinct_chars,
        metrics.max_run_length,
        metrics.fingerprint,
    )
    raise PathologicalDecoderOutput(reason, metrics)


def remove_configured_prompt_leak(text: str, prompt: str | None) -> str:
    """Remove substantial contiguous configured-prompt overlap from output."""
    if not prompt or not text:
        return text

    token_pattern = re.compile(r"[A-Za-z0-9]+")
    text_matches = list(token_pattern.finditer(text))
    prompt_tokens = [match.group().casefold() for match in token_pattern.finditer(prompt)]
    text_tokens = [match.group().casefold() for match in text_matches]
    if not prompt_tokens or not text_tokens:
        return text

    # Some decoder failures repeat a short prompt-derived anchor while
    # mutating the punctuation between its words and inventing a different
    # continuation each time. Allow three deliberate repetitions, then fail
    # closed from the first anchor when a fourth establishes the loop.
    repeated_anchor_start: int | None = None
    repeated_anchor_words = 0
    repeated_anchor_count = 0
    for size in range(min(5, len(prompt_tokens)), 2, -1):
        prompt_ngrams = {
            tuple(prompt_tokens[index : index + size])
            for index in range(len(prompt_tokens) - size + 1)
        }
        occurrences: dict[tuple[str, ...], list[int]] = {}
        for index in range(len(text_tokens) - size + 1):
            ngram = tuple(text_tokens[index : index + size])
            if ngram in prompt_ngrams:
                occurrences.setdefault(ngram, []).append(index)
        for indices in occurrences.values():
            if len(indices) < 4:
                continue
            start = text_matches[indices[0]].start()
            if repeated_anchor_start is None or start < repeated_anchor_start:
                repeated_anchor_start = start
                repeated_anchor_words = size
                repeated_anchor_count = len(indices)
        if repeated_anchor_start is not None:
            break

    if repeated_anchor_start is not None:
        log.warning(
            "Removed repeated configured-prompt fragment loop: "
            "anchor_words=%d occurrences=%d",
            repeated_anchor_words,
            repeated_anchor_count,
        )
        return text[:repeated_anchor_start].rstrip()

    minimum = 5
    best: tuple[int, int] | None = None
    best_length = 0
    for text_start in range(len(text_tokens)):
        for prompt_start in range(len(prompt_tokens)):
            length = 0
            while (
                text_start + length < len(text_tokens)
                and prompt_start + length < len(prompt_tokens)
                and text_tokens[text_start + length]
                == prompt_tokens[prompt_start + length]
            ):
                length += 1
            if length >= minimum and length > best_length:
                best = (text_start, text_start + length - 1)
                best_length = length

    if best is None:
        return text

    start = text_matches[best[0]].start()
    end = text_matches[best[1]].end()
    # Include prompt punctuation but preserve punctuation belonging to adjacent
    # dictation. Whitespace cleanup makes removal safe in the middle of text.
    while end < len(text) and text[end] in "'’\".,!?;: ":
        end += 1
    cleaned = (text[:start] + text[end:]).strip()
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    log.warning(
        "Removed configured-prompt overlap: overlap_words=%d prompt_words=%d",
        best_length,
        len(prompt_tokens),
    )
    return remove_configured_prompt_leak(cleaned, prompt)


def _parse_number_component(value: str) -> int | None:
    """Parse a numeric token or a bounded spoken number from zero through 59."""
    normalized = value.lower().replace("-", " ").strip()
    if normalized.isdigit():
        return int(normalized)
    if normalized.startswith("oh "):
        return _NUMBER_UNITS.get(normalized[3:])
    if normalized in _NUMBER_UNITS:
        return _NUMBER_UNITS[normalized]

    words = normalized.split()
    if not words or words[0] not in _NUMBER_TENS:
        return None
    result = _NUMBER_TENS[words[0]]
    if len(words) == 2 and words[1] in _NUMBER_UNITS:
        unit = _NUMBER_UNITS[words[1]]
        if unit < 10:
            return result + unit
    return result if len(words) == 1 else None


def _parse_cardinal(value: str) -> int | None:
    """Parse a conventional English cardinal-number phrase."""
    words = re.findall(r"[A-Za-z]+", value.casefold().replace("-", " "))
    if not words or all(word == "and" for word in words):
        return None

    total = 0
    current = 0
    saw_number = False
    last_large_scale = float("inf")
    for index, word in enumerate(words):
        if word == "and":
            if index == len(words) - 1 or (current < 100 and total == 0):
                return None
            continue
        if word in _NUMBER_UNITS:
            current += _NUMBER_UNITS[word]
            saw_number = True
            continue
        if word in _NUMBER_TENS:
            current += _NUMBER_TENS[word]
            saw_number = True
            continue
        if word == "hundred":
            current = max(current, 1) * 100
            saw_number = True
            continue
        scale = _CARDINAL_SCALE_VALUES.get(word)
        if scale is None or scale >= last_large_scale:
            return None
        total += max(current, 1) * scale
        current = 0
        last_large_scale = scale
        saw_number = True
    return total + current if saw_number else None


def _format_cardinal(value: int) -> str:
    return f"{value:,}" if value >= 1_000 else str(value)


def _is_spoken_number_words(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text.casefold().replace("-", " "))
    allowed = (
        set(_NUMBER_UNITS)
        | set(_NUMBER_TENS)
        | set(_CARDINAL_SCALE_VALUES)
        | {"hundred", "and", "oh"}
    )
    if not words or not all(word in allowed for word in words):
        return False
    return (
        _parse_cardinal(text) is not None
        or _BARE_TIME_RE.fullmatch(text.strip()) is not None
    )


def _parse_bare_time_match(text: str, match: re.Match[str]) -> str | None:
    if (
        _NUMBER_WORD_BEFORE_RE.search(text[: match.start()])
        or _NUMBER_WORD_AFTER_RE.match(text[match.end() :])
    ):
        return None
    hour = _parse_number_component(match.group("hour"))
    minute = _parse_number_component(match.group("minute"))
    if hour is None or minute is None or not 1 <= hour <= 12 or minute > 59:
        return None
    return f"{hour}:{minute:02d}"


def _find_mixed_ambiguous_integer(text: str) -> re.Match[str] | None:
    matches = list(_AMBIGUOUS_MIXED_INTEGER_RE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    surrounding = text[: match.start()] + text[match.end() :]
    if not re.search(r"[A-Za-z]", surrounding):
        return None
    if re.search(r"\b(?:version|vee|v)\s*$", text[: match.start()], re.IGNORECASE):
        return None
    if (
        _VERSION_RE.search(text)
        or _TIME_RE.search(text)
        or _COMPACT_TIME_RE.search(text)
        or _DOTTED_IDENTIFIER_RE.search(text)
    ):
        return None
    return match


def _find_mixed_single_digit_run(text: str) -> re.Match[str] | None:
    matches = list(_AMBIGUOUS_SINGLE_DIGIT_RUN_RE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    surrounding = text[: match.start()] + text[match.end() :]
    if not re.search(r"[A-Za-z]", surrounding):
        return None
    if re.search(r"\bdigit\s*$", text[: match.start()], re.IGNORECASE):
        return None
    return match


def _equivalent_numeric_context(left: str, right: str) -> bool:
    return (
        re.sub(r"\s+", " ", left).strip().casefold()
        == re.sub(r"\s+", " ", right).strip().casefold()
    )


def _numeric_first_pass_decision(
    raw_text: str,
    first_pass: str,
) -> tuple[str, bool, str, re.Match[str] | None] | None:
    """Classify a numeric-sensitive first pass without exposing its content."""
    if _AMBIGUOUS_INTEGER_RE.fullmatch(raw_text):
        if len(re.sub(r"\D", "", raw_text)) > 4:
            return "integer_only", False, "out_of_time_range", None
        return "integer_only", True, "eligible", None

    mixed_integer = _find_mixed_ambiguous_integer(first_pass)
    if mixed_integer is not None:
        return "mixed_integer", True, "eligible", mixed_integer

    mixed_digit_run = _find_mixed_single_digit_run(first_pass)
    if mixed_digit_run is not None:
        return "mixed_digit_run", True, "eligible", mixed_digit_run

    integer_matches = list(_AMBIGUOUS_MIXED_INTEGER_RE.finditer(first_pass))
    if integer_matches:
        reason = "multiple_first_pass_candidates"
        if len(integer_matches) == 1:
            match = integer_matches[0]
            surrounding = first_pass[: match.start()] + first_pass[match.end() :]
            if not re.search(r"[A-Za-z]", surrounding):
                reason = "no_prose_context"
            else:
                reason = "explicit_numeric_context"
        return "mixed_integer", False, reason, None

    digit_matches = list(_AMBIGUOUS_SINGLE_DIGIT_RUN_RE.finditer(first_pass))
    if digit_matches:
        reason = (
            "multiple_first_pass_candidates"
            if len(digit_matches) != 1
            else "no_prose_context"
        )
        shape = "mixed_digit_run"
        if len(digit_matches) == 1:
            match = digit_matches[0]
            surrounding = first_pass[: match.start()] + first_pass[match.end() :]
            if not re.search(r"[A-Za-z]", surrounding):
                shape = "standalone_digit_run"
            elif re.search(
                r"\bdigit\s*$",
                first_pass[: match.start()],
                re.IGNORECASE,
            ):
                reason = "explicit_numeric_context"
        return shape, False, reason, None
    return None


def _valid_bare_time_matches(
    text: str,
) -> list[tuple[re.Match[str], str]]:
    matches: list[tuple[re.Match[str], str]] = []
    for match in _BARE_TIME_RE.finditer(text):
        rendered = _parse_bare_time_match(text, match)
        if rendered is not None:
            matches.append((match, rendered))
    return matches


def _numeric_second_pass_details(
    candidate: str | None,
) -> tuple[str, int, re.Match[str] | None, str | None]:
    """Return only bounded classification metadata plus an internal rendering."""
    if candidate is None:
        return "other", 0, None, None

    digit_runs = list(_SPOKEN_SINGLE_DIGIT_RUN_RE.finditer(candidate))
    if digit_runs:
        return "single_digit_words", len(digit_runs), digit_runs[0], None

    bare_times = _valid_bare_time_matches(candidate)
    if bare_times:
        match, rendered = bare_times[0]
        return "bare_time_words", len(bare_times), match, rendered

    cardinals = [
        match
        for match in _CARDINAL_PHRASE_RE.finditer(candidate)
        if _parse_cardinal(match.group("value")) is not None
    ]
    if cardinals:
        return "cardinal_words", len(cardinals), cardinals[0], None

    numeric = list(re.finditer(r"(?<!\w)\d+(?!\w)", candidate))
    if numeric:
        return "numeric", len(numeric), numeric[0], None
    return "other", 0, None, None


def _normalize_cardinal_phrases(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if re.search(r"\bdigit\s+$", match.string[: match.start()], re.IGNORECASE):
            return match.group(0)
        parsed = _parse_cardinal(match.group("value"))
        if parsed is None or parsed < 10:
            return match.group(0)
        return _format_cardinal(parsed)

    return _CARDINAL_PHRASE_RE.sub(replace, text)


def _protect_explicit_digit_commands(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(1).casefold()
        rendered = _NUMERAL_VALUES.get(value, value)
        return _DIGIT_COMMAND_MARKERS[int(rendered)]

    return _DIGIT_COMMAND_RE.sub(replace, text)


def _render_explicit_digit_commands(text: str) -> str:
    for value, marker in enumerate(_DIGIT_COMMAND_MARKERS):
        text = text.replace(marker, str(value))
    return text


def normalize_spoken_numbers(text: str) -> str:
    """Normalize versions, times, and conventional spoken cardinal numbers."""
    text = _protect_explicit_digit_commands(text)

    def replace_version(match: re.Match[str]) -> str:
        components = re.split(
            r"\s*(?:\.\s*|\s+point\s+)",
            match.group("value"),
            flags=re.IGNORECASE,
        )
        parsed = [_parse_number_component(component) for component in components]
        if any(component is None for component in parsed):
            return match.group(0)
        prefix = "version " if match.group("prefix").lower() == "version" else "v"
        return prefix + ".".join(str(component) for component in parsed)

    def replace_time(match: re.Match[str]) -> str:
        hour = _parse_number_component(match.group("hour"))
        minute = _parse_number_component(match.group("minute"))
        if hour is None or minute is None or not 1 <= hour <= 12 or minute > 59:
            return match.group(0)
        period_initial = match.group("period").lstrip()[0].lower()
        period = "AM" if period_initial == "a" else "PM"
        return f"{hour}:{minute:02d} {period}"

    def replace_bare_time(match: re.Match[str]) -> str:
        return _parse_bare_time_match(match.string, match) or match.group(0)

    def replace_compact_time(match: re.Match[str]) -> str:
        value = match.group("value")
        hour = int(value[:-2])
        minute = int(value[-2:])
        if not 1 <= hour <= 12 or minute > 59:
            return match.group(0)
        period_initial = match.group("period").lstrip()[0].lower()
        period = "AM" if period_initial == "a" else "PM"
        return f"{hour}:{minute:02d} {period}"

    def replace_dotted_identifier(match: re.Match[str]) -> str:
        components = re.split(
            rf"(?:{re.escape(EXPLICIT_DOT_COMMAND)}|\s+point\s*(?:\.\s*)?)",
            match.group("value"),
            flags=re.IGNORECASE,
        )
        normalized: list[str] = []
        for component in components:
            component = component.strip()
            number = _parse_number_component(component)
            if number is not None:
                normalized.append(str(number))
            elif component.casefold() == "ex":
                normalized.append("x")
            elif re.fullmatch(r"[A-Za-z]", component):
                normalized.append(component.lower())
            else:
                return match.group(0)
        return ".".join(normalized)

    def replace_markdown_extension(match: re.Match[str]) -> str:
        return f"{match.group('base')}.md"

    text = _OWNER_DOTTED_VERSION_RE.sub("1.20.x", text)
    text = _VERSION_RE.sub(replace_version, text)
    text = _DOTTED_IDENTIFIER_RE.sub(replace_dotted_identifier, text)
    text = _MARKDOWN_EXTENSION_RE.sub(replace_markdown_extension, text)
    text = _COMPACT_TIME_RE.sub(replace_compact_time, text)
    text = _TIME_RE.sub(replace_time, text)
    text = _BARE_TIME_RE.sub(replace_bare_time, text)
    return _render_explicit_digit_commands(_normalize_cardinal_phrases(text))


class StreamingNumberNormalizer:
    """Compose a large cardinal that Whisper splits at magnitude boundaries."""

    def __init__(self) -> None:
        self._pending = ""
        self._pending_numeric_total: int | None = None
        self._pending_numeric_rank: int | None = None

    @staticmethod
    def _is_cardinal_fragment(text: str) -> bool:
        return _is_spoken_number_words(text)

    @staticmethod
    def _ends_with_large_scale(text: str) -> bool:
        words = re.findall(r"[A-Za-z]+", text.casefold().replace("-", " "))
        return bool(words) and words[-1] in _CARDINAL_SCALE_VALUES

    def reset(self) -> None:
        self._pending = ""
        self._pending_numeric_total = None
        self._pending_numeric_rank = None

    @property
    def has_pending(self) -> bool:
        return bool(self._pending) or self._pending_numeric_total is not None

    def flush(self) -> str:
        if self._pending:
            pending = self._pending
            self.reset()
            return normalize_spoken_numbers(pending)
        if self._pending_numeric_total is not None:
            pending = _format_cardinal(self._pending_numeric_total)
            self.reset()
            return pending
        return ""

    @staticmethod
    def _integer_fragment(text: str) -> tuple[int, str] | None:
        match = _INTEGER_FRAGMENT_RE.fullmatch(text)
        if match is None:
            return None
        return int(match.group("value").replace(",", "")), match.group("suffix")

    @staticmethod
    def _magnitude_rank(value: int) -> int:
        rank = 0
        while value and value % 10 == 0:
            value //= 10
            rank += 1
        return rank

    def _begin_numeric_pending(self, value: int) -> None:
        self._pending_numeric_total = value
        self._pending_numeric_rank = self._magnitude_rank(value)

    def _consume_numeric_fragment(self, value: int, suffix: str) -> str:
        if self._pending_numeric_total is None:
            if value >= 1_000 and self._magnitude_rank(value) >= 3:
                self._begin_numeric_pending(value)
                return ""
            return _format_cardinal(value) + suffix

        rank = self._magnitude_rank(value)
        assert self._pending_numeric_rank is not None
        if rank < self._pending_numeric_rank:
            self._pending_numeric_total += value
            self._pending_numeric_rank = rank
            if value >= 1_000 and rank >= 3:
                return ""
            result = _format_cardinal(self._pending_numeric_total) + suffix
            self.reset()
            return result

        previous = self.flush()
        current = self._consume_numeric_fragment(value, suffix)
        return f"{previous} {current}" if current else previous

    def feed(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""

        fragment = stripped.rstrip(".,!?;:… ")
        numeric = self._integer_fragment(stripped)
        if self._pending:
            if numeric is not None:
                pending_value = _parse_cardinal(self._pending)
                if pending_value is not None:
                    self._pending = ""
                    self._begin_numeric_pending(pending_value)
                    return self._consume_numeric_fragment(*numeric)
            if self._is_cardinal_fragment(fragment):
                combined = f"{self._pending} {fragment}"
                if self._ends_with_large_scale(fragment):
                    self._pending = combined
                    return ""
                self._pending = ""
                suffix = stripped[len(fragment):]
                return normalize_spoken_numbers(combined) + suffix
            pending = self.flush()
            current = normalize_spoken_numbers(stripped)
            return f"{pending} {current}" if current else pending

        if self._pending_numeric_total is not None:
            if numeric is not None:
                return self._consume_numeric_fragment(*numeric)
            if self._is_cardinal_fragment(fragment):
                value = _parse_cardinal(fragment)
                if value is not None:
                    suffix = stripped[len(fragment):]
                    return self._consume_numeric_fragment(value, suffix)
            pending = self.flush()
            current = normalize_spoken_numbers(stripped)
            return f"{pending} {current}" if current else pending

        if numeric is not None:
            return self._consume_numeric_fragment(*numeric)

        trailing = _TRAILING_CARDINAL_RE.search(stripped)
        if trailing is not None:
            number = trailing.group("value")
            value = _parse_cardinal(number)
            if value is not None and value >= 10:
                self._pending = number
                return normalize_spoken_numbers(stripped[: trailing.start()].rstrip())
        return normalize_spoken_numbers(stripped)


def suppress_pause_terminator(text: str) -> str:
    """Remove periods/ellipses inferred at a VAD-delimited phrase boundary."""
    if re.fullmatch(r"\s*(?:\.{1,}|…+)\s*", text):
        return text.strip()
    return _PAUSE_TERMINATOR_RE.sub("", text).rstrip()


def _mark_spoken_punctuation(text: str) -> str:
    """Convert spoken commands while retaining sentence-punctuation provenance."""
    if re.fullmatch(r"\s*close[.,]?\s*", text, re.IGNORECASE):
        return PENDING_CLOSE_COMMAND

    for phrase, character in _SPOKEN_PUNCTUATION:
        phrase_pattern = r"(?:\s+|[,.!?;:]\s*)".join(
            re.escape(word) for word in phrase.split()
        )
        pattern = re.compile(
            rf"[,.!?;:]?\s*\b{phrase_pattern}\b[.,]?",
            re.IGNORECASE,
        )
        text = pattern.sub(character, text)

    text = _NUMERAL_RE.sub(
        lambda match: _NUMERAL_VALUES.get(
            match.group(1).lower(),
            match.group(1),
        ),
        text,
    )
    text = re.sub(r"\s+([,.!?;:\)\]])", r"\1", text)
    text = re.sub(r"([\(\[])\s+", r"\1", text)
    text = re.sub(
        rf"\s*{re.escape(EXPLICIT_DOT_COMMAND)}\s*",
        EXPLICIT_DOT_COMMAND,
        text,
    )
    text = re.sub(
        rf"[ \t]*({re.escape(SPOKEN_NEW_LINE_COMMAND)}|"
        rf"{re.escape(SPOKEN_NEW_PARAGRAPH_COMMAND)})[ \t]*",
        r"\1",
        text,
    )
    text = re.sub(r"\s*_\s*", "_", text)
    text = re.sub(r"\s*—\s*", "—", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"~\s*", "~", text)
    text = re.sub(r"([,?])\1+", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _render_explicit_sentence_punctuation(text: str) -> str:
    replacements = {
        EXPLICIT_COMMA_COMMAND: ",",
        EXPLICIT_QUESTION_COMMAND: "?",
        EXPLICIT_EXCLAMATION_COMMAND: "!",
        EXPLICIT_PERIOD_COMMAND: ".",
        EXPLICIT_SEMICOLON_COMMAND: ";",
        EXPLICIT_COLON_COMMAND: ":",
        EXPLICIT_ELLIPSIS_COMMAND: "…",
    }
    for marker, rendered in replacements.items():
        text = re.sub(
            rf"{re.escape(marker)}(?=[A-Za-z0-9])",
            rendered + " ",
            text,
        )
        text = text.replace(marker, rendered)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def apply_spoken_punctuation(text: str) -> str:
    """Convert unambiguous spoken punctuation commands with exact spacing."""
    return render_spoken_boundaries(
        _render_explicit_sentence_punctuation(_mark_spoken_punctuation(text))
    )


def render_spoken_boundaries(text: str) -> str:
    """Render only internal spoken-boundary markers as literal line breaks."""
    return text.replace(SPOKEN_NEW_PARAGRAPH_COMMAND, "\n\n").replace(
        SPOKEN_NEW_LINE_COMMAND,
        "\n",
    )


def apply_automatic_punctuation_setting(
    text: str,
    automatic_punctuation: bool,
    *,
    preserve_spoken_boundaries: bool = False,
) -> str:
    """Apply spoken punctuation and optionally suppress model-inferred marks."""
    text = _mark_spoken_punctuation(text)
    if not automatic_punctuation:
        cleaned: list[str] = []
        index = 0
        while index < len(text):
            character = text[index]
            if character not in ".,!?;:…":
                cleaned.append(character)
                index += 1
                continue
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            lexical_numeric = (
                character in ".,:"
                and previous.isdigit()
                and following.isdigit()
            )
            if lexical_numeric:
                cleaned.append(character)
                index += 1
                continue
            run_end = index + 1
            while run_end < len(text) and text[run_end] in ".,!?;:…":
                run_end += 1
            following = text[run_end] if run_end < len(text) else ""
            if cleaned and cleaned[-1].isalnum() and following.isalnum():
                cleaned.append(" ")
            index = run_end
        text = "".join(cleaned)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    text = _render_explicit_sentence_punctuation(text)
    return text if preserve_spoken_boundaries else render_spoken_boundaries(text)


def lowercase_phrase_initial(text: str) -> str:
    """Prevent VAD phrase boundaries from creating false sentence casing."""
    word = re.search(r"[A-Za-z]+", text)
    preserve_acronym = (
        word is not None
        and word.group()[0].isupper()
        and any(character.isupper() for character in word.group()[1:])
    )
    if (
        word is None
        or not word.group()[0].isupper()
        or preserve_acronym
    ):
        lowered = text
    else:
        index = word.start()
        lowered = text[:index] + text[index].lower() + text[index + 1 :]

    return re.sub(
        r"\bi(?=\b|['’](?:m|d|ll|ve)\b)",
        "I",
        lowered,
        flags=re.IGNORECASE,
    )


def apply_vocabulary_corrections(text: str) -> str:
    """Apply deterministic product names after phrase-initial casing."""
    text = re.sub(r"\bjava\s+dock\b", "javadoc", text, flags=re.IGNORECASE)
    return re.sub(
        r"\badorable\s+hamster\s+pets\b",
        "Adorable Hamster Pets",
        text,
        flags=re.IGNORECASE,
    )


def apply_contextual_phrase_casing(text: str, capitalize: bool) -> str:
    """Apply caret-aware initial casing, then restore approved vocabulary."""
    if not capitalize:
        text = lowercase_phrase_initial(text)
    return apply_vocabulary_corrections(text)


def apply_boundary_phrase_casing(
    text: str,
    boundary: str,
    *,
    capitalize_new_paragraphs: bool,
    capitalize_new_lines: bool,
    preserve_explicit_initial: bool = False,
) -> str:
    """Apply one boundary's configured casing without altering internal casing.

    When preserve_explicit_initial is set, explicit capitalization from a
    formatting command in the same utterance is never lowercased; the
    boundary's own uppercase choice and other fixes still apply.
    """
    enabled = (
        capitalize_new_paragraphs
        if boundary in ("document", "paragraph")
        else capitalize_new_lines
        if boundary == "line"
        else False
    )
    if enabled:
        text = re.sub(
            r"[A-Za-z]",
            lambda match: match.group(0).upper(),
            text,
            count=1,
        )
    elif not preserve_explicit_initial:
        text = lowercase_phrase_initial(text)
    text = re.sub(
        r"\bi(?=\b|['’](?:m|d|ll|ve)\b)",
        "I",
        text,
        flags=re.IGNORECASE,
    )
    return apply_vocabulary_corrections(text)


def _merge_boundary(left: str, right: str) -> str:
    if "paragraph" in (left, right):
        return "paragraph"
    if "line" in (left, right):
        return "line"
    if "document" in (left, right):
        return "document"
    return "none"


def format_spoken_boundaries(
    text: str,
    *,
    initial_boundary: str,
    pending_boundary: str,
    capitalize_new_paragraphs: bool,
    capitalize_new_lines: bool,
    preserve_initial_when_none: bool = False,
    preserve_explicit_initial: bool = False,
) -> tuple[str, str, str]:
    """Render spoken markers and case only the first following alphabetic char."""
    marker_pattern = re.compile(
        f"({re.escape(SPOKEN_NEW_LINE_COMMAND)}|"
        f"{re.escape(SPOKEN_NEW_PARAGRAPH_COMMAND)})"
    )
    pieces = marker_pattern.split(text)
    rendered: list[str] = []
    match_text: list[str] = []
    boundary = (
        pending_boundary
        if pending_boundary != "none"
        else initial_boundary
    )
    needs_casing = True
    first_casing = True

    for piece in pieces:
        if not piece:
            continue
        if piece in (SPOKEN_NEW_LINE_COMMAND, SPOKEN_NEW_PARAGRAPH_COMMAND):
            line_break = (
                "\n\n"
                if piece == SPOKEN_NEW_PARAGRAPH_COMMAND
                else "\n"
            )
            rendered.append(line_break)
            match_text.append(line_break)
            spoken_boundary = (
                "paragraph"
                if piece == SPOKEN_NEW_PARAGRAPH_COMMAND
                else "line"
            )
            boundary = _merge_boundary(boundary if needs_casing else "none", spoken_boundary)
            needs_casing = True
            first_casing = False
            continue

        match_text.append(piece)
        if needs_casing:
            preserve = (
                first_casing
                and boundary == "none"
                and preserve_initial_when_none
            )
            rendered.append(
                piece
                if preserve
                else apply_boundary_phrase_casing(
                    piece,
                    boundary,
                    capitalize_new_paragraphs=capitalize_new_paragraphs,
                    capitalize_new_lines=capitalize_new_lines,
                    preserve_explicit_initial=preserve_explicit_initial,
                )
            )
            if re.search(r"[A-Za-z]", piece):
                needs_casing = False
                boundary = "none"
            first_casing = False
        else:
            rendered.append(piece)

    return (
        "".join(rendered),
        "".join(match_text),
        boundary if needs_casing else "none",
    )


def _find_pathological_repetition(
    text: str,
) -> tuple[int, int, int, int, str] | None:
    matches = list(_REPETITION_WORD_RE.finditer(text))
    if len(matches) < _PATHOLOGICAL_REPEAT_CYCLES:
        return None

    normalized = [match.group().casefold() for match in matches]
    for start in range(len(matches)):
        for cycle_words in range(1, _PATHOLOGICAL_MAX_CYCLE_WORDS + 1):
            minimum_cycles = (
                _PATHOLOGICAL_REPEAT_CYCLES
                if cycle_words == 1
                else _PATHOLOGICAL_MULTIWORD_REPEAT_CYCLES
            )
            if start + cycle_words * minimum_cycles > len(matches):
                continue
            cycle = normalized[start : start + cycle_words]
            if any(word in _REPETITION_GUARD_EXEMPT_WORDS for word in cycle):
                continue

            end = start + cycle_words
            while end + cycle_words <= len(matches):
                if normalized[end : end + cycle_words] != cycle:
                    break
                end += cycle_words
            cycles = (end - start) // cycle_words
            if cycles < minimum_cycles:
                continue

            first = matches[start]
            last = matches[end - 1]
            fingerprint = hashlib.sha256(
                " ".join(cycle).encode("utf-8")
            ).hexdigest()[:12]
            log.warning(
                "Collapsed pathological repetition: cycle_words=%d cycles=%d "
                "word_count=%d fingerprint=%s",
                cycle_words,
                cycles,
                end - start,
                fingerprint,
            )
            canonical_last = matches[start + cycle_words - 1]
            return (
                first.start(),
                canonical_last.end(),
                last.end(),
                cycles,
                fingerprint,
            )

    return None


def guard_pathological_repetition(text: str) -> str:
    """Collapse bounded exact one- through eight-word decoder loops."""
    for _ in range(8):
        repetition = _find_pathological_repetition(text)
        if repetition is None:
            return text
        start, canonical_end, repeated_end, _, _ = repetition
        text = text[:canonical_end] + text[repeated_end:]
    return text


def merge_transcription_segment_texts(segment_texts: list[str]) -> str:
    """Join decoder segments while removing exact word overlap at boundaries."""
    merged = ""
    for segment_text in segment_texts:
        segment = segment_text.strip()
        if not segment:
            continue
        if not merged:
            merged = segment
            continue

        previous_words = list(_REPETITION_WORD_RE.finditer(merged))
        next_words = list(_REPETITION_WORD_RE.finditer(segment))
        maximum = min(
            len(previous_words),
            len(next_words),
            _MAX_SEGMENT_OVERLAP_WORDS,
        )
        overlap_words = 0
        for candidate in range(maximum, 1, -1):
            previous = [
                match.group().casefold()
                for match in previous_words[-candidate:]
            ]
            following = [
                match.group().casefold()
                for match in next_words[:candidate]
            ]
            if previous == following:
                overlap_words = candidate
                break

        if overlap_words:
            overlap = " ".join(
                match.group().casefold()
                for match in next_words[:overlap_words]
            )
            fingerprint = hashlib.sha256(overlap.encode("utf-8")).hexdigest()[:12]
            log.info(
                "Removed decoder segment overlap: words=%d fingerprint=%s",
                overlap_words,
                fingerprint,
            )
            prefix = merged[: previous_words[-overlap_words].start()]
            separator = "" if not prefix or prefix[-1].isspace() else " "
            merged = prefix + separator + segment
        else:
            merged = merged.rstrip() + " " + segment

    return merged


class LocalEngine(TranscriptionEngine):
    """Transcription using faster-whisper locally."""

    def __init__(self, server_config: ServerConfig, engine_config: EngineConfig):
        self._model = None
        self._model_lock = threading.Lock()
        self._digit_text_token_ids: tuple[int, ...] | None = None
        self._model_name = server_config.model
        self._language = server_config.language
        self._prompt = server_config.prompt or None
        self._hotwords = server_config.hotwords or None
        self._temperature = server_config.temperature
        self._compute_type = engine_config.compute_type
        self._device = engine_config.device

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        with self._model_lock:
            if self._model is not None:
                return

            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise RuntimeError(
                    "faster-whisper not installed. "
                    "Install with: pip install 'faster-whisper-dictation[local]'"
                )

            device = self._device
            compute_type = self._compute_type

            if device == "auto":
                try:
                    import torch

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"

            if compute_type == "auto":
                compute_type = "float16" if device == "cuda" else "int8"

            log.info(
                "Loading model %s on %s (%s)...",
                self._model_name,
                device,
                compute_type,
            )
            self._model = WhisperModel(
                self._model_name,
                device=device,
                compute_type=compute_type,
            )
        log.info("Model loaded")

    def _decode_audio(
        self,
        audio: np.ndarray,
        *,
        vad_filter: bool,
        suppress_tokens: list[int] | None = None,
    ) -> str:
        options = {
            "language": self._language,
            "beam_size": 5,
            "temperature": self._temperature,
            "initial_prompt": self._prompt,
            "hotwords": self._hotwords,
            "vad_filter": vad_filter,
            "condition_on_previous_text": False,
            "no_repeat_ngram_size": 3,
        }
        if suppress_tokens is not None:
            options["suppress_tokens"] = suppress_tokens
        segments, _ = self._model.transcribe(audio, **options)
        return merge_transcription_segment_texts(
            [segment.text for segment in segments]
        )

    def _get_digit_text_token_ids(self) -> tuple[int, ...]:
        if self._digit_text_token_ids is not None:
            return self._digit_text_token_ids
        tokenizer = self._model.hf_tokenizer
        end_of_text = tokenizer.token_to_id("<|endoftext|>")
        if end_of_text is None:
            self._digit_text_token_ids = ()
            return self._digit_text_token_ids
        token_ids: list[int] = []
        for token_id in tokenizer.get_vocab().values():
            if token_id >= end_of_text:
                continue
            try:
                decoded = tokenizer.decode([token_id])
            except Exception:
                continue
            if any(character.isdigit() for character in decoded):
                token_ids.append(token_id)
        self._digit_text_token_ids = tuple(sorted(set(token_ids)))
        return self._digit_text_token_ids

    def _decode_numeral_suppressed(
        self,
        audio: np.ndarray,
        *,
        vad_filter: bool,
    ) -> str | None:
        token_ids = self._get_digit_text_token_ids()
        if not token_ids:
            return None
        started = time.perf_counter()
        try:
            candidate = self._decode_audio(
                audio,
                vad_filter=vad_filter,
                suppress_tokens=[-1, *token_ids],
            )
            candidate = remove_configured_prompt_leak(candidate, self._prompt)
            candidate = guard_pathological_decoder_output(candidate)
            return suppress_pause_terminator(candidate)
        except Exception:
            log.warning("Targeted numeric second decode failed; retaining first pass.", exc_info=True)
            return None
        finally:
            log.info(
                "Targeted numeric second decode completed: elapsed_ms=%d",
                round((time.perf_counter() - started) * 1000),
            )

    def _recover_numeric_with_classification(
        self,
        audio: np.ndarray,
        raw_text: str,
        first_pass: str,
        *,
        vad_filter: bool,
        automatic_punctuation: bool,
        preserve_spoken_boundaries: bool,
    ) -> str | None:
        started = time.perf_counter()
        analysis_raw = apply_automatic_punctuation_setting(
            raw_text,
            automatic_punctuation,
            preserve_spoken_boundaries=preserve_spoken_boundaries,
        )
        analysis_first_pass = apply_automatic_punctuation_setting(
            first_pass,
            automatic_punctuation,
            preserve_spoken_boundaries=preserve_spoken_boundaries,
        )
        decision = _numeric_first_pass_decision(analysis_raw, analysis_first_pass)
        if decision is None:
            return None

        shape, triggered, reason, first_match = decision
        metadata: dict[str, object] = {
            "acceptance": "not_triggered",
            "candidate_match_count": 0,
            "digit_value_agreement": None,
            "elapsed_ms": 0,
            "first_pass_shape": shape,
            "left_context_aligned": None,
            "reason": reason,
            "right_context_aligned": None,
            "second_pass_class": "other",
            "triggered": triggered,
        }
        if not triggered:
            metadata["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            _log_numeric_classification_event(**metadata)
            return None

        candidate = self._decode_numeral_suppressed(
            audio,
            vad_filter=vad_filter,
        )
        analysis_candidate = (
            apply_automatic_punctuation_setting(
                candidate,
                automatic_punctuation,
                preserve_spoken_boundaries=preserve_spoken_boundaries,
            )
            if candidate is not None
            else None
        )
        candidate_class, match_count, recovered_match, rendered = (
            _numeric_second_pass_details(analysis_candidate)
        )
        metadata["acceptance"] = "rejected"
        metadata["candidate_match_count"] = match_count
        metadata["second_pass_class"] = candidate_class

        recovered: str | None = None
        if candidate is None:
            metadata["reason"] = "second_decode_unavailable"
        elif shape == "integer_only":
            if _is_spoken_number_words(analysis_candidate):
                metadata["acceptance"] = "accepted"
                metadata["reason"] = "eligible"
                recovered = analysis_candidate
            else:
                metadata["reason"] = "candidate_class_mismatch"
        else:
            assert first_match is not None
            if recovered_match is not None:
                left_aligned = _equivalent_numeric_context(
                    analysis_first_pass[: first_match.start()],
                    analysis_candidate[: recovered_match.start()],
                )
                right_aligned = _equivalent_numeric_context(
                    analysis_first_pass[first_match.end() :],
                    analysis_candidate[recovered_match.end() :],
                )
                metadata["left_context_aligned"] = left_aligned
                metadata["right_context_aligned"] = right_aligned
            else:
                left_aligned = None
                right_aligned = None

            expected_class = (
                "bare_time_words"
                if shape == "mixed_integer"
                else "single_digit_words"
            )
            if candidate_class != expected_class:
                metadata["reason"] = "candidate_class_mismatch"
            elif match_count != 1 or recovered_match is None:
                metadata["reason"] = "candidate_match_count"
            elif not left_aligned:
                metadata["reason"] = "left_context_mismatch"
            elif not right_aligned:
                metadata["reason"] = "right_context_mismatch"
            elif shape == "mixed_integer":
                assert rendered is not None
                metadata["acceptance"] = "accepted"
                metadata["reason"] = "eligible"
                recovered = (
                    analysis_first_pass[: first_match.start()]
                    + rendered
                    + analysis_first_pass[first_match.end() :]
                )
            else:
                first_digits = first_match.group("value").split()
                recovered_words = recovered_match.group("value").casefold().split()
                recovered_digits = [_NUMERAL_VALUES[word] for word in recovered_words]
                digit_agreement = first_digits == recovered_digits
                metadata["digit_value_agreement"] = digit_agreement
                if not digit_agreement:
                    metadata["reason"] = "digit_value_mismatch"
                else:
                    metadata["acceptance"] = "accepted"
                    metadata["reason"] = "eligible"
                    recovered = (
                        analysis_first_pass[: first_match.start()]
                        + recovered_match.group("value")
                        + analysis_first_pass[first_match.end() :]
                    )

        metadata["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        _log_numeric_classification_event(**metadata)
        return recovered

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        vad_filter: bool = True,
        automatic_punctuation: bool = True,
        normalize_numbers: bool = True,
        preserve_spoken_boundaries: bool = False,
    ) -> str:
        self._ensure_model()

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32) / 32768.0

        text = self._decode_audio(audio, vad_filter=vad_filter)
        text = remove_configured_prompt_leak(text, self._prompt)
        text = guard_pathological_decoder_output(text)
        first_pass = suppress_pause_terminator(text)
        recovered = self._recover_numeric_with_classification(
            audio,
            text,
            first_pass,
            vad_filter=vad_filter,
            automatic_punctuation=automatic_punctuation,
            preserve_spoken_boundaries=preserve_spoken_boundaries,
        )
        if recovered is not None:
            text = recovered
        text = suppress_pause_terminator(text)
        text = apply_automatic_punctuation_setting(
            text,
            automatic_punctuation,
            preserve_spoken_boundaries=preserve_spoken_boundaries,
        )
        if normalize_numbers:
            text = normalize_spoken_numbers(text)
        text = guard_pathological_repetition(text)
        if text:
            log.debug("Transcribed: %d chars", len(text))
        return text

    def is_available(self) -> bool:
        try:
            self._ensure_model()
            return True
        except Exception:
            log.error("Local engine model load failed", exc_info=True)
            return False

    def close(self) -> None:
        if self._model is not None and hasattr(self._model, "close"):
            self._model.close()
        self._model = None

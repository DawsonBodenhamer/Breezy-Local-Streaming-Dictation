"""Local transcription engine using faster-whisper (no server needed)."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
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
}
_NUMBER_WORD_PATTERN = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|(?:twenty|thirty|forty|fifty)(?:[- ](?:one|two|three|"
    r"four|five|six|seven|eight|nine))?)"
)
_NUMBER_COMPONENT_PATTERN = rf"(?:[0-9]+|{_NUMBER_WORD_PATTERN})"
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
    rf"\s+(?P<period>{_MERIDIEM_PATTERN})"
    rf"(?!\w)",
    re.IGNORECASE,
)
_COMPACT_TIME_RE = re.compile(
    rf"(?<![\w.])(?P<value>[0-9]{{3,4}})"
    rf"\s+(?P<period>{_MERIDIEM_PATTERN})(?!\w)",
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
    ("close quotation mark", CLOSE_QUOTE_COMMAND),
    ("open quotation mark", OPEN_QUOTE_COMMAND),
    ("close quote", CLOSE_QUOTE_COMMAND),
    ("open quote", OPEN_QUOTE_COMMAND),
    ("close parentheses", ")"),
    ("open parentheses", " ("),
    ("exclamation mark", "!"),
    ("question mark", "?"),
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
    ("dot dot dot", "…"),
    ("ellipses", "…"),
    ("ellipsis", "…"),
    ("dot", EXPLICIT_DOT_COMMAND),
    ("forward slash", "/"),
    ("slash", "/"),
    ("back tick", BACKTICK_COMMAND),
    ("backtick", BACKTICK_COMMAND),
    ("hyphen", "-"),
    ("tilde", " ~"),
    ("tildi", " ~"),
    ("underscore", "_"),
    ("quote", OPEN_QUOTE_COMMAND),
    ("semicolon", ";"),
    ("colon", ":"),
    ("period", "."),
    ("comma", ","),
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


def normalize_spoken_numbers(text: str) -> str:
    """Normalize strongly identified versions, dotted identifiers, and times."""
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
    return _TIME_RE.sub(replace_time, text)


def suppress_pause_terminator(text: str) -> str:
    """Remove periods/ellipses inferred at a VAD-delimited phrase boundary."""
    if re.fullmatch(r"\s*(?:\.{1,}|…+)\s*", text):
        return text.strip()
    return _PAUSE_TERMINATOR_RE.sub("", text).rstrip()


def apply_spoken_punctuation(text: str) -> str:
    """Convert unambiguous spoken punctuation commands with exact spacing."""
    if re.fullmatch(r"\s*close[.,]?\s*", text, re.IGNORECASE):
        return PENDING_CLOSE_COMMAND

    for phrase, character in _SPOKEN_PUNCTUATION:
        pattern = re.compile(
            rf"[,.!?;:]?\s*\b{re.escape(phrase)}\b[.,]?",
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
    text = re.sub(r"\s*_\s*", "_", text)
    text = re.sub(r"\s*—\s*", "—", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"~\s*", "~", text)
    text = re.sub(r"([,?])\1+", r"\1", text)
    text = re.sub(r"([,.!?;:])(?=[A-Za-z0-9])", r"\1 ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


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

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        vad_filter: bool = True,
    ) -> str:
        self._ensure_model()

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32) / 32768.0

        segments, _ = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=5,
            temperature=self._temperature,
            initial_prompt=self._prompt,
            hotwords=self._hotwords,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
            no_repeat_ngram_size=3,
        )

        text = merge_transcription_segment_texts(
            [seg.text for seg in segments]
        )
        text = guard_pathological_decoder_output(text)
        text = suppress_pause_terminator(text)
        text = apply_spoken_punctuation(text)
        text = normalize_spoken_numbers(text)
        text = guard_pathological_repetition(text)
        if text:
            log.debug("Transcribed: %d chars", len(text))
        return text

    def is_available(self) -> bool:
        try:
            self._ensure_model()
            return True
        except Exception as e:
            log.debug("Local engine not available: %s", e)
            return False

    def close(self) -> None:
        if self._model is not None and hasattr(self._model, "close"):
            self._model.close()
        self._model = None

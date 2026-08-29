"""Dictation daemon — ties together audio capture, VAD, transcription, and typing."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .audio import AudioStream
from .caret_context import CaretContext, classify_casing_context, get_caret_context
from .config import CONFIG_DIR, Config, load_config
from .conversions import ConversionStore
from .engine import TranscriptionEngine, create_engine, create_ws_engine
from .engine.local import (
    EXPLICIT_DOT_COMMAND,
    LocalEngine,
    PathologicalDecoderOutput,
    StreamingNumberNormalizer,
    apply_automatic_punctuation_setting,
    format_spoken_boundaries,
    guard_pathological_decoder_output,
    guard_pathological_repetition,
    normalize_spoken_numbers,
    remove_configured_prompt_leak,
)
from .hotkey import HotkeyListener
from .notifier import notify
from .typer import type_text
from .vad import SpeechDetector

if TYPE_CHECKING:
    from .engine.whisperlivekit import WhisperLiveKitEngine

log = logging.getLogger(__name__)


class ManualLineBreakSignal:
    """Consume a content-free, originating-window-bound manual newline marker."""

    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            self.path = path
            return
        local_app_data = os.environ.get("LOCALAPPDATA")
        runtime_dir = (
            Path(local_app_data) / "breezy_local_streaming_dictation"
            if local_app_data
            else CONFIG_DIR
        )
        self.path = runtime_dir / "manual_line_break.signal"

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.debug("Manual-line-break signal cleanup failed", exc_info=True)

    def consume(self, foreground_window_handle: int | None) -> int:
        try:
            value = self.path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            self.clear()
            return 0
        self.clear()
        if not value.isascii():
            return 0
        if ":" in value:
            handle_text, count_text = value.split(":", 1)
        else:
            handle_text, count_text = value, "1"
        if not handle_text.isdecimal() or count_text not in ("1", "2"):
            return 0
        if not isinstance(foreground_window_handle, int):
            return 0
        recorded_window_handle = int(handle_text)
        if not (
            recorded_window_handle > 0
            and foreground_window_handle > 0
            and recorded_window_handle == foreground_window_handle
        ):
            return 0
        return int(count_text)


def get_foreground_window_handle() -> int:
    """Return the actual foreground top-level window handle without reading content."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        log.debug("Foreground-window handle lookup failed", exc_info=True)
        return 0


def uppercase_phrase_initial(text: str) -> str:
    """Uppercase only the first alphabetic character in a phrase."""
    return re.sub(
        r"[A-Za-z]",
        lambda match: match.group(0).upper(),
        text,
        count=1,
    )


_ALL_CAPS_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:[,.!?;:]\s*)?\ball caps (?P<explicit>on|off)\b(?:\s*[,.!?;:])?"
    r"|(?:[,.!?;:]\s*)?\b(?P<toggle>caps lock)\b"
    r"(?!\s+key\b)(?:\s*[,.!?;:])?"
    r")",
    re.IGNORECASE,
)
_BACKTICK_COMMAND = "\ue000"
_OPEN_QUOTE_COMMAND = "\ue001"
_CLOSE_QUOTE_COMMAND = "\ue002"
_PENDING_CLOSE_COMMAND = "\ue003"
_FORMAT_COMMAND_RE = re.compile(
    r"(?:[,.!?;:]\s*)?\b(caps on|caps off|cap)\b"
    r"(?:\s*[,.!?;:])?",
    re.IGNORECASE,
)
_DEFAULT_FORMATTING_TIMEOUT_S = 2.0
_DEFAULT_NUMBER_FLUSH_TIMEOUT_S = 0.1


class SessionEmissionHistory:
    """Bound exact one- through four-emission cycles within one recording."""

    def __init__(self, maximum: int = 16) -> None:
        self._items: deque[str] = deque(maxlen=maximum)
        self._suppressed_tail: deque[str] = deque()

    def reset(self) -> None:
        self._items.clear()
        self._suppressed_tail.clear()

    def accept(self, text: str) -> bool:
        normalized = " ".join(re.findall(r"[A-Za-z0-9]+", text.casefold()))
        if not normalized:
            return True
        if self._suppressed_tail:
            if normalized == self._suppressed_tail[0]:
                self._suppressed_tail.popleft()
                log.warning(
                    "Suppressed cross-utterance repetition tail: remaining=%d",
                    len(self._suppressed_tail),
                )
                return False
            self._suppressed_tail.clear()

        prior = list(self._items)
        for width in range(1, min(4, len(prior) // 2) + 1):
            cycle = prior[-width:]
            if prior[-2 * width : -width] == cycle and normalized == cycle[0]:
                self._suppressed_tail.extend(cycle[1:])
                log.warning(
                    "Suppressed cross-utterance repetition: cycle_emissions=%d",
                    width,
                )
                return False
        self._items.append(normalized)
        return True


class ClientReadinessState:
    """Own the explicit marker indicating model and hotkey readiness."""

    def __init__(self, marker: Path) -> None:
        self._marker = marker

    def loading(self) -> None:
        self._marker.unlink(missing_ok=True)

    def ready(self) -> None:
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        self._marker.write_text("ready\n", encoding="ascii")

    def stopped(self) -> None:
        self._marker.unlink(missing_ok=True)


def _notify_pathological_decoder_output(
    error: PathologicalDecoderOutput,
) -> None:
    """Notify without exposing decoder output or diagnostic payload."""
    log.info(
        "Discarded unsafe decoder output before injection: reason=%s",
        error.reason,
    )
    notify(
        "Dictation retry",
        "Unusual transcription discarded; please try again.",
    )


def _log_focus_rejection(context: CaretContext, boundary: str) -> None:
    target = context.target
    log.info(
        "Focus rejection: boundary=%s executable=%s window_class=%s "
        "control_type=%s text_pattern=%s value_pattern=%s error=%s",
        boundary,
        target.foreground_executable if target else "",
        target.foreground_window_class if target else "",
        target.control_type if target else "",
        target.text_pattern if target else False,
        target.value_pattern if target else False,
        target.error if target else "no_diagnostic",
    )


def _capitalize_word_initials(text: str, every_word: bool) -> tuple[str, bool]:
    """Capitalize one or all word initials; return whether one-word cap was used."""
    limit = 0 if every_word else 1
    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        if limit and changed >= limit:
            return match.group(0)
        changed += 1
        return match.group(1).upper()

    return re.sub(r"\b([A-Za-z])", replace, text), bool(changed)


def apply_formatting_commands(
    text: str,
    caps_active: bool,
    cap_next: bool,
) -> tuple[str, bool, bool]:
    """Apply line-break and title-casing commands with persistent state."""
    output: list[str] = []
    cursor = 0

    def append_words(portion: str) -> None:
        nonlocal cap_next
        if caps_active:
            portion, _ = _capitalize_word_initials(portion, every_word=True)
        elif cap_next:
            portion, used = _capitalize_word_initials(portion, every_word=False)
            cap_next = not used
        output.append(portion)

    for match in _FORMAT_COMMAND_RE.finditer(text):
        append_words(text[cursor:match.start()])
        command = match.group(1).lower()
        if command == "caps on":
            caps_active = True
        elif command == "caps off":
            caps_active = False
        else:
            cap_next = True
        cursor = match.end()
    append_words(text[cursor:])

    cleaned = "".join(output)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(" \t"), caps_active, cap_next


def apply_all_caps_commands(text: str, active: bool) -> tuple[str, bool]:
    """Apply stateful all-caps commands and return cleaned text plus new state."""
    output: list[str] = []
    cursor = 0
    for match in _ALL_CAPS_COMMAND_RE.finditer(text):
        portion = text[cursor:match.start()]
        output.append(portion.upper() if active else portion)
        command = match.group("explicit")
        active = not active if command is None else command.lower() == "on"
        cursor = match.end()

    portion = text[cursor:]
    output.append(portion.upper() if active else portion)
    cleaned = "".join(output)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"([\(\[])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([\)\]])", r"\1", cleaned)
    return cleaned, active


def apply_backtick_commands(text: str, active: bool) -> tuple[str, bool]:
    """Pair spoken backtick commands across decoded phrase boundaries."""
    output: list[str] = []
    pieces = text.split(_BACKTICK_COMMAND)
    for index, piece in enumerate(pieces):
        if index == 0:
            output.append(piece)
            continue

        if active:
            while output and output[-1].endswith((" ", "\t")):
                output[-1] = output[-1].rstrip(" \t")
            output.append("`")
            output.append(piece)
        else:
            if output and output[-1] and not output[-1][-1].isspace():
                output.append(" ")
            output.append("`")
            output.append(piece.lstrip())
        active = not active
    return "".join(output).strip(" \t"), active


@dataclass(frozen=True)
class QuoteState:
    """Smart-quote state shared by committed phrases in one recording."""

    open: bool = False
    pending_close: bool = False


def apply_quote_commands(text: str, state: QuoteState) -> tuple[str, QuoteState]:
    """Resolve smart-quote commands, including close/quote split across phrases."""
    if text == _PENDING_CLOSE_COMMAND:
        if state.open:
            return "", QuoteState(open=True, pending_close=True)
        return "close", state

    quote_open = state.open
    if state.pending_close:
        stripped = text.lstrip()
        if stripped.startswith(_OPEN_QUOTE_COMMAND):
            remainder = stripped[len(_OPEN_QUOTE_COMMAND):].lstrip()
            separator = (
                " "
                if remainder and remainder[0] not in ".,!?;:%)]}\u2019\u201d\u2014\u2026"
                else ""
            )
            text = _CLOSE_QUOTE_COMMAND + separator + remainder
        else:
            text = "close " + text

    output: list[str] = []
    skip_spaces = False
    for character in text:
        if skip_spaces and character.isspace():
            continue
        skip_spaces = False

        if character == _OPEN_QUOTE_COMMAND:
            if output and not output[-1].isspace() and output[-1] not in "([{":
                output.append(" ")
            output.append("“")
            skip_spaces = True
            quote_open = True
        elif character == _CLOSE_QUOTE_COMMAND:
            while output and output[-1].isspace():
                output.pop()
            output.append("”")
            quote_open = False
        else:
            output.append(character)
    return "".join(output).strip(" \t"), QuoteState(open=quote_open)


def _play_toggle_tone(active: bool) -> None:
    """Play the configured best-effort Windows cue asynchronously."""
    if sys.platform != "win32":
        return

    try:
        import os
        import winsound

        filename = (
            "local_dictation_start.wav"
            if active
            else "local_dictation_stop.wav"
        )
        sound_path = (
            Path(os.environ["LOCALAPPDATA"])
            / "breezy_local_streaming_dictation"
            / "assets"
            / filename
        )
        winsound.PlaySound(
            str(sound_path),
            winsound.SND_FILENAME
            | winsound.SND_ASYNC
            | winsound.SND_NODEFAULT,
        )
    except Exception:
        log.debug("Toggle sound failed; using tone fallback", exc_info=True)
        try:
            winsound.Beep(880 if active else 440, 120)
        except Exception:
            log.debug("Toggle tone fallback failed", exc_info=True)


def _set_recording_indicator(active: bool) -> None:
    """Publish actual capture state for the supervised tray icon."""
    if sys.platform != "win32":
        return
    try:
        import os

        state_path = (
            Path(os.environ["LOCALAPPDATA"])
            / "breezy_local_streaming_dictation"
            / "recording.active"
        )
        if active:
            state_path.write_text("recording\n", encoding="ascii")
        else:
            state_path.unlink(missing_ok=True)
    except Exception:
        log.debug("Recording-state indicator update failed", exc_info=True)


class DictationDaemon:
    """Main dictation daemon.

    Listens for hotkey, captures audio, detects speech via VAD,
    transcribes via engine, and types into the focused application.
    """

    def __init__(self, config: Config, streaming: bool = False):
        self.config = config
        self.streaming = streaming
        self._engine: TranscriptionEngine = create_engine(config)
        self._conversion_store = ConversionStore()

        # WebSocket streaming: server handles VAD, text arrives via callback
        # Local streaming: local VAD splits utterances, REST/local engine transcribes
        self._use_ws = streaming and config.engine.type == "server"
        self._ws_engine: WhisperLiveKitEngine | None = None

        # VAD only needed for local-engine streaming or batch mode
        self._vad = SpeechDetector(
            sample_rate=config.audio.sample_rate,
            threshold=config.vad.threshold,
            silence_ms=config.vad.silence_ms,
            min_speech_ms=config.vad.min_speech_ms,
            max_speech_s=config.vad.max_speech_s,
            pre_speech_ms=config.vad.pre_speech_ms,
        )
        self._audio: AudioStream | None = None
        self._hotkey: HotkeyListener | None = None
        self._running = threading.Event()
        self._stop_event = threading.Event()
        self._recording = False
        self._finalization_pending = False
        self._recording_start: float = 0.0
        self._recorded_chunks: list[np.ndarray] = []
        # Cap batch audio buffer to prevent unbounded memory growth.
        # At 32ms blocks: 90s max → 2812 chunks (~5.6 MB at 16kHz float32).
        self._max_batch_chunks = int(config.vad.max_speech_s / 0.032)
        self._lock = threading.Lock()
        self._transcribe_pool = ThreadPoolExecutor(max_workers=1)
        # Repetition filter for streaming: suppress hallucination loops
        # (e.g. Whisper repeating "Thank you" during silence).
        self._last_ws_text: str = ""
        self._ws_repeat_count: int = 0
        self._WS_MAX_REPEATS: int = 2  # allow 2 identical, suppress from 3rd
        # Defer inter-chunk whitespace until the next committed chunk. This
        # lets a user add punctuation immediately after committed text.
        self._ws_has_output: bool = False
        self._ws_last_char: str = ""
        self._all_caps_active: bool = False
        self._caps_active: bool = False
        self._cap_next: bool = False
        self._backtick_active: bool = False
        self._quote_state = QuoteState()
        self._manual_line_break_signal = ManualLineBreakSignal()
        self._number_normalizer = StreamingNumberNormalizer()
        self._number_lock = threading.Lock()
        self._number_flush_timer: threading.Timer | None = None
        self._number_flush_generation = 0
        self._number_flush_timeout_s = _DEFAULT_NUMBER_FLUSH_TIMEOUT_S
        self._automatic_punctuation_for_recording = config.formatting.automatic_punctuation
        self._capitalize_new_paragraphs = config.formatting.capitalize_new_paragraphs
        self._capitalize_new_lines = config.formatting.capitalize_new_lines
        self._pending_spoken_boundary = "none"
        runtime_config_path = os.environ.get("BREEZY_LOCAL_DICTATION_CONFIG_FILE")
        self._runtime_config_path = (
            Path(runtime_config_path) if runtime_config_path else None
        )
        self._formatting_timeout_s: float = _DEFAULT_FORMATTING_TIMEOUT_S
        self._last_speech_end_time: float = 0.0
        self._emission_history = SessionEmissionHistory()
        ready_path = os.environ.get("BREEZY_LOCAL_DICTATION_READY_FILE")
        self._readiness = ClientReadinessState(
            Path(ready_path) if ready_path else CONFIG_DIR / "client.ready"
        )

    def _remove_prompt_leak(self, text: str) -> str:
        return remove_configured_prompt_leak(text, self.config.server.prompt)

    def _create_ws_engine(
        self, **kwargs: object,
    ) -> WhisperLiveKitEngine:
        """Create the WhisperLiveKit WebSocket engine."""
        return create_ws_engine(self.config, **kwargs)  # type: ignore[return-value]

    def _apply_user_conversions(
        self,
        text: str,
        *,
        match_text: str | None = None,
    ) -> str:
        """Apply user rules once without weakening existing safety gates."""
        store = getattr(self, "_conversion_store", None)
        if store is None:
            store = ConversionStore()
            self._conversion_store = store
        try:
            return store.apply(text, match_text=match_text)
        except Exception:
            log.error("Text conversion application failed", exc_info=True)
            return text

    def _refresh_capitalization_settings(self) -> None:
        """Refresh only next-utterance capitalization controls from runtime TOML."""
        if self._runtime_config_path is None or not self._runtime_config_path.exists():
            return
        try:
            formatting = load_config(self._runtime_config_path).formatting
            self._capitalize_new_paragraphs = formatting.capitalize_new_paragraphs
            self._capitalize_new_lines = formatting.capitalize_new_lines
        except Exception:
            log.warning(
                "Could not refresh capitalization settings; retaining prior values."
            )

    def _apply_boundary_casing_and_conversions(
        self,
        text: str,
        caret_context: CaretContext,
        manual_break_count: int = 0,
        *,
        preserve_when_none: bool = False,
    ) -> str:
        """Case output from uncased match text, then apply corrections exactly once."""
        self._refresh_capitalization_settings()
        boundary = classify_casing_context(caret_context, manual_break_count)
        cased, match_text, pending_boundary = format_spoken_boundaries(
            text,
            initial_boundary=boundary,
            pending_boundary=self._pending_spoken_boundary,
            capitalize_new_paragraphs=self._capitalize_new_paragraphs,
            capitalize_new_lines=self._capitalize_new_lines,
            preserve_initial_when_none=preserve_when_none,
        )
        self._pending_spoken_boundary = pending_boundary
        return self._apply_user_conversions(cased, match_text=match_text)

    def _normalize_numbers(self, text: str) -> str:
        if not self.streaming:
            return normalize_spoken_numbers(text)
        with self._number_lock:
            self._cancel_number_flush_locked()
            return self._number_normalizer.feed(text)

    def _cancel_number_flush_locked(self) -> None:
        self._number_flush_generation += 1
        if self._number_flush_timer is not None:
            self._number_flush_timer.cancel()
            self._number_flush_timer = None

    def _reset_number_normalizer(self) -> None:
        with self._number_lock:
            self._cancel_number_flush_locked()
            self._number_normalizer.reset()

    def _defer_pending_number_flush_for_speech(self) -> None:
        with self._number_lock:
            if self._number_flush_timer is not None:
                self._cancel_number_flush_locked()

    def _has_pending_number(self) -> bool:
        with self._number_lock:
            return self._number_normalizer.has_pending

    def _schedule_pending_number_flush(self, expected_target: object) -> None:
        with self._number_lock:
            if not self._number_normalizer.has_pending:
                return
            self._cancel_number_flush_locked()
            generation = self._number_flush_generation
            timer = threading.Timer(
                self._number_flush_timeout_s,
                self._flush_pending_number_and_type,
                kwargs={
                    "expected_target": expected_target,
                    "generation": generation,
                },
            )
            timer.daemon = True
            self._number_flush_timer = timer
            timer.start()

    def _consume_manual_line_break(self) -> int:
        if not self._manual_line_break_signal.path.exists():
            return 0
        return self._manual_line_break_signal.consume(
            get_foreground_window_handle()
        )

    def _discard_manual_line_break(self) -> None:
        if self._manual_line_break_signal.path.exists():
            self._manual_line_break_signal.clear()

    def _finalize_recording_boundaries(self) -> None:
        """Clear manual and spoken boundary state after queued final output."""
        self._manual_line_break_signal.clear()
        with self._lock:
            self._pending_spoken_boundary = "none"
            self._finalization_pending = False

    def _flush_pending_number_and_type(
        self,
        *,
        expected_target: object | None = None,
        generation: int | None = None,
    ) -> None:
        with self._number_lock:
            if generation is not None and generation != self._number_flush_generation:
                return
            self._cancel_number_flush_locked()
            text = self._number_normalizer.flush()
        if not text:
            return
        caret_context = get_caret_context()
        if (
            not caret_context.injection_allowed
            or (
                expected_target is not None
                and caret_context.target != expected_target
            )
        ):
            self._discard_manual_line_break()
            _log_focus_rejection(caret_context, "pending_number")
            return
        manual_line_break = self._consume_manual_line_break()
        text = self._apply_boundary_casing_and_conversions(
            text,
            caret_context,
            manual_line_break,
        )
        injection = self._prepare_ws_injection(
            text,
            caret_context=caret_context,
            manual_line_break=manual_line_break,
        )
        if injection:
            type_text(injection, expected_target=caret_context.target)

    def _on_audio_chunk(self, audio: np.ndarray) -> None:
        """Called for each audio chunk from the microphone.

        Note: reads _recording without the lock for performance in the
        audio callback hot path. Safe on CPython due to the GIL."""
        if not self._recording:
            return

        ws_engine = self._ws_engine  # snapshot to avoid race with deactivate
        if self._use_ws and ws_engine is not None:
            try:
                ws_engine.send_audio(audio)
            except Exception:
                log.error("WS audio send failed", exc_info=True)
        elif self.streaming:
            self._on_audio_chunk_streaming(audio)
        else:
            with self._lock:
                if len(self._recorded_chunks) >= self._max_batch_chunks:
                    return  # buffer full, drop chunk silently
                self._recorded_chunks.append(audio.copy())

    def _on_audio_chunk_streaming(self, audio: np.ndarray) -> None:
        """Streaming mode: VAD splits speech, each utterance transcribed immediately."""
        try:
            complete, utterance = self._vad.process_chunk(audio)
        except Exception:
            log.error("VAD processing failed", exc_info=True)
            return

        if self._vad.is_speaking:
            self._defer_pending_number_flush_for_speech()

        if complete and utterance is not None:
            utterance_end_time = time.monotonic()
            self._transcribe_pool.submit(
                self._transcribe_and_type,
                utterance,
                utterance_end_time,
            )

    def _on_ws_text(self, text: str) -> None:
        """Callback from WebSocket engine when transcription text arrives.

        Called from the WS receiver thread. Uses _lock for the repeat-check
        to avoid racing with _on_activate which resets the counters.
        """
        if not text or not text.strip():
            return
        try:
            cleaned = guard_pathological_decoder_output(
                self._remove_prompt_leak(text)
            ).strip()
        except PathologicalDecoderOutput as error:
            _notify_pathological_decoder_output(error)
            return
        if not cleaned:
            return
        cleaned = apply_automatic_punctuation_setting(
            cleaned,
            self._automatic_punctuation_for_recording,
            preserve_spoken_boundaries=True,
        )
        cleaned = self._normalize_numbers(cleaned)
        caret_context = get_caret_context()
        if not caret_context.injection_allowed:
            if self._has_pending_number():
                self._reset_number_normalizer()
            self._discard_manual_line_break()
            _log_focus_rejection(caret_context, "websocket_stream")
            return
        if self._has_pending_number():
            self._schedule_pending_number_flush(caret_context.target)
        if not cleaned:
            return
        cleaned = guard_pathological_repetition(cleaned)
        uncased_cleaned = cleaned
        # Suppress hallucination loops (e.g. "Thank you" repeated during silence).
        # Allow up to _WS_MAX_REPEATS identical emissions, then suppress.
        normalized = uncased_cleaned.lower().rstrip(".,!?")
        with self._lock:
            if normalized == self._last_ws_text:
                self._ws_repeat_count += 1
                if self._ws_repeat_count >= self._WS_MAX_REPEATS:
                    log.debug("Suppressed repeated text (count=%d)", self._ws_repeat_count)
                    return
            else:
                self._last_ws_text = normalized
                self._ws_repeat_count = 0
        manual_line_break = self._consume_manual_line_break()
        cleaned = self._apply_boundary_casing_and_conversions(
            uncased_cleaned,
            caret_context,
            manual_line_break,
        )
        try:
            type_text(
                self._prepare_ws_injection(
                    cleaned,
                    caret_context=caret_context,
                    manual_line_break=manual_line_break,
                ),
                expected_target=caret_context.target,
            )
            log.debug("WS typed: %d chars", len(cleaned))
        except Exception:
            log.error("Typing failed", exc_info=True)

    def _prepare_ws_injection(
        self,
        cleaned: str,
        caret_context: CaretContext | None = None,
        manual_line_break: bool = False,
    ) -> str:
        """Join text using actual caret surroundings when UIA is available."""
        no_leading_space = (
            ".,!?;:/%)]}'\u2019\u201d\u2014\u2026_" + EXPLICIT_DOT_COMMAND
        )
        no_space_after = "/([{'\"\u2018\u201c\u2014_" + EXPLICIT_DOT_COMMAND
        no_trailing_space_before = (
            ".,!?;:/%)]}'\u2019\u201d\u2014\u2026_" + EXPLICIT_DOT_COMMAND
        )
        with self._lock:
            previous_char = self._ws_last_char
            if caret_context is not None and caret_context.available:
                previous_char = (
                    ""
                    if caret_context.is_empty_document
                    else caret_context.before_char
                )
                if (
                    self._ws_last_char == EXPLICIT_DOT_COMMAND
                    and previous_char == "."
                ):
                    # Preserve explicit `dot` intent after the marker has
                    # already been rendered into the editor's literal value.
                    previous_char = EXPLICIT_DOT_COMMAND

            if (
                previous_char in ",?"
                and cleaned.startswith(previous_char)
            ):
                cleaned = cleaned.lstrip(previous_char)
                if not cleaned:
                    return ""

            if caret_context is not None and caret_context.available:
                needs_space = (
                    not caret_context.has_selection
                    and not manual_line_break
                    and bool(previous_char)
                    and not previous_char.isspace()
                    and not cleaned[0].isspace()
                    and cleaned[0] not in no_leading_space
                    and previous_char not in no_space_after
                    and not (
                        previous_char == "`"
                        and self._backtick_active
                    )
                )
                needs_trailing_space = (
                    not caret_context.has_selection
                    and not caret_context.is_empty_document
                    and bool(caret_context.after_char)
                    and not caret_context.after_char.isspace()
                    and caret_context.after_char not in no_trailing_space_before
                    and not cleaned[-1].isspace()
                    and cleaned[-1] not in no_space_after
                )
            else:
                needs_space = (
                    self._ws_has_output
                    and not manual_line_break
                    and not self._ws_last_char.isspace()
                    and not cleaned[0].isspace()
                    and cleaned[0] not in no_leading_space
                    and self._ws_last_char not in no_space_after
                    and not (
                        self._ws_last_char == "`"
                        and self._backtick_active
                    )
                )
                needs_trailing_space = False

            self._ws_has_output = True
            self._ws_last_char = cleaned[-1]
        return (
            (" " if needs_space else "")
            + cleaned.replace(EXPLICIT_DOT_COMMAND, ".")
            + (" " if needs_trailing_space else "")
        )

    def _transcribe_and_type(
        self,
        audio: np.ndarray,
        utterance_end_time: float | None = None,
        automatic_punctuation: bool | None = None,
    ) -> None:
        """Transcribe audio and type the result."""
        if utterance_end_time is None:
            utterance_end_time = time.monotonic()
        audio_duration = len(audio) / self.config.audio.sample_rate
        speech_start_time = utterance_end_time - audio_duration
        if automatic_punctuation is None:
            automatic_punctuation = self._automatic_punctuation_for_recording

        try:
            if isinstance(self._engine, LocalEngine):
                raw_text = self._engine.transcribe(
                    audio,
                    self.config.audio.sample_rate,
                    vad_filter=not self.streaming,
                    automatic_punctuation=automatic_punctuation,
                    normalize_numbers=not self.streaming,
                    preserve_spoken_boundaries=True,
                )
            else:
                raw_text = self._engine.transcribe(
                    audio,
                    self.config.audio.sample_rate,
                )
                raw_text = apply_automatic_punctuation_setting(
                    raw_text,
                    automatic_punctuation,
                    preserve_spoken_boundaries=True,
                )
            text = guard_pathological_decoder_output(
                self._remove_prompt_leak(raw_text)
            )
            text = guard_pathological_repetition(self._normalize_numbers(text))
            caret_context = get_caret_context()
            if not caret_context.injection_allowed:
                if self._has_pending_number():
                    self._reset_number_normalizer()
                self._discard_manual_line_break()
                _log_focus_rejection(caret_context, "local")
                return
            if self._has_pending_number():
                self._schedule_pending_number_flush(caret_context.target)
            if not text:
                return
            if (
                self.streaming
                and not self._use_ws
                and not self._emission_history.accept(text)
            ):
                return
            manual_line_break = False
            with self._lock:
                if (
                    self._last_speech_end_time > 0.0
                    and (speech_start_time - self._last_speech_end_time)
                    >= self._formatting_timeout_s
                ):
                    self._all_caps_active = False
                    self._caps_active = False
                    self._cap_next = False

                original_all_caps = self._all_caps_active
                original_caps = self._caps_active
                original_cap_next = self._cap_next
                original_backtick = self._backtick_active
                original_quote = self._quote_state

                preview, preview_all_caps = apply_all_caps_commands(
                    text,
                    original_all_caps,
                )
                preview, preview_caps, preview_cap_next = apply_formatting_commands(
                    preview,
                    original_caps,
                    original_cap_next,
                )
                preview, preview_backtick = apply_backtick_commands(
                    preview,
                    original_backtick,
                )
                preview, preview_quote = apply_quote_commands(
                    preview,
                    original_quote,
                )

                if preview and self.streaming:
                    manual_line_break = self._consume_manual_line_break()
                if preview:
                    formatting_casing_active = any(
                        (
                            original_all_caps,
                            original_caps,
                            original_cap_next,
                            preview_all_caps,
                            preview_caps,
                            preview_cap_next,
                        )
                    )
                    text = self._apply_boundary_casing_and_conversions(
                        preview,
                        caret_context,
                        manual_line_break,
                        preserve_when_none=formatting_casing_active,
                    )
                else:
                    text = ""

                self._all_caps_active = preview_all_caps
                self._caps_active = preview_caps
                self._cap_next = preview_cap_next
                self._backtick_active = preview_backtick
                self._quote_state = preview_quote
                self._last_speech_end_time = utterance_end_time
            if text:
                injection = (
                    self._prepare_ws_injection(
                        text,
                        caret_context=caret_context,
                        manual_line_break=manual_line_break,
                    )
                    if self.streaming
                    else text.replace(EXPLICIT_DOT_COMMAND, ".") + " "
                )
                type_text(injection, expected_target=caret_context.target)
                log.debug("Typed: %d chars", len(text))
            elif not self.streaming:
                log.info("No speech detected")
        except PathologicalDecoderOutput as error:
            _notify_pathological_decoder_output(error)
        except Exception:
            log.error("Transcription or typing failed", exc_info=True)

    def _deactivate_ws(
        self, ws_engine: WhisperLiveKitEngine, rec_duration: float,
    ) -> None:
        """Finalize WS streaming session in a background thread.

        Called via _transcribe_pool so the hotkey listener thread is not
        blocked by sleep + wait_for_completion.
        """
        try:
            # FIFO queue guarantees EOA is sent after all queued audio.
            ws_engine.flush(send_eoa=True)
            if not ws_engine.wait_for_completion(timeout=3.0):
                log.debug("WS streaming finalization timed out")
            pending = ws_engine.get_pending_text()
            if pending:
                log.debug("Emitting pending partial text: %d chars", len(pending))
                # Type directly — bypass repetition filter since pending
                # text is the final unstreamed word, not a hallucination.
                try:
                    caret_context = get_caret_context()
                    if not caret_context.injection_allowed:
                        self._discard_manual_line_break()
                        _log_focus_rejection(caret_context, "websocket_pending")
                        return
                    cleaned = guard_pathological_decoder_output(
                        self._remove_prompt_leak(pending)
                    )
                    if not cleaned.strip():
                        return
                    cleaned = apply_automatic_punctuation_setting(
                        cleaned.strip(),
                        self._automatic_punctuation_for_recording,
                        preserve_spoken_boundaries=True,
                    )
                    cleaned = self._normalize_numbers(cleaned)
                    if not cleaned:
                        return
                    cleaned = guard_pathological_repetition(cleaned)
                    pre_correction_text = cleaned
                    manual_line_break = self._consume_manual_line_break()
                    cleaned = self._apply_boundary_casing_and_conversions(
                        pre_correction_text,
                        caret_context,
                        manual_line_break,
                    )
                    type_text(
                        self._prepare_ws_injection(
                            cleaned,
                            caret_context=caret_context,
                            manual_line_break=manual_line_break,
                        ),
                        expected_target=caret_context.target,
                    )
                except PathologicalDecoderOutput as error:
                    _notify_pathological_decoder_output(error)
                except Exception:
                    log.error("Typing pending text failed", exc_info=True)
        except Exception:
            log.error("WS streaming deactivation failed", exc_info=True)
        finally:
            try:
                self._flush_pending_number_and_type()
            except Exception:
                log.error("Typing pending number failed", exc_info=True)
            self._finalize_recording_boundaries()
            ws_engine.close()

    def _transcribe_batch_ws(self, audio: np.ndarray) -> None:
        """Transcribe via WebSocket batch mode."""
        try:
            ws_cfg = self.config.websocket
            ws = self._create_ws_engine(
                server_url=self.config.server.url,
                language=self.config.server.language,
                reconnect_attempts=ws_cfg.reconnect_attempts,
                reconnect_delay=ws_cfg.reconnect_delay,
            )
            text = ws.transcribe_batch(audio, self.config.audio.sample_rate)
        except Exception:
            log.error("WS batch transcription failed", exc_info=True)
            notify("Error", "Transcription failed")
            return
        try:
            if text:
                text = guard_pathological_decoder_output(
                    self._remove_prompt_leak(text)
                )
                if not text.strip():
                    return
                caret_context = get_caret_context()
                if not caret_context.injection_allowed:
                    _log_focus_rejection(caret_context, "websocket_batch")
                    return
                text = normalize_spoken_numbers(text)
                text = apply_automatic_punctuation_setting(
                    text,
                    self._automatic_punctuation_for_recording,
                    preserve_spoken_boundaries=True,
                )
                manual_line_break = self._consume_manual_line_break()
                text = self._apply_boundary_casing_and_conversions(
                    text,
                    caret_context,
                    manual_line_break,
                )
                type_text(text + " ", expected_target=caret_context.target)
                log.debug("WS batch typed: %d chars", len(text))
            else:
                log.info("No speech detected (WS batch)")
        except PathologicalDecoderOutput as error:
            _notify_pathological_decoder_output(error)
        except Exception:
            log.error("Typing failed after WS batch transcription", exc_info=True)

    def _on_activate(self) -> None:
        """Hotkey pressed — start recording."""
        with self._lock:
            if self._recording:
                return
            if self._finalization_pending:
                notify(
                    "Finishing dictation",
                    "Wait for the previous dictation to finish, then try again.",
                )
                return
            self._manual_line_break_signal.clear()
            self._recording = True
            try:
                if (
                    self._runtime_config_path is not None
                    and self._runtime_config_path.exists()
                ):
                    self._automatic_punctuation_for_recording = load_config(
                        self._runtime_config_path
                    ).formatting.automatic_punctuation
            except Exception:
                log.warning("Could not refresh automatic punctuation setting; retaining prior value.")
            self._recorded_chunks.clear()
            self._recording_start = time.monotonic()
            self._last_ws_text = ""
            self._ws_repeat_count = 0
            self._ws_has_output = False
            self._ws_last_char = ""
            self._all_caps_active = False
            self._caps_active = False
            self._cap_next = False
            self._backtick_active = False
            self._quote_state = QuoteState()
            self._pending_spoken_boundary = "none"
            self._last_speech_end_time = 0.0
            self._emission_history.reset()
        if self.streaming and not self._use_ws:
            # Keep the reset behind any prior recording's queued fragments and
            # final flush, while still placing it before this recording's VAD work.
            self._transcribe_pool.submit(self._reset_number_normalizer)
        else:
            self._reset_number_normalizer()
        log.info("Recording started (use_ws=%s, streaming=%s, engine=%s)",
                 self._use_ws, self.streaming, self.config.engine.type)

        ws_engine = None
        if self._use_ws:
            ws_cfg = self.config.websocket
            ws_engine = self._create_ws_engine(
                server_url=self.config.server.url,
                language=self.config.server.language,
                reconnect_attempts=ws_cfg.reconnect_attempts,
                reconnect_delay=ws_cfg.reconnect_delay,
                on_text=self._on_ws_text,
            )
            try:
                ws_engine.connect()
            except Exception:
                log.error("WebSocket connection failed", exc_info=True)
                ws_engine.close()
                with self._lock:
                    self._recording = False
                notify("Error", "WebSocket connection failed")
                return
        elif self.streaming:
            self._vad.reset()

        # Set ws_engine under lock before audio starts so callbacks can see it
        with self._lock:
            self._ws_engine = ws_engine

        audio = AudioStream(self.config.audio, self._on_audio_chunk)
        try:
            audio.start()
        except Exception:
            log.error("Failed to start audio capture", exc_info=True)
            audio.stop()
            if ws_engine is not None:
                ws_engine.close()
            with self._lock:
                self._ws_engine = None
                self._recording = False
                self._audio = None
            notify("Error", "Could not access microphone")
            return

        with self._lock:
            self._audio = audio
        _set_recording_indicator(active=True)
        _play_toggle_tone(active=True)
        notify("Recording", "Speak now...")

    def _on_deactivate(self) -> None:
        """Hotkey released/toggled — stop recording and transcribe."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._finalization_pending = True
            chunks = list(self._recorded_chunks)
            self._recorded_chunks.clear()
            audio = self._audio
            self._audio = None
            # Capture WS engine under lock to prevent race with stop()
            ws_engine = self._ws_engine
            self._ws_engine = None
            rec_start = self._recording_start

        rec_duration = max(0.0, time.monotonic() - rec_start)
        log.info("Recording stopped (%.1fs)", rec_duration)
        _set_recording_indicator(active=False)
        _play_toggle_tone(active=False)

        if audio is not None:
            audio.stop()

        if self._use_ws and ws_engine is not None:
            notify("Transcribing", f"{rec_duration:.0f}s of audio...")
            # Dispatch WS flush/close to thread pool so the hotkey listener
            # thread is not blocked (sleep + wait_for_completion can take 3s+).
            self._transcribe_pool.submit(
                self._deactivate_ws, ws_engine, rec_duration,
            )
        elif self.streaming:
            # Local-engine streaming: flush remaining VAD-buffered speech
            remaining = self._vad.flush()
            self._vad.log_session_summary()
            if remaining is not None:
                utterance_end_time = time.monotonic()
                self._transcribe_pool.submit(
                    self._transcribe_and_type,
                    remaining,
                    utterance_end_time,
                )
            self._transcribe_pool.submit(self._flush_pending_number_and_type)
            self._transcribe_pool.submit(self._finalize_recording_boundaries)
        elif chunks:
            # Batch mode: transcribe the full recording at once
            full_audio = np.concatenate(chunks)
            duration = len(full_audio) / self.config.audio.sample_rate
            log.info("Transcribing %.1fs of audio...", duration)
            notify("Transcribing", f"{duration:.0f}s of audio...")
            if self.config.engine.type == "server":
                self._transcribe_pool.submit(self._transcribe_batch_ws, full_audio)
            else:
                self._transcribe_pool.submit(
                    self._transcribe_and_type,
                    full_audio,
                    time.monotonic(),
                )
            self._transcribe_pool.submit(self._finalize_recording_boundaries)
        else:
            log.info("No audio recorded")
            self._finalize_recording_boundaries()

    def _check_server_available(self) -> bool:
        """Check if the transcription server is reachable."""
        # Try REST health check first (works with OpenAI-compatible servers)
        if self._engine.is_available():
            return True
        # Fall back to WebSocket probe (WhisperLiveKit may lack /health endpoint)
        if self.config.engine.type == "server":
            ws = self._create_ws_engine(
                server_url=self.config.server.url,
                language=self.config.server.language,
                reconnect_attempts=0,  # probe: fail fast, do not retry
                reconnect_delay=self.config.websocket.reconnect_delay,
            )
            try:
                return ws.is_available()
            finally:
                ws.close()
        return False

    def start(self) -> None:
        """Start the dictation daemon."""
        self._readiness.loading()
        _set_recording_indicator(active=False)
        if not self._check_server_available():
            engine_type = self.config.engine.type
            if engine_type == "server":
                log.error(
                    "Server not reachable at %s. Start with: wlk --model large-v3",
                    self.config.server.url,
                )
            else:
                log.error("Local engine not available.")
            notify("Error", "Transcription engine not available")
            self._readiness.stopped()
            raise RuntimeError("Transcription engine not available")

        # Start hotkey listener
        self._hotkey = HotkeyListener(
            binding=self.config.hotkey.binding,
            mode=self.config.hotkey.mode,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )
        self._hotkey.start()

        self._readiness.ready()

        self._running.set()
        mode = self.config.hotkey.mode
        binding = self.config.hotkey.binding
        engine = self.config.engine.type
        log.info(
            "Dictation daemon started: hotkey=%s, mode=%s, engine=%s",
            binding,
            mode,
            engine,
        )
        notify(
            "Dictation Ready",
            f"Press {binding} to {'start/stop' if mode == 'toggle' else 'hold and speak'}",
        )

    def stop(self) -> None:
        """Stop the dictation daemon."""
        self._readiness.stopped()
        self._running.clear()
        self._stop_event.set()
        _set_recording_indicator(active=False)

        # Stop hotkey FIRST to prevent concurrent _on_deactivate from hotkey thread
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

        with self._lock:
            should_deactivate = self._recording
        if should_deactivate:
            self._on_deactivate()

        # Capture under lock — _on_deactivate may have already cleared it
        with self._lock:
            ws_engine = self._ws_engine
            self._ws_engine = None
        if ws_engine is not None:
            ws_engine.close()
        self._transcribe_pool.shutdown(wait=True, cancel_futures=True)
        self._engine.close()
        log.info("Dictation daemon stopped")
        notify("Dictation Stopped", "Daemon exited")

    def request_stop(self) -> None:
        """Signal the daemon to stop. Safe to call from a signal handler."""
        self._stop_event.set()

    def wait(self) -> None:
        """Block until the daemon is stopped or a stop is requested."""
        try:
            self._running.wait()
            while self._running.is_set():
                if self._stop_event.wait(timeout=1.0):
                    break
        except KeyboardInterrupt:
            log.info("Interrupted")

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

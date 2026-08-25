"""Voice Activity Detection using Silero VAD."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import deque
from urllib.parse import urlparse

import numpy as np

log = logging.getLogger(__name__)
_SILERO_CHUNK_MS = 32
_ONNX_MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"
_DEFAULT_ONNX_MODEL_URL = (
    "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
)
_ONNX_MODEL_URL = os.environ.get("DICTATION_VAD_MODEL_URL", _DEFAULT_ONNX_MODEL_URL)
_VERIFY_HASH = os.environ.get("DICTATION_VAD_VERIFY_HASH", "").lower() in (
    "1",
    "true",
    "yes",
)
if _ONNX_MODEL_URL != _DEFAULT_ONNX_MODEL_URL:
    _parsed = urlparse(_ONNX_MODEL_URL)
    if _parsed.scheme not in ("http", "https"):
        raise ValueError(
            "DICTATION_VAD_MODEL_URL must use http or https scheme, "
            f"got: {_parsed.scheme!r}"
        )

_model: OnnxVAD | object | None = None
_model_lock = threading.Lock()


def _load_model() -> None:
    """Load the VAD model lazily and thread-safely."""
    global _model
    if _model is not None:
        return
    with _model_lock:
        if _model is not None:
            return
        log.info("Loading Silero VAD through ONNX Runtime")
        _load_onnx_model()


def _verify_model_hash(path: str | bytes, expected: str) -> None:
    """Verify SHA-256 of a downloaded model."""
    from pathlib import Path

    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if actual != expected:
        Path(path).unlink(missing_ok=True)
        raise RuntimeError(
            f"Model integrity check failed: expected {expected[:16]}..., "
            f"got {actual[:16]}...; file deleted"
        )


def _load_onnx_model() -> None:
    """Load Silero VAD directly with ONNX Runtime."""
    global _model
    import urllib.request
    from pathlib import Path

    from platformdirs import user_cache_dir

    from .config import APP_NAME

    cache = Path(user_cache_dir(APP_NAME)) / "silero_vad.onnx"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = cache.with_suffix(".tmp")
        log.info("Downloading Silero VAD ONNX model")
        try:
            maximum_bytes = 50 * 1024 * 1024
            with urllib.request.urlopen(_ONNX_MODEL_URL, timeout=60) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise RuntimeError(
                            f"VAD model download exceeds {maximum_bytes} bytes"
                        )
                    chunks.append(chunk)
                temporary.write_bytes(b"".join(chunks))
            if _VERIFY_HASH:
                _verify_model_hash(temporary, _ONNX_MODEL_SHA256)
            temporary.replace(cache)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        _model = OnnxVAD(str(cache))
    except Exception:
        cache.unlink(missing_ok=True)
        log.error("Failed to load ONNX model; deleted cache %s", cache)
        raise
    log.info("Silero VAD ONNX model loaded")


class OnnxVAD:
    """Minimal Silero VAD v5 ONNX wrapper."""

    def __init__(self, model_path: str) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(model_path, sess_options=options)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def reset_states(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, audio: np.ndarray, sample_rate: int) -> float:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32) / 32768.0
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]
        inputs = {
            "input": audio,
            "state": self._state,
            "sr": (
                np.array(sample_rate, dtype=np.int64)
                if int(self._sr) != sample_rate
                else self._sr
            ),
        }
        output, new_state = self.session.run(None, inputs)
        self._state = new_state
        return float(output[0][0])


class SpeechDetector:
    """Split streaming audio into VAD-delimited utterances."""

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        silence_ms: int = 800,
        min_speech_ms: int = 250,
        max_speech_s: float = 90.0,
        pre_speech_ms: int = 256,
    ) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.silence_chunks = silence_ms // _SILERO_CHUNK_MS
        self.min_speech_chunks = min_speech_ms // _SILERO_CHUNK_MS
        self.max_speech_chunks = int(
            max_speech_s * 1000 / _SILERO_CHUNK_MS
        )
        self.pre_speech_chunks = pre_speech_ms // _SILERO_CHUNK_MS
        self.chunk_size = sample_rate * _SILERO_CHUNK_MS // 1000
        self._lock = threading.Lock()
        self._ring_buffer: deque[np.ndarray] = deque(
            maxlen=self.pre_speech_chunks
        )
        self._is_speaking = False
        self._silence_count = 0
        self._speech_count = 0
        self._speech_frames: list[np.ndarray] = []
        self._buffer = np.array([], dtype=np.float32)
        self._model_loaded = False
        self._current_pre_roll_chunks = 0
        self._detected_utterances = 0
        self._rejected_utterances = 0
        log.info(
            "VAD settings: threshold=%.2f, silence=%dms effective, "
            "minimum_speech=%dms effective, pre_roll=%dms effective",
            self.threshold,
            self.silence_chunks * _SILERO_CHUNK_MS,
            self.min_speech_chunks * _SILERO_CHUNK_MS,
            self.pre_speech_chunks * _SILERO_CHUNK_MS,
        )

    def _ensure_model(self) -> None:
        if not self._model_loaded:
            _load_model()
            self._model_loaded = True

    def reset(self) -> None:
        """Reset transient detector state for a new capture session."""
        with self._lock:
            self._is_speaking = False
            self._silence_count = 0
            self._speech_count = 0
            self._speech_frames.clear()
            self._ring_buffer.clear()
            self._buffer = np.array([], dtype=np.float32)
            self._current_pre_roll_chunks = 0
            if _model is not None and hasattr(_model, "reset_states"):
                _model.reset_states()

    def process_chunk(
        self, audio: np.ndarray
    ) -> tuple[bool, np.ndarray | None]:
        """Consume microphone audio and return completed utterances."""
        self._ensure_model()
        with self._lock:
            return self._process_chunk_impl(audio)

    def _process_chunk_impl(
        self, audio: np.ndarray
    ) -> tuple[bool, np.ndarray | None]:
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        incoming = audio.reshape(-1)
        if self._buffer.size == 0:
            self._buffer = incoming.copy()
        elif incoming.size:
            self._buffer = np.concatenate([self._buffer, incoming])

        completed_utterance = None
        while len(self._buffer) >= self.chunk_size:
            chunk = self._buffer[: self.chunk_size]
            self._buffer = self._buffer[self.chunk_size :]
            probability = _model(chunk, self.sample_rate)

            if probability >= self.threshold:
                if not self._is_speaking:
                    self._is_speaking = True
                    self._silence_count = 0
                    self._speech_count = 0
                    self._current_pre_roll_chunks = len(self._ring_buffer)
                    self._speech_frames.extend(self._ring_buffer)
                    self._ring_buffer.clear()
                    log.debug("Speech started (probability=%.2f)", probability)
                self._speech_count += 1
                self._silence_count = 0
                self._speech_frames.append(chunk)
                if self._speech_count >= self.max_speech_chunks:
                    completed_utterance = np.concatenate(self._speech_frames)
                    self._record_completion(
                        completed_utterance, boundary="maximum"
                    )
                    self._reset_utterance()
                    break
            elif self._is_speaking:
                self._silence_count += 1
                self._speech_frames.append(chunk)
                if self._silence_count >= self.silence_chunks:
                    if self._speech_count >= self.min_speech_chunks:
                        completed_utterance = np.concatenate(self._speech_frames)
                        self._record_completion(
                            completed_utterance, boundary="silence"
                        )
                    else:
                        self._rejected_utterances += 1
                        log.info(
                            "VAD rejected short event: speech_chunks=%d, "
                            "rejected_total=%d",
                            self._speech_count,
                            self._rejected_utterances,
                        )
                    self._reset_utterance()
                    if completed_utterance is not None:
                        break
            else:
                self._ring_buffer.append(chunk)
        return completed_utterance is not None, completed_utterance

    def _record_completion(
        self, audio: np.ndarray, boundary: str
    ) -> None:
        self._detected_utterances += 1
        log.info(
            "VAD utterance: boundary=%s, duration=%.3fs, pre_roll=%dms, "
            "speech_chunks=%d, detected_total=%d, rejected_total=%d",
            boundary,
            len(audio) / self.sample_rate,
            self._current_pre_roll_chunks * _SILERO_CHUNK_MS,
            self._speech_count,
            self._detected_utterances,
            self._rejected_utterances,
        )

    def _reset_utterance(self) -> None:
        self._speech_frames.clear()
        self._is_speaking = False
        self._silence_count = 0
        self._speech_count = 0
        self._current_pre_roll_chunks = 0

    def flush(self) -> np.ndarray | None:
        """Return an in-progress utterance when capture stops."""
        with self._lock:
            if not self._is_speaking or not self._speech_frames:
                return None
            audio = np.concatenate(self._speech_frames)
            has_enough = self._speech_count >= self.min_speech_chunks
            if has_enough:
                self._record_completion(audio, boundary="flush")
            else:
                self._rejected_utterances += 1
                log.info(
                    "VAD rejected short flush: speech_chunks=%d, "
                    "rejected_total=%d",
                    self._speech_count,
                    self._rejected_utterances,
                )
            self._reset_utterance()
            self._ring_buffer.clear()
            return audio if has_enough else None

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

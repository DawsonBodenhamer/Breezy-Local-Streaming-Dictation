"""Unit tests for formatting commands and automatic inactivity expiration."""

from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# Ensure local src is at the front of sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np

from whisper_dictation.config import AudioConfig, Config, EngineConfig
from whisper_dictation.daemon import (
    DictationDaemon,
    apply_all_caps_commands,
    apply_formatting_commands,
)


class TestFormattingCommands(unittest.TestCase):
    """Test raw string transformation for all caps and caps on/off commands."""

    def test_all_caps_on_and_off_inline(self) -> None:
        text, active = apply_all_caps_commands(
            "all caps on hello world all caps off back to normal",
            active=False,
        )
        self.assertEqual(text, "HELLO WORLD back to normal")
        self.assertFalse(active)

    def test_all_caps_state_continuation(self) -> None:
        text1, active1 = apply_all_caps_commands("all caps on", active=False)
        self.assertEqual(text1, "")
        self.assertTrue(active1)

        text2, active2 = apply_all_caps_commands("capitalize this text", active=active1)
        self.assertEqual(text2, "CAPITALIZE THIS TEXT")
        self.assertTrue(active2)

        text3, active3 = apply_all_caps_commands("all caps off", active=active2)
        self.assertEqual(text3, "")
        self.assertFalse(active3)

    def test_caps_on_and_off_inline(self) -> None:
        text, caps_active, cap_next = apply_formatting_commands(
            "caps on hello world caps off back to normal",
            caps_active=False,
            cap_next=False,
        )
        self.assertEqual(text, "Hello World back to normal")
        self.assertFalse(caps_active)
        self.assertFalse(cap_next)

    def test_caps_state_continuation(self) -> None:
        text1, caps_active1, cap_next1 = apply_formatting_commands(
            "caps on",
            caps_active=False,
            cap_next=False,
        )
        self.assertEqual(text1, "")
        self.assertTrue(caps_active1)
        self.assertFalse(cap_next1)

        text2, caps_active2, cap_next2 = apply_formatting_commands(
            "my title phrase",
            caps_active=caps_active1,
            cap_next=cap_next1,
        )
        self.assertEqual(text2, "My Title Phrase")
        self.assertTrue(caps_active2)

        text3, caps_active3, cap_next3 = apply_formatting_commands(
            "caps off",
            caps_active=caps_active2,
            cap_next=cap_next2,
        )
        self.assertEqual(text3, "")
        self.assertFalse(caps_active3)

    def test_single_cap_command(self) -> None:
        text, caps_active, cap_next = apply_formatting_commands(
            "cap apple banana",
            caps_active=False,
            cap_next=False,
        )
        self.assertEqual(text, "Apple banana")
        self.assertFalse(caps_active)
        self.assertFalse(cap_next)


class TestFormattingTimeout(unittest.TestCase):
    """Test 2-second silence inactivity timeout in DictationDaemon."""

    def setUp(self) -> None:
        self.config = Config(
            audio=AudioConfig(sample_rate=16000),
            engine=EngineConfig(type="local"),
        )

    @patch("whisper_dictation.daemon.get_caret_context")
    @patch("whisper_dictation.daemon.type_text")
    @patch("whisper_dictation.daemon.create_engine")
    def test_all_caps_inactivity_timeout(
        self,
        mock_create_engine: MagicMock,
        mock_type_text: MagicMock,
        mock_get_caret_context: MagicMock,
    ) -> None:
        mock_caret = MagicMock()
        mock_caret.injection_allowed = True
        mock_caret.available = False
        mock_get_caret_context.return_value = mock_caret

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        daemon = DictationDaemon(self.config, streaming=True)
        # 16000 samples = 1.0 second of audio
        dummy_audio = np.zeros(16000, dtype=np.float32)

        # 1. Utterance 1: "all caps on" at end_time = 10.0 (audio 1.0s, start = 9.0)
        mock_engine.transcribe.return_value = "all caps on"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=10.0)
        self.assertTrue(daemon._all_caps_active)
        self.assertEqual(daemon._last_speech_end_time, 10.0)

        # 2. Utterance 2: "hello world" at end_time = 12.5 (audio 1.0s, start = 11.5)
        # Pause elapsed = 11.5 - 10.0 = 1.5s (< 2.0s threshold) -> stays active!
        mock_engine.transcribe.return_value = "hello world"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=12.5)
        self.assertTrue(daemon._all_caps_active)
        mock_type_text.assert_called_with("HELLO WORLD", expected_target=mock_caret.target)
        self.assertEqual(daemon._last_speech_end_time, 12.5)

        # 3. Utterance 3: "normal text" at end_time = 16.0 (audio 1.0s, start = 15.0)
        # Pause elapsed = 15.0 - 12.5 = 2.5s (>= 2.0s threshold) -> timeout expired!
        mock_engine.transcribe.return_value = "normal text"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=16.0)
        self.assertFalse(daemon._all_caps_active)
        mock_type_text.assert_called_with(" normal text", expected_target=mock_caret.target)

    @patch("whisper_dictation.daemon.get_caret_context")
    @patch("whisper_dictation.daemon.type_text")
    @patch("whisper_dictation.daemon.create_engine")
    def test_caps_on_inactivity_timeout(
        self,
        mock_create_engine: MagicMock,
        mock_type_text: MagicMock,
        mock_get_caret_context: MagicMock,
    ) -> None:
        mock_caret = MagicMock()
        mock_caret.injection_allowed = True
        mock_caret.available = False
        mock_get_caret_context.return_value = mock_caret

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        daemon = DictationDaemon(self.config, streaming=True)
        dummy_audio = np.zeros(16000, dtype=np.float32)

        # 1. Utterance 1: "caps on" at end_time = 10.0
        mock_engine.transcribe.return_value = "caps on"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=10.0)
        self.assertTrue(daemon._caps_active)

        # 2. Utterance 2: "title phrase" at end_time = 11.8 (audio 1.0s, start = 10.8)
        # Pause elapsed = 10.8 - 10.0 = 0.8s (< 2.0s threshold) -> stays active!
        mock_engine.transcribe.return_value = "title phrase"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=11.8)
        self.assertTrue(daemon._caps_active)
        mock_type_text.assert_called_with("Title Phrase", expected_target=mock_caret.target)

        # 3. Utterance 3: "normal text" at end_time = 15.0 (audio 1.0s, start = 14.0)
        # Pause elapsed = 14.0 - 11.8 = 2.2s (>= 2.0s threshold) -> timeout expired!
        mock_engine.transcribe.return_value = "normal text"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=15.0)
        self.assertFalse(daemon._caps_active)
        mock_type_text.assert_called_with(" normal text", expected_target=mock_caret.target)

    @patch("whisper_dictation.daemon.get_caret_context")
    @patch("whisper_dictation.daemon.type_text")
    @patch("whisper_dictation.daemon.create_engine")
    def test_cap_next_inactivity_timeout(
        self,
        mock_create_engine: MagicMock,
        mock_type_text: MagicMock,
        mock_get_caret_context: MagicMock,
    ) -> None:
        mock_caret = MagicMock()
        mock_caret.injection_allowed = True
        mock_caret.available = False
        mock_get_caret_context.return_value = mock_caret

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        daemon = DictationDaemon(self.config, streaming=True)
        dummy_audio = np.zeros(16000, dtype=np.float32)

        # 1. Utterance 1: "cap" at end_time = 10.0
        mock_engine.transcribe.return_value = "cap"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=10.0)
        self.assertTrue(daemon._cap_next)

        # 2. Utterance 2: "banana" at end_time = 14.0 (audio 1.0s, start = 13.0)
        # Pause elapsed = 13.0 - 10.0 = 3.0s (>= 2.0s threshold) -> timeout expired!
        mock_engine.transcribe.return_value = "banana"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=14.0)
        self.assertFalse(daemon._cap_next)
        mock_type_text.assert_called_with("banana", expected_target=mock_caret.target)

    @patch("whisper_dictation.daemon.get_caret_context")
    @patch("whisper_dictation.daemon.type_text")
    @patch("whisper_dictation.daemon.create_engine")
    def test_explicit_all_caps_off_before_timeout(
        self,
        mock_create_engine: MagicMock,
        mock_type_text: MagicMock,
        mock_get_caret_context: MagicMock,
    ) -> None:
        mock_caret = MagicMock()
        mock_caret.injection_allowed = True
        mock_caret.available = False
        mock_get_caret_context.return_value = mock_caret

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        daemon = DictationDaemon(self.config, streaming=True)
        dummy_audio = np.zeros(16000, dtype=np.float32)

        # 1. Utterance 1: "all caps on"
        mock_engine.transcribe.return_value = "all caps on"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=10.0)
        self.assertTrue(daemon._all_caps_active)

        # 2. Utterance 2: "all caps off" after 0.5s pause
        mock_engine.transcribe.return_value = "all caps off"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=11.5)
        self.assertFalse(daemon._all_caps_active)

        # 3. Utterance 3: "hello world" after 0.5s pause
        mock_engine.transcribe.return_value = "hello world"
        daemon._transcribe_and_type(dummy_audio, utterance_end_time=13.0)
        self.assertFalse(daemon._all_caps_active)
        mock_type_text.assert_called_with("hello world", expected_target=mock_caret.target)


if __name__ == "__main__":
    unittest.main()

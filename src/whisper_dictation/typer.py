"""Platform-aware text input — type text into the focused application."""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)
_typing_lock = threading.Lock()


def _type_windows(text: str) -> None:
    """Type Unicode text in one Win32 SendInput batch."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    input_keyboard = 1
    keyeventf_keyup = 0x0002
    keyeventf_unicode = 0x0004
    ulong_ptr = wintypes.WPARAM

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [
            ("mi", MouseInput),
            ("ki", KeyboardInput),
            ("hi", HardwareInput),
        ]

    class Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    utf16 = text.encode("utf-16-le", errors="surrogatepass")
    code_units = [
        int.from_bytes(utf16[index:index + 2], "little")
        for index in range(0, len(utf16), 2)
    ]
    inputs = (Input * (len(code_units) * 2))()
    for index, code_unit in enumerate(code_units):
        inputs[index * 2] = Input(
            type=input_keyboard,
            ki=KeyboardInput(0, code_unit, keyeventf_unicode, 0, 0),
        )
        inputs[index * 2 + 1] = Input(
            type=input_keyboard,
            ki=KeyboardInput(
                0,
                code_unit,
                keyeventf_unicode | keyeventf_keyup,
                0,
                0,
            ),
        )

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(Input))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


def type_text(text: str) -> None:
    """Type text into the focused application."""
    if not text:
        return
    log.debug("Typing %d chars", len(text))
    with _typing_lock:
        if sys.platform == "win32":
            _type_windows(text)
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")

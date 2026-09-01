"""Platform-aware text input — type text into the focused application."""

from __future__ import annotations

import logging
import sys
import threading
import time

from .caret_context import FocusedControlDiagnostic, get_focused_control_diagnostic

log = logging.getLogger(__name__)
_typing_lock = threading.Lock()


def _target_identity(target: FocusedControlDiagnostic) -> tuple[str, str] | None:
    executable = target.foreground_executable.strip().casefold()
    window_class = target.foreground_window_class.strip().casefold()
    if not executable or not window_class:
        return None
    return executable, window_class


def _is_modern_notepad_window(target: FocusedControlDiagnostic) -> bool:
    return (
        target.foreground_executable.casefold() == "notepad.exe"
        and target.foreground_window_class.casefold() == "notepad"
    )


def _is_exact_writable_notepad_control(
    target: FocusedControlDiagnostic,
) -> bool:
    return (
        _is_modern_notepad_window(target)
        and target.class_name.casefold() == "richeditd2dpt"
        and target.native_window_handle > 0
        and target.has_keyboard_focus is True
        and target.is_enabled is True
        and target.is_offscreen is False
        and target.read_only is False
    )


def _replace_windows_notepad_selection(
    text: str,
    target: FocusedControlDiagnostic,
) -> bool:
    """Replace the exact focused native Notepad selection with undo support."""
    import ctypes
    from ctypes import wintypes

    handle = target.native_window_handle
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = (wintypes.HWND,)
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    if not user32.IsWindow(handle) or not user32.IsWindowEnabled(handle):
        return False
    foreground = user32.GetForegroundWindow()
    if not foreground or user32.GetAncestor(handle, 2) != foreground:  # GA_ROOT
        return False
    class_name = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(handle, class_name, len(class_name)):
        return False
    if class_name.value.casefold() != "richeditd2dpt":
        return False

    native_text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    buffer = ctypes.create_unicode_buffer(native_text)
    result = ctypes.c_size_t()
    sent = user32.SendMessageTimeoutW(
        handle,
        0x00C2,  # EM_REPLACESEL
        1,  # replacement can be undone
        ctypes.cast(buffer, ctypes.c_void_p).value,
        0x0001 | 0x0002,  # SMTO_BLOCK | SMTO_ABORTIFHUNG
        2000,
        ctypes.byref(result),
    )
    return bool(sent)


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


def type_text(
    text: str,
    expected_target: FocusedControlDiagnostic | None = None,
) -> None:
    """Type text into the focused application."""
    if not text:
        return
    log.debug("Typing %d chars", len(text))
    with _typing_lock:
        if sys.platform == "win32":
            safety_started = time.monotonic()
            # The final safety snapshot never reads ancestor classes, so it
            # skips the parent walk that only the Photoshop layer-name
            # discriminator needs.
            current_target = get_focused_control_diagnostic(
                collect_ancestors=False
            )
            safety_ms = (time.monotonic() - safety_started) * 1000.0
            expected_notepad = (
                expected_target is not None
                and _is_modern_notepad_window(expected_target)
            )
            current_notepad = _is_modern_notepad_window(current_target)
            if expected_notepad:
                if (
                    not _is_exact_writable_notepad_control(current_target)
                    or current_target.native_window_handle
                    != expected_target.native_window_handle
                ):
                    raise RuntimeError(
                        "Focused Notepad control changed before text injection"
                    )
                dispatch_started = time.monotonic()
                if not _replace_windows_notepad_selection(text, current_target):
                    raise RuntimeError("Focused Notepad native replacement failed")
                log.info(
                    "Injection phases: safety_snapshot_ms=%.1f dispatch_ms=%.1f "
                    "path=notepad",
                    safety_ms,
                    (time.monotonic() - dispatch_started) * 1000.0,
                )
                return
            if current_notepad:
                if expected_target is not None:
                    raise RuntimeError(
                        "Focus changed to Notepad before text injection"
                    )
                if not _is_exact_writable_notepad_control(current_target):
                    raise RuntimeError(
                        "Focused Notepad native conditions are unavailable"
                    )
                dispatch_started = time.monotonic()
                if not _replace_windows_notepad_selection(text, current_target):
                    raise RuntimeError("Focused Notepad native replacement failed")
                log.info(
                    "Injection phases: safety_snapshot_ms=%.1f dispatch_ms=%.1f "
                    "path=notepad_unexpected",
                    safety_ms,
                    (time.monotonic() - dispatch_started) * 1000.0,
                )
                return
            expected_identity = (
                _target_identity(expected_target)
                if expected_target is not None
                else None
            )
            current_identity = _target_identity(current_target)
            if expected_target is not None and (
                expected_identity is None
                or current_identity is None
                or expected_identity != current_identity
            ):
                raise RuntimeError(
                    "Focused application identity is unavailable or changed "
                    "before text injection"
                )
            dispatch_started = time.monotonic()
            _type_windows(text)
            log.info(
                "Injection phases: safety_snapshot_ms=%.1f dispatch_ms=%.1f "
                "path=sendinput",
                safety_ms,
                (time.monotonic() - dispatch_started) * 1000.0,
            )
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")

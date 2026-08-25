"""Read-only focused-text context for conservative phrase capitalization."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

log = logging.getLogger(__name__)

_EDITOR_MARKERS = str.maketrans("", "", "\u200b\u200c\u200d\ufeff\ufffc")
_UNAVAILABLE_CONTEXT_TARGET_ALLOWLIST = frozenset(
    {
        ("idea64.exe", "sunawtframe"),
        ("antigravity ide.exe", "chrome_widgetwin_1"),
    }
)


@dataclass(frozen=True)
class FocusedControlDiagnostic:
    """Metadata-only snapshot of the focused UI Automation control."""

    foreground_executable: str = ""
    foreground_window_class: str = ""
    control_type: str = ""
    control_type_id: int | None = None
    class_name: str = ""
    automation_id: str = ""
    has_keyboard_focus: bool | None = None
    is_keyboard_focusable: bool | None = None
    is_enabled: bool | None = None
    is_offscreen: bool | None = None
    read_only: bool | None = None
    native_window_handle: int = 0
    text_pattern: bool = False
    value_pattern: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe metadata without reading control content."""
        return {
            "foreground_executable": self.foreground_executable,
            "foreground_window_class": self.foreground_window_class,
            "control_type": self.control_type,
            "control_type_id": self.control_type_id,
            "class_name": self.class_name,
            "automation_id": self.automation_id,
            "has_keyboard_focus": self.has_keyboard_focus,
            "is_keyboard_focusable": self.is_keyboard_focusable,
            "is_enabled": self.is_enabled,
            "is_offscreen": self.is_offscreen,
            "read_only": self.read_only,
            "native_window_handle": self.native_window_handle,
            "text_pattern": self.text_pattern,
            "value_pattern": self.value_pattern,
            "error": self.error,
        }


def _has_visible_text(text: str) -> bool:
    """Ignore whitespace and invisible markers exposed by rich text editors."""
    return bool(text.translate(_EDITOR_MARKERS).strip())


def _is_placeholder_context(
    document_text: str,
    line_text: str,
    selection_text: str,
    before_char: str,
    after_char: str,
) -> bool:
    """Recognize a rich editor exposing placeholder text as its document."""
    return (
        not selection_text
        and bool(document_text)
        and document_text[0] in ("\r", "\n")
        and not line_text.strip()
        and after_char in ("\r", "\n")
    )


@dataclass(frozen=True)
class CaretContext:
    """Read-only text immediately surrounding the focused selection/caret."""

    available: bool = False
    injection_allowed: bool = False
    document_text: str = ""
    line_text: str = ""
    selection_text: str = ""
    before_char: str = ""
    after_char: str = ""

    @property
    def has_selection(self) -> bool:
        return bool(self.selection_text)

    @property
    def is_empty_document(self) -> bool:
        return (
            not _has_visible_text(self.document_text)
            or _is_placeholder_context(
                self.document_text,
                self.line_text,
                self.selection_text,
                self.before_char,
                self.after_char,
            )
        )

    @property
    def should_capitalize(self) -> bool:
        return (
            self.available
            and is_empty_casing_context(
                self.document_text,
                self.line_text,
                self.selection_text,
                self.before_char,
                self.after_char,
            )
        )


def is_empty_casing_context(
    document_text: str,
    line_text: str,
    selection_text: str,
    before_char: str = "",
    after_char: str = "",
) -> bool:
    """Return true only for an empty document or an empty unselected line."""
    if selection_text:
        return False
    if (
        not _has_visible_text(document_text)
        or _is_placeholder_context(
            document_text,
            line_text,
            selection_text,
            before_char,
            after_char,
        )
    ):
        return True
    return (
        not line_text.strip()
        and before_char in ("", "\r", "\n")
        and after_char in ("", "\r", "\n")
    )


def _foreground_target_identity() -> tuple[str, str]:
    """Return foreground executable and top-level window class on Windows."""
    if sys.platform != "win32":
        return "", ""

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    window = user32.GetForegroundWindow()
    if not window:
        return "", ""
    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(window, ctypes.byref(process_id)):
        return "", ""
    window_class = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(
        window,
        window_class,
        len(window_class),
    ):
        return "", ""

    process = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id.value,
    )
    if not process:
        return "", ""
    try:
        capacity = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            process,
            0,
            path,
            ctypes.byref(capacity),
        ):
            return "", ""
        return (
            PureWindowsPath(path.value).name.casefold(),
            window_class.value.casefold(),
        )
    finally:
        kernel32.CloseHandle(process)


def _get_pattern(control: Any, pattern_id: int) -> Any | None:
    try:
        return control.GetPattern(pattern_id)
    except Exception:
        return None


def _control_type_name(auto: Any, control_type: int | None) -> str:
    if control_type is None:
        return ""
    return getattr(auto, "ControlTypeNames", {}).get(
        control_type,
        f"Unknown({control_type})",
    )


def _focused_control_diagnostic(
    control: Any,
    auto: Any,
    executable_name: str,
    window_class: str,
    text_pattern: Any | None = None,
    value_pattern: Any | None = None,
) -> FocusedControlDiagnostic:
    control_type = None
    try:
        control_type = int(control.ControlType)
    except Exception:
        pass

    read_only: bool | None = None
    if value_pattern is not None:
        try:
            read_only = bool(value_pattern.IsReadOnly)
        except Exception:
            pass
    if read_only is None and text_pattern is not None:
        try:
            selections = text_pattern.GetSelection()
            if len(selections) == 1:
                read_only = bool(
                    selections[0].GetAttributeValue(
                        auto.TextAttributeId.IsReadOnlyAttribute,
                    )
                )
        except Exception:
            pass

    def control_bool(name: str) -> bool | None:
        try:
            return bool(getattr(control, name))
        except Exception:
            return None

    def control_text(name: str) -> str:
        try:
            return str(getattr(control, name) or "")
        except Exception:
            return ""

    try:
        native_window_handle = int(control.NativeWindowHandle or 0)
    except Exception:
        native_window_handle = 0

    return FocusedControlDiagnostic(
        foreground_executable=executable_name,
        foreground_window_class=window_class,
        control_type=_control_type_name(auto, control_type),
        control_type_id=control_type,
        class_name=control_text("ClassName"),
        automation_id=control_text("AutomationId"),
        has_keyboard_focus=control_bool("HasKeyboardFocus"),
        is_keyboard_focusable=control_bool("IsKeyboardFocusable"),
        is_enabled=control_bool("IsEnabled"),
        is_offscreen=control_bool("IsOffscreen"),
        read_only=read_only,
        native_window_handle=native_window_handle,
        text_pattern=text_pattern is not None,
        value_pattern=value_pattern is not None,
    )


def get_focused_control_diagnostic() -> FocusedControlDiagnostic:
    """Return focused-control metadata without reading text or changing state."""
    executable_name, window_class = _foreground_target_identity()
    try:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            control = auto.GetFocusedControl()
            if control is None:
                return FocusedControlDiagnostic(
                    foreground_executable=executable_name,
                    foreground_window_class=window_class,
                    error="no_focused_control",
                )
            text_pattern = _get_pattern(control, auto.PatternId.TextPattern)
            value_pattern = _get_pattern(control, auto.PatternId.ValuePattern)
            return _focused_control_diagnostic(
                control,
                auto,
                executable_name,
                window_class,
                text_pattern,
                value_pattern,
            )
    except Exception as exc:
        log.debug("Focused-control diagnostic failed", exc_info=True)
        return FocusedControlDiagnostic(
            foreground_executable=executable_name,
            foreground_window_class=window_class,
            error=type(exc).__name__,
        )


def _is_focused_writable_edit_control(control: Any, auto: Any) -> bool:
    try:
        if not control.HasKeyboardFocus or not control.IsEnabled:
            return False
        if control.IsOffscreen:
            return False
        return control.ControlType in (
            auto.ControlType.EditControl,
            auto.ControlType.DocumentControl,
        )
    except Exception:
        return False


def _utf16_index(value: str, offset: int) -> int:
    """Convert a Win32 UTF-16 selection offset to a Python string index."""
    encoded = value.encode("utf-16-le")
    byte_offset = max(0, min(len(encoded), int(offset) * 2))
    byte_offset -= byte_offset % 2
    return len(encoded[:byte_offset].decode("utf-16-le", errors="ignore"))


def _read_native_edit_context(
    handle: int,
    fallback_value: str,
) -> tuple[str, int, int] | None:
    """Read a native edit's value and selection using non-mutating Win32 calls."""
    if sys.platform != "win32" or not handle:
        return None

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SendMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    # ctypes.wintypes does not expose LRESULT on this CPython build;
    # LRESULT is a pointer-sized signed integer on Win32/Win64.
    user32.SendMessageW.restype = ctypes.c_ssize_t

    length = max(0, int(user32.GetWindowTextLengthW(handle)))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, len(buffer))
    value = buffer.value or fallback_value

    selection = (wintypes.DWORD * 2)()
    user32.SendMessageW(
        handle,
        0x00B0,  # EM_GETSEL
        0,
        ctypes.addressof(selection),
    )
    start = _utf16_index(value, selection[0])
    end = _utf16_index(value, selection[1])
    if end < start:
        start, end = end, start
    return value, start, end


def _value_pattern_context(control: Any, value_pattern: Any) -> CaretContext:
    try:
        if value_pattern.IsReadOnly:
            return CaretContext()
        value = str(value_pattern.Value or "")
        handle = int(control.NativeWindowHandle or 0)
    except Exception:
        return CaretContext()

    native = _read_native_edit_context(handle, value)
    if native is None:
        # ValuePattern proves writable focus, but without a native selection
        # anchor we deliberately avoid fabricating caret or replacement state.
        return CaretContext(injection_allowed=True)

    value, start, end = native
    return CaretContext(
        available=True,
        injection_allowed=True,
        document_text=value,
        line_text=value,
        selection_text=value[start:end],
        before_char=value[start - 1 : start],
        after_char=value[end : end + 1],
    )


def is_allowlisted_unavailable_context_target(
    executable_name: str | None = None,
    window_class: str | None = None,
) -> bool:
    """Allow injection only into exact unsupported editor window identities."""
    if executable_name is None and window_class is None:
        executable_name, window_class = _foreground_target_identity()
    elif executable_name is None or window_class is None:
        return False
    return (
        PureWindowsPath(executable_name).name.casefold(),
        window_class.casefold(),
    ) in _UNAVAILABLE_CONTEXT_TARGET_ALLOWLIST


def get_caret_context() -> CaretContext:
    """Inspect focused UIA text without moving the caret or selection.

    Unsupported controls and probe failures return an unavailable context so the
    caller can retain its established conservative fallback.
    """
    try:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            control = auto.GetFocusedControl()
            if control is None or not _is_focused_writable_edit_control(control, auto):
                executable_name, window_class = _foreground_target_identity()
                return CaretContext(
                    injection_allowed=is_allowlisted_unavailable_context_target(
                        executable_name,
                        window_class,
                    )
                )

            pattern = _get_pattern(control, auto.PatternId.TextPattern)
            value_pattern = _get_pattern(control, auto.PatternId.ValuePattern)
            if pattern is None and value_pattern is not None:
                return _value_pattern_context(control, value_pattern)
            if pattern is None:
                executable_name, window_class = _foreground_target_identity()
                return CaretContext(
                    injection_allowed=is_allowlisted_unavailable_context_target(
                        executable_name,
                        window_class,
                    )
                )

            selections = pattern.GetSelection()
            if len(selections) != 1:
                return CaretContext()
            selection = selections[0]
            selection_text = selection.GetText(-1)
            if bool(
                selection.GetAttributeValue(
                    auto.TextAttributeId.IsReadOnlyAttribute,
                )
            ):
                return CaretContext()

            document_text = pattern.DocumentRange.GetText(-1)
            line = selection.Clone()
            line.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
            line_text = line.GetText(-1)

            before = selection.Clone()
            before.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                selection,
                auto.TextPatternRangeEndpoint.Start,
                waitTime=0,
            )
            before.MoveEndpointByUnit(
                auto.TextPatternRangeEndpoint.Start,
                auto.TextUnit.Character,
                -1,
                waitTime=0,
            )
            before_text = before.GetText(-1)

            after = selection.Clone()
            after.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.Start,
                selection,
                auto.TextPatternRangeEndpoint.End,
                waitTime=0,
            )
            after.MoveEndpointByUnit(
                auto.TextPatternRangeEndpoint.End,
                auto.TextUnit.Character,
                1,
                waitTime=0,
            )
            after_text = after.GetText(-1)

            return CaretContext(
                available=True,
                injection_allowed=True,
                document_text=document_text,
                line_text=line_text,
                selection_text=selection_text,
                before_char=before_text[-1:] if before_text else "",
                after_char=after_text[:1] if after_text else "",
            )
    except Exception:
        executable_name, window_class = _foreground_target_identity()
        injection_allowed = is_allowlisted_unavailable_context_target(
            executable_name,
            window_class,
        )
        log.debug("Focused text context unavailable", exc_info=True)
        return CaretContext(injection_allowed=injection_allowed)


def should_capitalize_at_caret() -> bool:
    """Return conservative contextual capitalization for compatibility."""
    context = get_caret_context()
    return context.available and context.should_capitalize


def has_selected_text() -> bool:
    """Return true only when the focused UIA text control has selected text."""
    return get_caret_context().has_selection

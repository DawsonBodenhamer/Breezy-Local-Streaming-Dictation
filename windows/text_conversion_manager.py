"""On-demand native manager for personal text conversions."""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent
ISOLATED_SITE_PACKAGES = RUNTIME_ROOT / ".venv" / "Lib" / "site-packages"
if ISOLATED_SITE_PACKAGES.is_dir():
    sys.path.insert(0, str(ISOLATED_SITE_PACKAGES))

from whisper_dictation.conversions import (
    ANYWHERE,
    SEPARATE_WORDS,
    ConversionRule,
    ConversionStore,
    ConversionValidationError,
    new_rule,
)

WINDOW_TITLE = "Manage text conversions"
SEPARATE_WORDS_LABEL = "As separate words"
ANYWHERE_LABEL = "Anywhere, including inside words"
ERROR_ALREADY_EXISTS = 183


def _match_location_label(value: str) -> str:
    return (
        SEPARATE_WORDS_LABEL
        if value == SEPARATE_WORDS
        else ANYWHERE_LABEL
    )


def _acquire_singleton() -> tuple[object | None, bool]:
    """Acquire a per-user Windows mutex; return (handle, acquired)."""
    if os.name != "nt":
        return None, True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, True, "Local\\BreezyLocalStreamingDictationTextConversions")
    if not handle:
        return None, True
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None, False
    return handle, True


def _release_singleton(handle: object | None) -> None:
    if handle is not None and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(handle)


class ConversionManager:
    def __init__(self, root: tk.Tk, store: ConversionStore) -> None:
        self.root = root
        self.store = store
        self.rules = list(store.rules())
        self._status = tk.StringVar()
        self._build_window()
        self._refresh()

    def _build_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.geometry("900x570")
        self.root.minsize(720, 440)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        intro = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        intro.grid(row=0, column=0, sticky="ew")
        intro.columnconfigure(0, weight=1)
        ttk.Label(
            intro,
            text="Text conversions",
            font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            intro,
            text="Change literal phrases after transcription. Updates apply to the next completed transcription.",
            wraplength=820,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        content = ttk.Frame(self.root, padding=(18, 8, 18, 8))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        ttk.Label(
            content,
            text="When dictation hears / Replace with",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        table_frame = ttk.Frame(content)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("source", "replacement", "location", "case"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "source": "When dictation hears",
            "replacement": "Replace with",
            "location": "Where it matches",
            "case": "Capitalization",
        }
        widths = {"source": 200, "replacement": 200, "location": 250, "case": 150}
        for column in self.tree["columns"]:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._edit_selected)
        self.tree.bind("<Return>", self._edit_selected)
        self.tree.bind("<Delete>", self._delete_selected)

        self.empty_label = ttk.Label(
            table_frame,
            text="No conversions yet. Add one when you are ready; the shipped list starts empty.",
            justify="center",
            anchor="center",
        )
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

        status = ttk.Label(content, textvariable=self._status, wraplength=820)
        status.grid(row=2, column=0, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self.root, padding=(18, 8, 18, 16))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Add", command=self._add).grid(row=0, column=1, padx=(0, 6))
        self.edit_button = ttk.Button(actions, text="Edit", command=self._edit_selected)
        self.edit_button.grid(row=0, column=2, padx=(0, 6))
        self.delete_button = ttk.Button(actions, text="Delete", command=self._delete_selected)
        self.delete_button.grid(row=0, column=3, padx=(0, 6))
        ttk.Button(actions, text="Close", command=self.root.destroy).grid(row=0, column=4)

        self.root.bind("<Control-n>", lambda _event: self._add())
        self.root.bind("<Escape>", lambda _event: self.root.destroy())

    def _selected_rule(self) -> ConversionRule | None:
        selection = self.tree.selection()
        if not selection:
            return None
        identifier = selection[0]
        return next((rule for rule in self.rules if rule.identifier == identifier), None)

    def _refresh(self, selected: str | None = None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for rule in sorted(self.rules, key=lambda item: item.order):
            self.tree.insert(
                "",
                "end",
                iid=rule.identifier,
                values=(
                    rule.source,
                    rule.replacement,
                    _match_location_label(rule.match_location),
                    "Exact capitalization" if rule.case_sensitive else "Any capitalization",
                ),
            )
        empty = not self.rules
        if empty:
            self.empty_label.lift()
        else:
            self.empty_label.lower()
        state = "normal" if self.rules else "disabled"
        self.edit_button.configure(state=state)
        self.delete_button.configure(state=state)
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.focus(selected)
        elif self.rules:
            first = sorted(self.rules, key=lambda item: item.order)[0].identifier
            self.tree.selection_set(first)
            self.tree.focus(first)
        if self.store.last_error:
            self._status.set(self.store.last_error)
        elif empty:
            self._status.set("No conversions saved.")
        else:
            self._status.set("Changes apply to the next completed transcription.")

    def _add(self) -> None:
        self._open_editor(None)

    def _edit_selected(self, _event: object | None = None) -> str | None:
        rule = self._selected_rule()
        if rule is not None:
            self._open_editor(rule)
        return "break" if _event is not None else None

    def _delete_selected(self, _event: object | None = None) -> str | None:
        rule = self._selected_rule()
        if rule is None:
            return "break" if _event is not None else None
        if not messagebox.askyesno(
            "Delete conversion",
            f'Delete the conversion "{rule.source}" → "{rule.replacement}"?',
            parent=self.root,
        ):
            return "break" if _event is not None else None
        try:
            self.rules = list(self.store.save(rule_item for rule_item in self.rules if rule_item.identifier != rule.identifier))
        except ConversionValidationError as error:
            self._status.set(error.errors[0])
        else:
            self._refresh()
        return "break" if _event is not None else None

    def _open_editor(self, existing: ConversionRule | None) -> None:
        editor = tk.Toplevel(self.root)
        editor.title("Edit text conversion" if existing else "Add text conversion")
        editor.geometry("720x500")
        editor.minsize(620, 430)
        editor.transient(self.root)
        editor.grab_set()
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(3, weight=1)

        form = ttk.Frame(editor, padding=18)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="When dictation hears").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        source_var = tk.StringVar(value=existing.source if existing else "")
        source_entry = ttk.Entry(form, textvariable=source_var)
        source_entry.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(form, text="Replace with").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        replacement_var = tk.StringVar(value=existing.replacement if existing else "")
        replacement_entry = ttk.Entry(form, textvariable=replacement_var)
        replacement_entry.grid(row=1, column=1, sticky="ew", pady=5)

        match_frame = ttk.LabelFrame(editor, text="Where should it match?", padding=12)
        match_frame.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        match_frame.columnconfigure(0, weight=1)
        match_var = tk.StringVar(value=existing.match_location if existing else SEPARATE_WORDS)
        ttk.Radiobutton(
            match_frame,
            text=SEPARATE_WORDS_LABEL,
            variable=match_var,
            value=SEPARATE_WORDS,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            match_frame,
            text=ANYWHERE_LABEL,
            variable=match_var,
            value=ANYWHERE,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        explanation = ttk.Label(match_frame, wraplength=650, justify="left")
        explanation.grid(row=2, column=0, sticky="w", pady=(8, 0))

        case_var = tk.BooleanVar(value=existing.case_sensitive if existing else False)
        ttk.Checkbutton(
            editor,
            text="Match capitalization exactly",
            variable=case_var,
        ).grid(row=2, column=0, sticky="w", padx=18, pady=(0, 8))

        preview_frame = ttk.LabelFrame(editor, text="Preview", padding=12)
        preview_frame.grid(row=3, column=0, sticky="nsew", padx=18, pady=(0, 10))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview = ttk.Label(preview_frame, justify="left", anchor="nw", wraplength=650)
        preview.grid(row=0, column=0, sticky="nsew")

        error_var = tk.StringVar()
        error_label = ttk.Label(editor, textvariable=error_var, foreground="#b42318", wraplength=650)
        error_label.grid(row=4, column=0, sticky="w", padx=18, pady=(0, 8))

        actions = ttk.Frame(editor, padding=(18, 0, 18, 16))
        actions.grid(row=5, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Cancel", command=editor.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(
            actions,
            text="Save",
            command=lambda: self._save_editor(
                editor,
                existing,
                source_var,
                replacement_var,
                match_var,
                case_var,
                error_var,
            ),
        ).grid(row=0, column=2)

        def update_preview(*_args: object) -> None:
            source = source_var.get().strip()
            replacement = replacement_var.get().strip() or "(replacement text)"
            if not source:
                preview.configure(text="Enter a phrase to see an example.")
            elif match_var.get() == SEPARATE_WORDS:
                preview.configure(
                    text=(
                        f'Example: “a {source} here” becomes “a {replacement} here”.\n'
                        "The phrase can sit beside punctuation, but it does not match inside a longer word."
                    )
                )
            else:
                preview.configure(
                    text=(
                        f'Example: “a {source} here” becomes “a {replacement} here”.\n'
                        "The matching characters can also be replaced inside a longer word."
                    )
                )
            explanation.configure(
                text=(
                    "Matches the phrase as its own words, so a longer word is left unchanged."
                    if match_var.get() == SEPARATE_WORDS
                    else "Matches the exact characters anywhere, including inside a longer word."
                )
            )

        source_var.trace_add("write", update_preview)
        replacement_var.trace_add("write", update_preview)
        match_var.trace_add("write", update_preview)
        update_preview()
        source_entry.focus_set()
        editor.bind("<Escape>", lambda _event: editor.destroy())

    def _save_editor(
        self,
        editor: tk.Toplevel,
        existing: ConversionRule | None,
        source_var: tk.StringVar,
        replacement_var: tk.StringVar,
        match_var: tk.StringVar,
        case_var: tk.BooleanVar,
        error_var: tk.StringVar,
    ) -> None:
        identifier = existing.identifier if existing else None
        order = existing.order if existing else max((rule.order for rule in self.rules), default=-1) + 1
        try:
            candidate = new_rule(
                source_var.get().strip(),
                replacement_var.get().strip(),
                match_location=match_var.get(),
                case_sensitive=case_var.get(),
                order=order,
                identifier=identifier,
            )
            others = [rule for rule in self.rules if rule.identifier != identifier]
            self.rules = list(self.store.save((*others, candidate)))
        except ConversionValidationError as error:
            error_var.set(error.field_errors.get("source") or error.field_errors.get("replacement") or error.errors[0])
            return
        except OSError:
            error_var.set("The conversion file could not be saved. Check that the local runtime folder is writable.")
            return
        editor.destroy()
        self._refresh(selected=candidate.identifier)


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    mutex, acquired = _acquire_singleton()
    if not acquired:
        messagebox.showinfo(
            WINDOW_TITLE,
            "The text conversion manager is already open.",
            parent=root,
        )
        root.destroy()
        return
    try:
        root.deiconify()
        ConversionManager(root, ConversionStore())
        root.mainloop()
    finally:
        _release_singleton(mutex)


if __name__ == "__main__":
    main()

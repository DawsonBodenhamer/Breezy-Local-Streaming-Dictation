"""On-demand native manager for dictation corrections."""

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
    new_correction,
    organize_suggested_groups,
    suggest_compatible_groups,
)

WINDOW_TITLE = "Dictation corrections"
SEPARATE_WORDS_LABEL = "As separate words"
ANYWHERE_LABEL = "Anywhere, including inside words"
ERROR_ALREADY_EXISTS = 183


def suggestion_phrase_rows(rules: list[ConversionRule] | tuple[ConversionRule, ...]) -> tuple[str, ...]:
    """Return one complete visible row per phrase in a suggested group."""
    return tuple(rule.source for rule in rules)


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
            text="Dictation corrections",
            font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            intro,
            text=("Fix words or phrases Breezy often hears incorrectly. Add everything "
                  "Breezy might hear, then choose exactly what it should type."),
            wraplength=820,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        content = ttk.Frame(self.root, padding=(18, 8, 18, 8))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        ttk.Label(
            content,
            text="Breezy may hear / Breezy should type / Matching",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        table_frame = ttk.Frame(content)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("source", "replacement", "matching"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "source": "Breezy may hear",
            "replacement": "Breezy should type",
            "matching": "Matching",
        }
        widths = {"source": 280, "replacement": 220, "matching": 250}
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
            text="No corrections yet. Add one when Breezy hears something incorrectly.",
            justify="center",
            anchor="center",
        )
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

        status = ttk.Label(content, textvariable=self._status, wraplength=820)
        status.grid(row=2, column=0, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self.root, padding=(18, 8, 18, 16))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.review_button = ttk.Button(actions, text="Review suggested groups", command=self._review_suggested_groups)
        self.review_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(actions, text="Add correction", command=self._add).grid(row=0, column=2, padx=(0, 6))
        self.edit_button = ttk.Button(actions, text="Edit", command=self._edit_selected)
        self.edit_button.grid(row=0, column=3, padx=(0, 6))
        self.delete_button = ttk.Button(actions, text="Delete", command=self._delete_selected)
        self.delete_button.grid(row=0, column=4, padx=(0, 6))
        ttk.Button(actions, text="Close", command=self.root.destroy).grid(row=0, column=5)

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
                    rule.source if len(rule.sources) == 1 else f"{rule.source} (+{len(rule.sources) - 1} more)",
                    rule.replacement,
                    f"{_match_location_label(rule.match_location)}; "
                    f"{'exact' if rule.case_sensitive else 'any'} capitalization",
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
        self.review_button.configure(
            state="normal" if suggest_compatible_groups(self.rules) else "disabled"
        )
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
            self._status.set("No corrections saved.")
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
            "Delete correction",
            f'Delete the correction "{rule.source}" → "{rule.replacement}"?',
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
        editor.title("Edit correction" if existing else "Add correction")
        editor.geometry("720x500")
        editor.minsize(620, 430)
        editor.transient(self.root)
        editor.grab_set()
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(3, weight=1)

        form = ttk.Frame(editor, padding=18)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Breezy may hear (one phrase per line)").grid(row=0, column=0, sticky="nw", padx=(0, 12), pady=5)
        source_entry = tk.Text(form, height=5, width=50)
        source_entry.insert("1.0", "\n".join(existing.sources) if existing else "")
        source_entry.grid(row=0, column=1, sticky="ew", pady=5)
        phrase_actions = ttk.Frame(form)
        phrase_actions.grid(row=1, column=1, sticky="w")
        ttk.Button(phrase_actions, text="Add another phrase", command=lambda: source_entry.insert("end", "\n")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(phrase_actions, text="Remove selected phrase", command=lambda: source_entry.delete("insert linestart", "insert lineend+1c")).grid(row=0, column=1)
        ttk.Label(form, text="Breezy should type this").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=5)
        replacement_var = tk.StringVar(value=existing.replacement if existing else "")
        replacement_entry = ttk.Entry(form, textvariable=replacement_var)
        replacement_entry.grid(row=2, column=1, sticky="ew", pady=5)

        match_frame = ttk.LabelFrame(editor, text="Matching options", padding=12)
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
                source_entry,
                replacement_var,
                match_var,
                case_var,
                error_var,
            ),
        ).grid(row=0, column=2)

        def update_preview(*_args: object) -> None:
            source = source_entry.get("1.0", "end").splitlines()[0].strip() if source_entry.get("1.0", "end").strip() else ""
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

        replacement_var.trace_add("write", update_preview)
        match_var.trace_add("write", update_preview)
        source_entry.bind("<KeyRelease>", update_preview)
        update_preview()
        source_entry.focus_set()
        editor.bind("<Escape>", lambda _event: editor.destroy())

    def _save_editor(
        self,
        editor: tk.Toplevel,
        existing: ConversionRule | None,
        source_entry: tk.Text,
        replacement_var: tk.StringVar,
        match_var: tk.StringVar,
        case_var: tk.BooleanVar,
        error_var: tk.StringVar,
    ) -> None:
        identifier = existing.identifier if existing else None
        order = existing.order if existing else max((rule.order for rule in self.rules), default=-1) + 1
        try:
            sources = tuple(line.strip() for line in source_entry.get("1.0", "end").splitlines() if line.strip())
            candidate = new_correction(
                sources,
                replacement_var.get().strip(),
                match_location=match_var.get(),
                case_sensitive=case_var.get(),
                order=order,
                identifier=identifier,
            )
            others = [rule for rule in self.rules if rule.identifier != identifier]
            self.rules = list(self.store.save((*others, candidate)))
        except ConversionValidationError as error:
            error_var.set(error.field_errors.get("sources") or error.field_errors.get("replacement") or error.errors[0])
            return
        except OSError:
            error_var.set("The corrections file could not be saved. Check that the local runtime folder is writable.")
            return
        editor.destroy()
        self._refresh(selected=candidate.identifier)

    def _review_suggested_groups(self) -> None:
        suggestions = suggest_compatible_groups(self.rules)
        if not suggestions:
            return
        by_id = {rule.identifier: rule for rule in self.rules}
        selected = []
        for identifiers in suggestions:
            rules = [by_id[identifier] for identifier in identifiers]
            if self._confirm_suggested_group(rules):
                selected.append(identifiers)
        if not selected:
            return
        try:
            organized = organize_suggested_groups(self.rules, selected)
            self.rules = list(self.store.save(organized))
        except (ConversionValidationError, OSError) as error:
            self._status.set(
                error.errors[0]
                if isinstance(error, ConversionValidationError)
                else "The corrections file could not be saved. No groups were changed."
            )
            return
        self._refresh()

    def _confirm_suggested_group(self, rules: list[ConversionRule]) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title("Review suggested group")
        dialog.geometry("620x420")
        dialog.minsize(520, 360)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)
        decision = tk.BooleanVar(value=False)

        heading = ttk.Frame(dialog, padding=(20, 18, 20, 10))
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(
            heading,
            text="Group these phrases?",
            font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            heading,
            text="Each row below is one complete phrase, even when it contains punctuation.",
            wraplength=560,
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))

        result = ttk.Frame(dialog, padding=(20, 4, 20, 10))
        result.grid(row=1, column=0, sticky="ew")
        result.columnconfigure(0, weight=1)
        ttk.Label(result, text="Breezy should type", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            result,
            text=rules[0].replacement,
            font=("Segoe UI", 11),
            wraplength=560,
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        phrases_frame = ttk.LabelFrame(dialog, text="Breezy may hear", padding=10)
        phrases_frame.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 12))
        phrases_frame.columnconfigure(0, weight=1)
        phrases_frame.rowconfigure(0, weight=1)
        phrase_list = tk.Listbox(
            phrases_frame,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 10),
            selectmode="none",
        )
        for phrase in suggestion_phrase_rows(rules):
            phrase_list.insert("end", phrase)
        phrase_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(phrases_frame, orient="vertical", command=phrase_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        phrase_list.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(dialog, padding=(20, 0, 20, 18))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        def close(group: bool) -> None:
            decision.set(group)
            dialog.destroy()

        keep_button = ttk.Button(
            actions,
            text="Keep separate",
            command=lambda: close(False),
        )
        keep_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            actions,
            text="Group phrases",
            command=lambda: close(True),
        ).grid(row=0, column=2)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        dialog.bind("<Escape>", lambda _event: close(False))
        dialog.bind("<Control-Return>", lambda _event: close(True))
        keep_button.focus_set()
        self.root.wait_window(dialog)
        return decision.get()


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

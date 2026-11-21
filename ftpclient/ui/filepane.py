"""Reusable two-pane file browser widget."""

from __future__ import annotations

import os
import posixpath
import stat as stat_module
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

from ..transfer import format_bytes


@dataclass
class LocalEntry:
    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mtime: float | None = None
    mode: int | None = None
    is_link: bool = False

    @property
    def mode_text(self) -> str:
        if self.mode is None:
            return ""
        return stat_module.filemode(self.mode)[1:10]


@dataclass
class PaneCallbacks:
    navigate: Callable[[str], None]
    refresh: Callable[[], None]
    activate: Callable[[object], None] | None = None
    new_folder: Callable[[], None] | None = None
    rename: Callable[[object], None] | None = None
    delete: Callable[[list], None] | None = None
    transfer: Callable[[], None] | None = None
    chmod: Callable[[object], None] | None = None
    open_external: Callable[[object], None] | None = None
    hidden_changed: Callable[[bool], None] | None = None


class FilePane(ttk.Frame):
    """One side of the dual-pane browser.

    The pane is a pure view: it knows how to list a directory, sort and present
    entries, but every filesystem or network action is delegated to callbacks.
    """

    def __init__(
        self,
        master,
        *,
        side: str,
        title: str,
        callbacks: PaneCallbacks,
        palette: dict,
        transfer_label: str,
        path_style: str = "local",
        show_hidden: bool = False,
    ) -> None:
        super().__init__(master, style="Card.TFrame", padding=(0, 0, 0, 0))
        self.side = side
        self.title = title
        self.callbacks = callbacks
        self.palette = palette
        self.path_style = path_style
        self.transfer_label = transfer_label

        self._items: list = []
        self._rows: dict[str, object] = {}
        self._sort_column = "name"
        self._sort_reverse = False
        self._enabled = True
        self._transfer_ready = False
        self._busy = False
        self._placeholder = ""
        self.current_path = "/"

        self._build(show_hidden)

    # -- construction -----------------------------------------------------
    def _build(self, show_hidden: bool) -> None:
        family = self.palette["family"]

        header = ttk.Frame(self)
        header.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(header, text=self.title.upper(), style="Title.TLabel").pack(side="left")
        ttk.Label(
            header, text=f"  {self.side}", style="Badge.TLabel"
        ).pack(side="left", pady=(3, 0))

        pathbar = ttk.Frame(self)
        pathbar.pack(fill="x", padx=10, pady=(0, 6))
        self.btn_up = ttk.Button(pathbar, text="↑", width=3, command=self._go_up)
        self.btn_up.pack(side="left")
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(pathbar, textvariable=self.path_var, style="Path.TEntry")
        self.path_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.path_entry.bind("<Return>", self._submit_path)
        self.path_entry.bind("<KP_Enter>", self._submit_path)
        self.path_entry.bind("<Escape>", lambda _e: self._reset_path())
        self.btn_refresh = ttk.Button(pathbar, text="↻", width=3, command=self._refresh)
        self.btn_refresh.pack(side="left")

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(0, 6))
        self.btn_new = ttk.Button(toolbar, text="New Folder", command=self._new_folder)
        self.btn_new.pack(side="left")
        self.btn_rename = ttk.Button(toolbar, text="Rename…", command=self._rename)
        self.btn_rename.pack(side="left", padx=(4, 0))
        self.btn_delete = ttk.Button(toolbar, text="Delete", command=self._delete)
        self.btn_delete.pack(side="left", padx=(4, 0))
        self.btn_transfer = ttk.Button(
            toolbar, text=self.transfer_label, command=self._transfer
        )
        self.btn_transfer.pack(side="right")

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True, padx=10)
        columns = ("kind", "size", "modified", "mode")
        self.tree = ttk.Treeview(
            table, columns=columns, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="Name", command=lambda: self._sort_by("name"))
        self.tree.heading("kind", text="Kind", command=lambda: self._sort_by("kind"))
        self.tree.heading("size", text="Size", command=lambda: self._sort_by("size"))
        self.tree.heading("modified", text="Modified", command=lambda: self._sort_by("mtime"))
        self.tree.heading("mode", text="Mode", command=lambda: self._sort_by("mode"))
        self.tree.column("#0", width=220, minwidth=110, stretch=True)
        self.tree.column("kind", width=58, minwidth=48, stretch=False, anchor="w")
        self.tree.column("size", width=78, minwidth=58, stretch=False, anchor="e")
        self.tree.column("modified", width=132, minwidth=100, stretch=False, anchor="w")
        self.tree.column("mode", width=74, minwidth=64, stretch=False, anchor="w")

        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.tree.tag_configure("dir", foreground=self.palette["dir"], font=(family, 12, "bold"))
        self.tree.tag_configure("file", foreground=self.palette["fg"], font=(family, 12))
        self.tree.tag_configure("link", foreground=self.palette["muted"], font=(family, 12, "italic"))
        self.tree.tag_configure("placeholder", foreground=self.palette["muted"], font=(family, 12, "italic"))

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<KP_Enter>", self._on_return)
        self.tree.bind("<BackSpace>", lambda _e: self._go_up())
        self.tree.bind("<Delete>", lambda _e: self._delete())
        self.tree.bind("<Button-2>", self._on_context_menu)
        self.tree.bind("<Button-3>", self._on_context_menu)
        self.tree.bind("<Control-Button-1>", self._on_context_menu)
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Command-a>", self._select_all)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._selection_changed())

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=10, pady=(4, 8))
        self.footer_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.footer_label.pack(side="left")
        self.hidden_var = tk.BooleanVar(value=show_hidden)
        self.hidden_check = ttk.Checkbutton(
            footer,
            text="Hidden files",
            variable=self.hidden_var,
            command=self._toggle_hidden,
        )
        self.hidden_check.pack(side="right")

        self._update_actions()

    # -- public API -------------------------------------------------------
    def set_path(self, path: str) -> None:
        self.current_path = path
        self.path_var.set(self._display(path))
        self.path_entry.icursor("end")

    def set_items(self, items: list, select_paths: set[str] | None = None) -> None:
        if select_paths is None:
            selected = {item.path for item in self.selected_items()}
        else:
            selected = set(select_paths)
        self._items = list(items)
        self._placeholder = ""
        self._populate(selected)
        self._busy = False
        self._update_footer()
        self._update_actions()

    def set_placeholder(self, text: str) -> None:
        self._items = []
        self._rows.clear()
        self._placeholder = text
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", iid="row-placeholder", text=text, values=("", "", "", ""), tags=("placeholder",))
        self._busy = False
        self._update_footer()
        self._update_actions()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        state = "normal" if enabled else "disabled"
        self.path_entry.configure(state=state)
        self.hidden_check.state(["!disabled"] if enabled else ["disabled"])
        self._update_actions()

    def set_transfer_enabled(self, enabled: bool) -> None:
        self._transfer_ready = enabled
        self._update_actions()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_footer()
        self._update_actions()

    def set_status(self, text: str) -> None:
        self.footer_label.configure(text=text)

    def selected_items(self) -> list:
        return [
            self._rows[iid]
            for iid in self.tree.selection()
            if iid in self._rows
        ]

    def focus_first(self) -> None:
        children = self.tree.get_children()
        for iid in children:
            if iid in self._rows:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.focus_force()
                return

    def refresh_view(self) -> None:
        self._populate({item.path for item in self.selected_items()})
        self._update_footer()
        self._update_actions()

    # -- listing ----------------------------------------------------------
    def _populate(self, selected_paths: set[str]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        ordered = self._sorted(self._items)
        for index, entry in enumerate(ordered):
            iid = f"row{index}"
            self._rows[iid] = entry
            if entry.is_link:
                tag = "link"
                kind = "Link"
            elif entry.is_dir:
                tag = "dir"
                kind = "Folder"
            else:
                tag = "file"
                kind = "File"
            size = "—" if entry.is_dir else format_bytes(entry.size)
            modified = _format_mtime(entry.mtime)
            mode = getattr(entry, "mode_text", "")
            self.tree.insert(
                "",
                "end",
                iid=iid,
                text=entry.name,
                values=(kind, size, modified, mode),
                tags=(tag,),
            )
            if entry.path in selected_paths:
                self.tree.selection_add(iid)

    def _sorted(self, items: list) -> list:
        column = self._sort_column
        reverse = self._sort_reverse

        def key(entry):
            if column == "size":
                return entry.size
            if column == "mtime":
                return entry.mtime or 0
            if column == "mode":
                return entry.mode if entry.mode is not None else -1
            if column == "kind":
                return (entry.is_dir, entry.is_link)
            return (entry.name or "").lower()

        dirs = sorted([item for item in items if item.is_dir], key=key, reverse=reverse)
        files = sorted([item for item in items if not item.is_dir], key=key, reverse=reverse)
        return dirs + files

    def _sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._populate({item.path for item in self.selected_items()})
        for name in ("#0", "kind", "size", "modified", "mode"):
            label = {
                "#0": "Name", "kind": "Kind", "size": "Size",
                "modified": "Modified", "mode": "Mode",
            }[name]
            if (name == "#0" and column == "name") or name == column:
                arrow = " ▾" if self._sort_reverse else " ▴"
                self.tree.heading(name, text=label + arrow)
            else:
                self.tree.heading(name, text=label)

    def _display(self, path: str) -> str:
        if self.path_style == "local":
            home = os.path.expanduser("~")
            if path == home:
                return "~"
            if path.startswith(home + os.sep):
                return "~" + path[len(home):]
        return path

    # -- interaction ------------------------------------------------------
    def _submit_path(self, _event=None):
        text = self.path_var.get().strip()
        if text:
            self.callbacks.navigate(text)
        return "break"

    def _reset_path(self) -> None:
        self.path_var.set(self._display(self.current_path))

    def _refresh(self) -> None:
        if self._enabled:
            self.callbacks.refresh()

    def _go_up(self) -> None:
        if not self._enabled or self._placeholder:
            return
        if self.path_style == "local":
            parent = os.path.dirname(self.current_path.rstrip(os.sep)) or os.sep
        else:
            parent = posixpath.dirname(self.current_path.rstrip("/")) or "/"
        if parent == self.current_path:
            return
        self.callbacks.navigate(parent)

    def _on_double_click(self, event) -> None:
        if not self._enabled or self._placeholder:
            return
        iid = self.tree.identify_row(event.y)
        item = self._rows.get(iid)
        if item is None:
            return
        if item.is_dir:
            self.callbacks.navigate(item.path)
        elif self.callbacks.activate is not None:
            self.callbacks.activate(item)

    def _on_return(self, _event=None):
        if not self._enabled or self._placeholder:
            return "break"
        items = self.selected_items()
        if not items:
            return "break"
        item = items[0]
        if item.is_dir:
            self.callbacks.navigate(item.path)
        elif self.callbacks.activate is not None:
            self.callbacks.activate(item)
        return "break"

    def _on_context_menu(self, event) -> None:
        if not self._enabled:
            return
        iid = self.tree.identify_row(event.y)
        item = self._rows.get(iid)
        if iid and item is not None and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        menu = tk.Menu(self, tearoff=0)
        if item is not None:
            if item.is_dir:
                menu.add_command(
                    label="Open", command=lambda: self.callbacks.navigate(item.path)
                )
            if self.callbacks.transfer is not None:
                menu.add_command(label=self.transfer_label, command=self._transfer)
            if self.callbacks.open_external is not None:
                menu.add_command(label="Reveal in Finder", command=lambda: self.callbacks.open_external(item))
            menu.add_separator()
            if self.callbacks.rename is not None:
                menu.add_command(label="Rename…", command=self._rename)
            if self.callbacks.delete is not None:
                menu.add_command(label="Delete", command=self._delete)
            if self.callbacks.chmod is not None:
                menu.add_command(label="Permissions…", command=self._chmod)
            menu.add_separator()
            menu.add_command(
                label="Copy Path",
                command=lambda: self._copy_path(item.path),
            )
        menu.add_command(label="New Folder", command=self._new_folder)
        menu.add_command(label="Refresh", command=self._refresh)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _selection_changed(self) -> None:
        self._update_footer()
        self._update_actions()

    def _update_footer(self) -> None:
        if self._busy:
            self.footer_label.configure(text="Loading…")
            return
        if self._placeholder:
            self.footer_label.configure(text=self._placeholder)
            return
        count = len(self._items)
        selected = len(self.selected_items())
        if count == 0:
            text = "Empty folder"
        else:
            text = f"{count} item{'s' if count != 1 else ''}"
            if selected:
                text += f" · {selected} selected"
        self.footer_label.configure(text=text)

    def _update_actions(self) -> None:
        items = self.selected_items()
        has_selection = bool(items)
        can_mutate = self._enabled and not self._placeholder
        self.btn_up.state(["!disabled"] if can_mutate else ["disabled"])
        self.btn_refresh.state(["!disabled"] if self._enabled and not self._busy else ["disabled"])
        self.btn_new.state(["!disabled"] if can_mutate and not self._busy else ["disabled"])
        self.btn_rename.state(["!disabled"] if can_mutate and has_selection else ["disabled"])
        self.btn_delete.state(["!disabled"] if can_mutate and has_selection else ["disabled"])
        transfer_ok = can_mutate and has_selection and self._transfer_ready and not self._busy
        self.btn_transfer.state(["!disabled"] if transfer_ok else ["disabled"])

    def _new_folder(self) -> None:
        if self.callbacks.new_folder is not None:
            self.callbacks.new_folder()

    def _rename(self) -> None:
        items = self.selected_items()
        if items and self.callbacks.rename is not None:
            self.callbacks.rename(items[0])

    def _delete(self) -> None:
        items = self.selected_items()
        if items and self.callbacks.delete is not None:
            self.callbacks.delete(items)

    def _transfer(self) -> None:
        if self.callbacks.transfer is not None:
            self.callbacks.transfer()

    def _chmod(self) -> None:
        items = self.selected_items()
        if items and self.callbacks.chmod is not None:
            self.callbacks.chmod(items[0])

    def _toggle_hidden(self) -> None:
        if self.callbacks.hidden_changed is not None:
            self.callbacks.hidden_changed(self.hidden_var.get())

    def _copy_path(self, path: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(path)
        self.set_status("Path copied to clipboard")


def _format_mtime(value: float | None) -> str:
    if not value:
        return ""
    try:
        return time.strftime("%d %b %Y  %H:%M", time.localtime(value))
    except (OverflowError, ValueError, OSError):
        return ""

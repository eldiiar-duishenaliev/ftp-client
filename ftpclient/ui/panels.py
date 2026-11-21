"""Bottom panels: transfer queue and activity log."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from ..transfer import format_bytes, format_duration

BAR_WIDTH = 12


class TransferPanel(ttk.Frame):
    def __init__(
        self,
        master,
        palette: dict,
        *,
        on_cancel,
        on_retry,
        on_remove,
        on_clear,
        on_reveal,
        on_error,
    ) -> None:
        super().__init__(master, padding=(10, 8))
        self.palette = palette
        self.on_cancel = on_cancel
        self.on_retry = on_retry
        self.on_remove = on_remove
        self.on_clear = on_clear
        self.on_reveal = on_reveal
        self.on_error = on_error
        self._tasks: dict[int, object] = {}

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        self.btn_cancel = ttk.Button(toolbar, text="Cancel", command=self._cancel_selected)
        self.btn_cancel.pack(side="left")
        self.btn_retry = ttk.Button(toolbar, text="Retry", command=self._retry_selected)
        self.btn_retry.pack(side="left", padx=(4, 0))
        self.btn_remove = ttk.Button(toolbar, text="Remove", command=self._remove_selected)
        self.btn_remove.pack(side="left", padx=(4, 0))
        self.btn_clear = ttk.Button(toolbar, text="Clear Finished", command=self.on_clear)
        self.btn_clear.pack(side="left", padx=(4, 0))
        self.btn_error = ttk.Button(
            toolbar, text="Show Error", command=self._show_error_selected
        )
        self.btn_error.pack(side="left", padx=(4, 0))

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        columns = ("type", "name", "size", "progress", "speed", "eta", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=7)
        headings = {
            "type": ("Type", 70, "w"),
            "name": ("File", 220, "w"),
            "size": ("Size", 80, "e"),
            "progress": ("Progress", 200, "w"),
            "speed": ("Speed", 78, "e"),
            "eta": ("Left", 62, "e"),
            "status": ("Status", 180, "w"),
        }
        for key, (label, width, anchor) in headings.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        family = palette["family"]
        self.tree.tag_configure("done", foreground="#1f8b4c")
        self.tree.tag_configure("failed", foreground="#c0392b")
        self.tree.tag_configure("cancelled", foreground=palette["muted"])
        self.tree.tag_configure("active", foreground=palette["fg"], font=(family, 11, "bold"))
        self.tree.tag_configure("placeholder", foreground=palette["muted"], font=(family, 11, "italic"))

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-2>", self._on_context_menu)
        self.tree.bind("<Button-3>", self._on_context_menu)
        self.tree.bind("<Control-Button-1>", self._on_context_menu)

    # -- rendering --------------------------------------------------------
    def update_tasks(self, tasks: list) -> None:
        selected = self._selected_ids()
        current_ids = {str(task.task_id) for task in tasks}
        self._tasks = {task.task_id: task for task in tasks}

        if not tasks:
            if self.tree.get_children() != ("placeholder",):
                self._clear_tree()
                self.tree.insert(
                    "",
                    "end",
                    iid="placeholder",
                    values=(
                        "",
                        "No transfers yet — select a file and use Upload or Download.",
                        "", "", "", "", "",
                    ),
                    tags=("placeholder",),
                )
            self._update_buttons()
            return

        if "placeholder" in self.tree.get_children():
            self._clear_tree()

        for iid in self.tree.get_children():
            if iid not in current_ids:
                self.tree.delete(iid)

        for task in tasks:
            iid = str(task.task_id)
            values = self._values(task)
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=(self._tag(task),))
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=(self._tag(task),))

        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self._update_buttons()

    def _clear_tree(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _values(self, task) -> tuple:
        kind = "Upload" if task.direction == "upload" else "Download"
        name = ("▸ " if task.kind == "tree" else "   ") + task.display_name
        size = format_bytes(task.total_bytes) if task.total_bytes else "—"
        progress = self._progress_text(task)
        speed = f"{format_bytes(task.speed)}/s" if task.status == "running" and task.speed else ""
        eta = format_duration(task.eta_seconds) if task.status == "running" else ""
        return (kind, name, size, progress, speed, eta, self._status_text(task))

    @staticmethod
    def _progress_text(task) -> str:
        if task.status == "done":
            return "█" * BAR_WIDTH + " 100%"
        if task.status == "queued":
            return "waiting"
        if task.status in ("cancelled", "failed") and task.total_bytes <= 0:
            return "—"
        if task.total_bytes <= 0:
            return "scanning…"
        percent = task.percent
        filled = round(percent / 100 * BAR_WIDTH)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        return f"{bar} {percent:3.0f}%"

    @staticmethod
    def _status_text(task) -> str:
        labels = {
            "queued": "Queued",
            "done": "Completed",
            "failed": "Failed",
            "cancelled": "Cancelled",
        }
        if task.status == "running":
            if task.kind == "tree" and task.total_bytes <= 0:
                return "Scanning folder…"
            return "Uploading…" if task.direction == "upload" else "Downloading…"
        text = labels.get(task.status, task.status.title())
        if task.status == "failed" and task.error:
            short = task.error.replace("\n", " ").strip()
            if len(short) > 34:
                short = short[:31] + "…"
            text = f"Failed — {short}"
        elif task.status == "running" and task.error:
            text = task.error
        return text

    @staticmethod
    def _tag(task) -> str:
        if task.status == "running":
            return "active"
        if task.status in ("done", "failed", "cancelled"):
            return task.status
        return "queued"

    # -- selection --------------------------------------------------------
    def _selected_tasks(self) -> list:
        return [
            self._tasks[int(iid)]
            for iid in self.tree.selection()
            if iid.isdigit() and int(iid) in self._tasks
        ]

    def _selected_ids(self) -> list[str]:
        return list(self.tree.selection())

    def _update_buttons(self) -> None:
        tasks = self._selected_tasks()
        can_cancel = any(t.status in ("queued", "running") for t in tasks)
        can_retry = any(t.status in ("failed", "cancelled") for t in tasks)
        can_remove = any(t.status in ("done", "failed", "cancelled") for t in tasks)
        can_show_error = any(t.status == "failed" and t.error for t in tasks)
        self.btn_cancel.state(["!disabled"] if can_cancel else ["disabled"])
        self.btn_retry.state(["!disabled"] if can_retry else ["disabled"])
        self.btn_remove.state(["!disabled"] if can_remove else ["disabled"])
        self.btn_error.state(["!disabled"] if can_show_error else ["disabled"])
        self.btn_clear.state(["!disabled"] if any(not t.is_active for t in self._tasks.values()) else ["disabled"])

    def _cancel_selected(self) -> None:
        for task in self._selected_tasks():
            if task.status in ("queued", "running"):
                self.on_cancel(task)

    def _retry_selected(self) -> None:
        for task in self._selected_tasks():
            if task.status in ("failed", "cancelled"):
                self.on_retry(task)

    def _remove_selected(self) -> None:
        for task in self._selected_tasks():
            if not task.is_active:
                self.on_remove(task)

    def _show_error_selected(self) -> None:
        for task in self._selected_tasks():
            if task.status == "failed" and task.error:
                self.on_error(task)
                return

    def _on_double_click(self, _event) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            return
        task = tasks[0]
        if task.status == "failed":
            self.on_error(task)
        elif task.status == "done" and task.direction == "download":
            self.on_reveal(task)

    def _on_context_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and iid in self._tasks:
            self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Cancel", command=self._cancel_selected)
        menu.add_command(label="Retry", command=self._retry_selected)
        menu.add_command(label="Remove from List", command=self._remove_selected)
        menu.add_separator()
        menu.add_command(label="Show Error", command=self._show_error_selected)
        menu.add_command(label="Show in Finder", command=self._reveal_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _reveal_selected(self) -> None:
        for task in self._selected_tasks():
            if task.status == "done" and task.direction == "download":
                self.on_reveal(task)
                return


class LogPanel(ttk.Frame):
    def __init__(self, master, palette: dict) -> None:
        super().__init__(master, padding=(10, 8))
        self.palette = palette

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(toolbar, text="Copy All", command=self.copy_all).pack(side="left", padx=(4, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body,
            height=7,
            wrap="word",
            font=(palette["mono"], 11),
            background=palette["log_bg"],
            foreground=palette["fg"],
            insertbackground=palette["fg"],
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            state="disabled",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.text.tag_configure("time", foreground=palette["muted"])
        self.text.tag_configure("info", foreground=palette["fg"])
        self.text.tag_configure("success", foreground="#1f8b4c")
        self.text.tag_configure("error", foreground="#c0392b")
        self.text.tag_configure("warning", foreground="#b26a00")

    def append(self, message: str, level: str = "info") -> None:
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        self.text.insert("end", time.strftime("%H:%M:%S  "), "time")
        self.text.insert("end", message.rstrip() + "\n", level if level in ("info", "success", "error", "warning") else "info")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_all(self) -> None:
        content = self.text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)

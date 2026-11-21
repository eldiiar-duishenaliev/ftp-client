"""Visual theme: fonts, colours and ttk styles that fit macOS."""

from __future__ import annotations

import subprocess
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

ACCENT = "#0a66ff"
ACCENT_DARK = "#4c9aff"
GREEN = "#1f8b4c"
RED = "#c0392b"
AMBER = "#b26a00"

LIGHT = {
    "bg": "#ffffff",
    "fg": "#1d1d1f",
    "dir": "#0a58c8",
    "muted": "#86868b",
    "heading_bg": "#f5f5f7",
    "stripe": "#fbfbfd",
    "selected": "#0a66ff",
    "selected_fg": "#ffffff",
    "border": "#d9d9de",
    "log_bg": "#fbfbfd",
}

DARK = {
    "bg": "#1e1e1f",
    "fg": "#ececee",
    "dir": "#79b8ff",
    "muted": "#98989f",
    "heading_bg": "#2a2a2c",
    "stripe": "#232325",
    "selected": "#0a58c8",
    "selected_fg": "#ffffff",
    "border": "#3a3a3c",
    "log_bg": "#181819",
}


def is_dark_mode() -> bool:
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip().lower() == "dark"
    except Exception:
        return False


def configure_styles(root: tk.Misc) -> dict:
    style = ttk.Style(root)
    for theme in ("aqua", "clam", "default"):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue

    palette = dict(DARK if is_dark_mode() else LIGHT)
    family = tkfont.nametofont("TkDefaultFont").actual("family") or "Helvetica"
    mono = "Menlo" if "Menlo" in tkfont.families(root) else "Courier"

    style.configure("Treeview", rowheight=26, borderwidth=0)
    try:
        style.configure(
            "Treeview",
            background=palette["bg"],
            fieldbackground=palette["bg"],
            foreground=palette["fg"],
        )
        style.map(
            "Treeview",
            background=[("selected", palette["selected"])],
            foreground=[("selected", palette["selected_fg"])],
        )
        style.configure(
            "Treeview.Heading",
            background=palette["heading_bg"],
            foreground=palette["muted"],
            font=(family, 11, "bold"),
            relief="flat",
            padding=(6, 5),
        )
        style.map("Treeview.Heading", background=[("active", palette["heading_bg"])])
    except tk.TclError:
        pass

    style.configure("Title.TLabel", font=(family, 14, "bold"))
    style.configure("Subtitle.TLabel", font=(family, 11), foreground=palette["muted"])
    style.configure("Muted.TLabel", font=(family, 11), foreground=palette["muted"])
    style.configure("Badge.TLabel", font=(family, 9, "bold"), foreground=palette["muted"])
    style.configure("Accent.TButton", font=(family, 12, "bold"))
    style.configure("Status.TLabel", font=(family, 11), foreground=palette["muted"])
    style.configure("Path.TEntry", padding=(6, 4))
    style.configure("Card.TFrame", relief="flat", borderwidth=0)
    style.configure("Toolbar.TButton", font=(family, 11))

    palette["family"] = family
    palette["mono"] = mono
    palette["style"] = style
    return palette

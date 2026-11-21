"""Main window and application logic."""

from __future__ import annotations

import os
import posixpath
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Callable

from .. import APP_NAME, __version__, keychain
from ..backends import AuthError, BackendError, create_backend
from ..config import Config
from ..models import Profile, RemoteEntry, join_remote, normalise_remote, parent_remote
from ..transfer import TransferManager, format_bytes
from .dialogs import ConnectionDialog, PermissionsDialog
from .filepane import FilePane, LocalEntry, PaneCallbacks
from .panels import LogPanel, TransferPanel
from .theme import GREEN, RED, configure_styles

HOME = os.path.expanduser("~")


def _shorten(text: str, limit: int = 58) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _open_in_finder(path: str, reveal: bool = True) -> None:
    target = path if os.path.exists(path) else os.path.dirname(path)
    try:
        if reveal and os.path.isfile(target):
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["open", target])
    except OSError:
        pass


class FTPClientApp(tk.Tk):
    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.config_store = config or Config()
        self.settings = self.config_store.load_settings()
        self.profiles: list[Profile] = self.config_store.load_profiles()

        self.backend = None
        self.profile: Profile | None = None
        self._closing = False
        self.password = ""
        self.passphrase = ""
        self.remote_path = "/"
        self.remote_items: list[RemoteEntry] = []
        self.session = 0
        self._connecting = False
        self._busy_ops = 0
        self._pending_refresh: set[str] = set()
        self._was_pending = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._browser_queue: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._browser_loop, name="browser", daemon=True
        ).start()

        self.title(APP_NAME)
        try:
            self.tk.call("tk", "appname", APP_NAME)
        except tk.TclError:
            pass
        self.minsize(1000, 640)
        self._apply_saved_geometry()

        self.palette = configure_styles(self)
        self._configure_extra_styles()

        self.transfer_manager = TransferManager(self._create_transfer_backend, self._emit)
        self.transfer_manager.start()

        self._build_menu()
        self._build_ui()
        self._reload_profiles(self.settings.get("last_profile", ""))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()

        self._set_remote_placeholder()
        self._update_connection_ui()
        self._refresh_local(self.settings.get("local_path") or HOME, silent=True)
        self._log_welcome()
        self.after(80, self._pump)
        try:
            self.createcommand("tk::mac::Quit", self._on_close)
        except tk.TclError:
            pass

    # -- setup ------------------------------------------------------------
    def _apply_saved_geometry(self) -> None:
        geometry = self.settings.get("window_geometry") or "1220x780"
        try:
            self.geometry(geometry)
        except tk.TclError:
            self.geometry("1220x780")

    def _configure_extra_styles(self) -> None:
        style = ttk.Style(self)
        family = self.palette["family"]
        style.configure("DotOn.TLabel", foreground=GREEN, font=(family, 16))
        style.configure("DotOff.TLabel", foreground=self.palette["muted"], font=(family, 16))
        style.configure("DotBusy.TLabel", foreground="#b26a00", font=(family, 16))
        style.configure("Error.TLabel", foreground=RED, font=(family, 11))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="New Connection…", accelerator="Cmd+N", command=self._new_connection
        )
        file_menu.add_command(
            label="Edit Connection…", accelerator="Cmd+E", command=self._edit_connection
        )
        file_menu.add_command(
            label="Disconnect", accelerator="Cmd+D", command=lambda: self._disconnect(interactive=True)
        )
        file_menu.add_separator()
        file_menu.add_command(label="Refresh", accelerator="Cmd+R", command=self._refresh_both)
        file_menu.add_separator()
        file_menu.add_command(label="Close Window", accelerator="Cmd+W", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        transfer_menu = tk.Menu(menubar, tearoff=0)
        transfer_menu.add_command(
            label="Upload Selected", accelerator="Cmd+U", command=self._upload_selection
        )
        transfer_menu.add_command(
            label="Download Selected",
            accelerator="Cmd+Shift+U",
            command=self._download_selection,
        )
        transfer_menu.add_separator()
        transfer_menu.add_command(
            label="New Remote Folder",
            accelerator="Cmd+Shift+N",
            command=self._remote_new_folder,
        )
        transfer_menu.add_separator()
        transfer_menu.add_command(label="Cancel All Transfers", command=self._cancel_all)
        menubar.add_cascade(label="Transfer", menu=transfer_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self.local_hidden_var = tk.BooleanVar(value=self.settings.get("show_hidden_local", False))
        self.remote_hidden_var = tk.BooleanVar(value=self.settings.get("show_hidden_remote", True))
        view_menu.add_checkbutton(
            label="Show Hidden Files Locally",
            variable=self.local_hidden_var,
            command=lambda: self._hidden_menu_changed("local"),
        )
        view_menu.add_checkbutton(
            label="Show Hidden Files on Server",
            variable=self.remote_hidden_var,
            command=lambda: self._hidden_menu_changed("remote"),
        )
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About FTP Client", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=menubar)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(12, 10, 12, 4))
        toolbar.pack(fill="x")

        ttk.Label(toolbar, text="Connection").pack(side="left")
        self.profile_combo = ttk.Combobox(toolbar, state="readonly", width=26)
        self.profile_combo.pack(side="left", padx=(8, 0))
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_connection_ui())

        self.btn_new = ttk.Button(toolbar, text="New…", command=self._new_connection)
        self.btn_new.pack(side="left", padx=(6, 0))
        self.btn_edit = ttk.Button(toolbar, text="Edit…", command=self._edit_connection)
        self.btn_edit.pack(side="left", padx=(4, 0))

        self.btn_connect = ttk.Button(toolbar, text="Connect", command=self._on_connect)
        self.btn_connect.pack(side="left", padx=(10, 0))
        self.btn_disconnect = ttk.Button(
            toolbar, text="Disconnect", command=lambda: self._disconnect(interactive=True)
        )
        self.btn_disconnect.pack(side="left", padx=(4, 0))

        status_box = ttk.Frame(toolbar)
        status_box.pack(side="right")
        self.dot = ttk.Label(status_box, text="●", style="DotOff.TLabel")
        self.dot.pack(side="left", padx=(0, 6))
        self.connection_label = ttk.Label(status_box, text="Not connected", style="Muted.TLabel")
        self.connection_label.pack(side="left")

        statusbar = ttk.Frame(self, padding=(12, 4, 12, 6))
        statusbar.pack(side="bottom", fill="x")
        self.activity_label = ttk.Label(statusbar, text="Ready", style="Status.TLabel")
        self.activity_label.pack(side="left")
        self.queue_label = ttk.Label(statusbar, text="", style="Status.TLabel")
        self.queue_label.pack(side="right")

        self.vertical_panes = ttk.Panedwindow(self, orient="vertical")
        self.vertical_panes.pack(fill="both", expand=True, padx=12, pady=(6, 2))

        self.notebook = ttk.Notebook(self.vertical_panes)

        self.transfer_panel = TransferPanel(
            self.notebook,
            self.palette,
            on_cancel=self._cancel_task,
            on_retry=self._retry_task,
            on_remove=self._remove_task,
            on_clear=self._clear_finished,
            on_reveal=self._reveal_task,
            on_error=self._show_task_error,
        )
        self.log_panel = LogPanel(self.notebook, self.palette)
        self.notebook.add(self.transfer_panel, text="  Transfers  ")
        self.notebook.add(self.log_panel, text="  Log  ")

        panes = ttk.Panedwindow(self.vertical_panes, orient="horizontal")
        self.local_pane = FilePane(
            panes,
            side="local",
            title="Local Files",
            palette=self.palette,
            transfer_label="Upload →",
            path_style="local",
            show_hidden=self.settings.get("show_hidden_local", False),
            callbacks=PaneCallbacks(
                navigate=self._local_navigate,
                refresh=lambda: self._refresh_local(),
                activate=lambda item: self._upload_items([item]),
                new_folder=self._local_new_folder,
                rename=self._local_rename,
                delete=self._local_delete,
                transfer=self._upload_selection,
                open_external=lambda item: _open_in_finder(item.path),
                hidden_changed=self._local_hidden_changed,
            ),
        )
        self.remote_pane = FilePane(
            panes,
            side="remote",
            title="Server Files",
            palette=self.palette,
            transfer_label="← Download",
            path_style="posix",
            show_hidden=self.settings.get("show_hidden_remote", True),
            callbacks=PaneCallbacks(
                navigate=self._remote_navigate,
                refresh=lambda: self._refresh_remote(),
                activate=lambda item: self._download_items([item]),
                new_folder=self._remote_new_folder,
                rename=self._remote_rename,
                delete=self._remote_delete,
                transfer=self._download_selection,
                chmod=self._remote_chmod,
                hidden_changed=self._remote_hidden_changed,
            ),
        )
        panes.add(self.local_pane, weight=1)
        panes.add(self.remote_pane, weight=1)
        self.vertical_panes.add(panes, weight=4)
        self.vertical_panes.add(self.notebook, weight=1)
        self.after(120, self._place_sashes)

        self.remote_pane.set_enabled(False)
        self.remote_pane.set_transfer_enabled(False)
        self.local_pane.set_transfer_enabled(False)

    def _place_sashes(self) -> None:
        try:
            width = self.winfo_width()
            height = self.winfo_height()
            local_sash = int(self.settings.get("sash_local") or 0)
            vertical_sash = int(self.settings.get("sash_vertical") or 0)
            self.local_pane.master.sashpos(
                0, local_sash if local_sash > 200 else max(300, width // 2 - 12)
            )
            self.vertical_panes.sashpos(
                0, vertical_sash if vertical_sash > 200 else max(360, height - 300)
            )
        except (tk.TclError, ValueError):
            pass

    def _bind_shortcuts(self) -> None:
        bindings = {
            "<Command-n>": lambda _e: self._new_connection(),
            "<Command-e>": lambda _e: self._edit_connection(),
            "<Command-d>": lambda _e: self._disconnect(interactive=True),
            "<Command-r>": lambda _e: self._refresh_both(),
            "<Command-u>": lambda _e: self._upload_selection(),
            "<Command-U>": lambda _e: self._download_selection(),
            "<Command-Shift-N>": lambda _e: self._remote_new_folder(),
            "<Command-w>": lambda _e: self._on_close(),
            "<F5>": lambda _e: self._refresh_both(),
        }
        for sequence, callback in bindings.items():
            self.bind_all(sequence, callback)

    # -- profiles ---------------------------------------------------------
    def _reload_profiles(self, select: str = "") -> None:
        names = [profile.name for profile in self.profiles]
        self.profile_combo.configure(values=names)
        current = self.profile_combo.get()
        if select and select in names:
            self.profile_combo.set(select)
        elif current in names:
            self.profile_combo.set(current)
        elif names:
            self.profile_combo.set(names[0])
        else:
            self.profile_combo.set("")
        self._update_connection_ui()

    def _selected_profile(self) -> Profile | None:
        name = self.profile_combo.get().strip()
        if not name:
            return None
        return next((profile for profile in self.profiles if profile.name == name), None)

    def _new_connection(self) -> None:
        self._open_connection_dialog(None)

    def _edit_connection(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            messagebox.showinfo(
                "No connection selected",
                "Choose a saved connection first, or create a new one.",
                parent=self,
            )
            return
        self._open_connection_dialog(profile)

    def _open_connection_dialog(
        self,
        profile: Profile | None,
        *,
        initial_error: str = "",
        password: str = "",
        passphrase: str = "",
    ) -> None:
        editing_account = profile.keychain_account if profile else None
        has_saved = bool(profile) and keychain.is_available()
        dialog = ConnectionDialog(
            self,
            profile=profile,
            password=password,
            passphrase=passphrase,
            has_saved_password=has_saved,
            initial_error=initial_error,
        )
        self.wait_window(dialog)

        if dialog.deleted:
            if profile is None:
                return
            self.profiles = [
                item for item in self.profiles if item.keychain_account != editing_account
            ]
            self.config_store.save_profiles(self.profiles)
            keychain.delete_password(editing_account or "")
            self._reload_profiles()
            if self.profile and self.profile.keychain_account == editing_account:
                self._disconnect(interactive=False)
            self.log(f"Deleted connection “{profile.name}”.", "info")
            return

        if dialog.result is None:
            return

        new_profile, new_password, new_passphrase, remember = dialog.result
        used_password = new_password
        if profile is not None and not new_password:
            used_password = None  # keep the password already in the Keychain

        if not self._store_profile(new_profile, new_password, remember, editing_account):
            return
        self._reload_profiles(new_profile.name)
        self._connect(
            new_profile,
            password=used_password,
            passphrase=new_passphrase,
            from_dialog=True,
        )

    def _store_profile(
        self,
        profile: Profile,
        password: str,
        remember: bool,
        editing_account: str | None,
    ) -> bool:
        if editing_account:
            index = next(
                (
                    i
                    for i, item in enumerate(self.profiles)
                    if item.keychain_account == editing_account
                ),
                None,
            )
            if index is None:
                index = next(
                    (i for i, item in enumerate(self.profiles) if item.name == profile.name),
                    None,
                )
        else:
            index = next(
                (i for i, item in enumerate(self.profiles) if item.name == profile.name),
                None,
            )
            if index is not None:
                replace = messagebox.askyesno(
                    "Name already used",
                    f"A connection named “{profile.name}” already exists. Replace it?",
                    parent=self,
                )
                if not replace:
                    return False

        if index is None:
            self.profiles.append(profile)
        else:
            replaced = self.profiles[index]
            self.profiles[index] = profile
            if not editing_account and replaced.keychain_account != profile.keychain_account:
                keychain.delete_password(replaced.keychain_account)
        self.config_store.save_profiles(self.profiles)

        account_changed = bool(
            editing_account and editing_account != profile.keychain_account
        )
        if remember and password:
            if not keychain.store_password(profile.keychain_account, password):
                self.log("Could not save the password in the Keychain.", "warning")
        elif not remember:
            keychain.delete_password(profile.keychain_account)

        if account_changed:
            old_password = keychain.load_password(editing_account or "")
            if remember and not password and old_password:
                keychain.store_password(profile.keychain_account, old_password)
            keychain.delete_password(editing_account or "")
        return True

    # -- connection -------------------------------------------------------
    def _on_connect(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self._new_connection()
            return
        self._connect(profile)

    def _connect(
        self,
        profile: Profile,
        password: str | None = None,
        passphrase: str | None = None,
        from_dialog: bool = False,
    ) -> None:
        if (self.backend is not None or self._connecting) and not self._disconnect(
            interactive=True
        ):
            return

        self.session += 1
        token = self.session
        self._connecting = True
        self.profile = profile
        self._update_connection_ui()
        self._update_activity()
        self.log(
            f"Connecting to {profile.username}@{profile.host}:{profile.port} "
            f"({profile.protocol.upper()})…"
        )

        def work() -> None:
            backend = None
            try:
                resolved_password = password
                if resolved_password is None:
                    resolved_password = keychain.load_password(profile.keychain_account) or ""
                backend = create_backend(profile, resolved_password, passphrase or "")
                backend.connect()
                start = (profile.remote_path or "").strip() or backend.home or "/"
                home = backend.home or "/"
                if start in ("~", "~/") or not start:
                    start = home
                elif start.startswith("~/"):
                    start = home.rstrip("/") + "/" + start[2:]
                start = normalise_remote(start)
                try:
                    entries = backend.listdir(start)
                except BackendError:
                    start = backend.home or "/"
                    entries = backend.listdir(start)
                self._ui_queue.put(
                    (
                        "connect_ok",
                        token,
                        backend,
                        resolved_password,
                        passphrase or "",
                        start,
                        entries,
                    )
                )
            except Exception as exc:
                if backend is not None:
                    try:
                        backend.close()
                    except Exception:
                        pass
                self._ui_queue.put(
                    ("connect_error", token, exc, profile, from_dialog)
                )

        threading.Thread(target=work, name="connect", daemon=True).start()

    def _handle_connect_ok(
        self, token, backend, password, passphrase, start, entries
    ) -> None:
        if token != self.session:
            try:
                backend.close()
            except Exception:
                pass
            return
        self.backend = backend
        self.password = password
        self.passphrase = passphrase
        self.profile = next(
            (p for p in self.profiles if p.name == (self.profile.name if self.profile else "")),
            self.profile,
        )
        self._connecting = False
        self.remote_path = start
        self.remote_items = entries
        self.remote_pane.set_enabled(True)
        self.remote_pane.set_path(start)
        self._render_remote()
        self._set_remote_ready(True)

        profile = self.profile
        if profile is not None:
            self.log(
                f"Connected to {profile.host} as {profile.username} — "
                f"{len(entries)} items in {start}.",
                "success",
            )
            self.settings["last_profile"] = profile.name
            self._save_settings()
        self._update_connection_ui()

    def _handle_connect_error(self, token, exc, profile, from_dialog) -> None:
        if token != self.session:
            return
        self._connecting = False
        self.backend = None
        self._update_connection_ui()
        self._set_remote_placeholder()
        message = str(exc) or type(exc).__name__
        self.log(f"Connection failed: {message}", "error")
        messagebox.showerror(
            "Connection failed",
            f"Could not connect to {profile.host}:{profile.port}.\n\n{message}",
            parent=self,
        )
        if isinstance(exc, AuthError) and not from_dialog:
            self._open_connection_dialog(profile, initial_error=message)

    def _disconnect(self, interactive: bool = True) -> bool:
        self.session += 1
        if self._connecting:
            self._connecting = False

        if (
            self.transfer_manager.has_pending()
            and interactive
            and not messagebox.askyesno(
                "Transfers in progress",
                "Active transfers will be cancelled. Disconnect anyway?",
                parent=self,
            )
        ):
            return False
        self.transfer_manager.stop()
        self.transfer_manager.start()

        backend = self.backend
        self.backend = None
        if backend is not None:
            threading.Thread(target=self._safe_close, args=(backend,), daemon=True).start()
            self.log("Disconnected.")
        self.profile = None
        self.password = ""
        self.passphrase = ""
        self.remote_path = "/"
        self.remote_items = []
        self._update_connection_ui()
        self._set_remote_placeholder()
        return True

    @staticmethod
    def _safe_close(backend) -> None:
        try:
            backend.close()
        except Exception:
            pass

    def _set_remote_ready(self, ready: bool) -> None:
        self.remote_pane.set_enabled(ready)
        self.local_pane.set_transfer_enabled(ready)
        if not ready:
            self.remote_pane.set_transfer_enabled(False)
        else:
            self.remote_pane.set_transfer_enabled(True)

    def _set_remote_placeholder(self) -> None:
        self.remote_pane.set_path("/")
        self.remote_pane.set_placeholder("Not connected — choose a connection and press Connect")
        self.remote_pane.set_enabled(False)
        self.remote_pane.set_transfer_enabled(False)
        self.local_pane.set_transfer_enabled(False)

    def _update_connection_ui(self) -> None:
        connected = self.backend is not None
        connecting = self._connecting
        profile = self.profile if (connected or connecting) else None

        self.btn_connect.state(["disabled"] if connected or connecting else ["!disabled"])
        self.btn_connect.configure(
            text="Connected" if connected else ("Connecting…" if connecting else "Connect")
        )
        self.btn_disconnect.state(
            ["!disabled"] if connected or connecting else ["disabled"]
        )
        has_profile = self._selected_profile() is not None
        self.btn_edit.state(["!disabled"] if has_profile else ["disabled"])

        if connected:
            self.dot.configure(style="DotOn.TLabel")
            label = "Connected"
            if profile is not None:
                label = f"Connected — {profile.username}@{profile.host} · {profile.protocol.upper()}"
            self.connection_label.configure(text=_shorten(label), style="Muted.TLabel")
            self.title(f"{APP_NAME} — {profile.host} ({profile.protocol.upper()})" if profile else APP_NAME)
        elif connecting:
            self.dot.configure(style="DotBusy.TLabel")
            self.connection_label.configure(text="Connecting…", style="Muted.TLabel")
            self.title(f"{APP_NAME} — Connecting…")
        else:
            self.dot.configure(style="DotOff.TLabel")
            self.connection_label.configure(text="Not connected", style="Muted.TLabel")
            self.title(APP_NAME)

        self.remote_pane.set_enabled(connected)
        self.local_pane.set_transfer_enabled(connected)
        if connected:
            self.remote_pane.set_transfer_enabled(True)

    # -- local browsing ---------------------------------------------------
    def _refresh_local(
        self,
        path: str | None = None,
        select_path: str | None = None,
        silent: bool = False,
    ) -> None:
        target = path if path is not None else self.local_pane.current_path
        target = os.path.expanduser(target or HOME)
        if not os.path.isabs(target):
            target = os.path.join(self.local_pane.current_path, target)
        target = os.path.abspath(target)
        if not os.path.isdir(target):
            if silent:
                fallback = target
                while fallback and not os.path.isdir(fallback):
                    parent = os.path.dirname(fallback)
                    fallback = parent if parent != fallback else HOME
                if fallback != target:
                    self._refresh_local(fallback, silent=True)
                return
            self.local_pane.set_path(self.local_pane.current_path)
            messagebox.showerror(
                "Folder not found", f"“{target}” is not a folder.", parent=self
            )
            return
        show_hidden = self.local_pane.hidden_var.get()
        try:
            items = self._list_local(target, show_hidden)
        except PermissionError:
            if silent:
                self.log(f"Permission denied for local folder {target}.", "warning")
                return
            self.local_pane.set_path(self.local_pane.current_path)
            messagebox.showerror(
                "Permission denied", f"macOS does not allow access to “{target}”.", parent=self
            )
            return
        except OSError as exc:
            if silent:
                self.log(f"Cannot refresh local folder {target}: {exc}", "warning")
                return
            self.local_pane.set_path(self.local_pane.current_path)
            messagebox.showerror("Cannot open folder", str(exc), parent=self)
            return

        self.local_pane.set_path(target)
        self.local_pane.set_items(items, {select_path} if select_path else None)
        self.settings["local_path"] = target

    @staticmethod
    def _list_local(path: str, show_hidden: bool) -> list[LocalEntry]:
        items: list[LocalEntry] = []
        with os.scandir(path) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                items.append(
                    LocalEntry(
                        name=entry.name,
                        path=entry.path,
                        is_dir=is_dir,
                        size=0 if is_dir else info.st_size,
                        mtime=info.st_mtime,
                        mode=info.st_mode,
                        is_link=is_link,
                    )
                )
        return items

    def _local_navigate(self, raw: str) -> None:
        text = os.path.expanduser(raw.strip())
        if not text:
            self.local_pane.set_path(self.local_pane.current_path)
            return
        if not os.path.isabs(text):
            text = os.path.join(self.local_pane.current_path, text)
        target = os.path.abspath(text)
        if not os.path.isdir(target):
            self.local_pane.set_path(self.local_pane.current_path)
            messagebox.showerror(
                "Folder not found", f"“{target}” is not a folder.", parent=self
            )
            return
        self._refresh_local(target)

    def _local_new_folder(self) -> None:
        name = simpledialog.askstring("New Folder", "Folder name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or "/" in name or name in (".", ".."):
            messagebox.showerror("Invalid name", "Enter a single folder name.", parent=self)
            return
        target = os.path.join(self.local_pane.current_path, name)
        if os.path.exists(target):
            messagebox.showerror(
                "Already exists", f"“{name}” already exists here.", parent=self
            )
            return
        try:
            os.makedirs(target)
        except OSError as exc:
            messagebox.showerror("Cannot create folder", str(exc), parent=self)
            return
        self._refresh_local(select_path=target)
        self.log(f"Created local folder {target}.")

    def _local_rename(self, item) -> None:
        new_name = simpledialog.askstring(
            "Rename", "New name:", initialvalue=item.name, parent=self
        )
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or "/" in new_name or new_name in (".", ".."):
            messagebox.showerror("Invalid name", "Enter a single name.", parent=self)
            return
        target = os.path.join(os.path.dirname(item.path), new_name)
        if target == item.path:
            return
        if os.path.exists(target):
            messagebox.showerror(
                "Already exists", f"“{new_name}” already exists here.", parent=self
            )
            return
        try:
            os.rename(item.path, target)
        except OSError as exc:
            messagebox.showerror("Rename failed", str(exc), parent=self)
            return
        self._refresh_local(select_path=target)
        self.log(f"Renamed to {new_name}.")

    def _local_delete(self, items: list) -> None:
        if not items:
            return
        if self.settings.get("confirm_delete", True):
            names = ", ".join(item.name for item in items[:5])
            if len(items) > 5:
                names += f" and {len(items) - 5} more"
            if not messagebox.askyesno(
                "Delete locally",
                f"Delete {names}?\n\nThis removes the item from your Mac permanently.",
                parent=self,
            ):
                return
        errors = []
        for item in items:
            try:
                if item.is_dir and not item.is_link:
                    shutil.rmtree(item.path)
                else:
                    os.unlink(item.path)
            except OSError as exc:
                errors.append(f"{item.name}: {exc}")
        self._refresh_local()
        if errors:
            self.log("Some local items could not be deleted: " + "; ".join(errors), "error")
            messagebox.showerror(
                "Some items were not deleted", "\n".join(errors), parent=self
            )
        else:
            self.log(f"Deleted {len(items)} local item(s).")

    def _local_hidden_changed(self, value: bool) -> None:
        self.settings["show_hidden_local"] = value
        self.local_hidden_var.set(value)
        self._save_settings()
        self._refresh_local()

    def _remote_hidden_changed(self, value: bool) -> None:
        self.settings["show_hidden_remote"] = value
        self.remote_hidden_var.set(value)
        self._save_settings()
        self._render_remote()

    def _hidden_menu_changed(self, side: str) -> None:
        if side == "local":
            value = self.local_hidden_var.get()
            self.local_pane.hidden_var.set(value)
            self._local_hidden_changed(value)
        else:
            value = self.remote_hidden_var.get()
            self.remote_pane.hidden_var.set(value)
            self._remote_hidden_changed(value)

    # -- remote browsing --------------------------------------------------
    def _render_remote(self, select_path: str | None = None) -> None:
        show_hidden = self.remote_pane.hidden_var.get()
        items = [
            item for item in self.remote_items
            if show_hidden or not item.name.startswith(".")
        ]
        self.remote_pane.set_items(items, {select_path} if select_path else None)

    def _refresh_remote(
        self,
        path: str | None = None,
        select_path: str | None = None,
        silent: bool = False,
    ) -> None:
        if self.backend is None:
            return
        target = path if path is not None else self.remote_path
        backend = self.backend
        self.remote_pane.set_busy(True)
        self._run_remote(
            lambda: backend.listdir(target),
            on_ok=lambda entries: self._apply_remote_listing(target, entries, select_path),
            on_error=lambda exc: self._remote_listing_failed(exc, silent),
        )

    def _apply_remote_listing(self, target: str, entries, select_path: str | None) -> None:
        self.remote_path = target
        self.remote_items = entries
        self.remote_pane.set_path(target)
        self._render_remote(select_path)
        if self.profile is not None and self.profile.remote_path != target:
            self.profile.remote_path = target

    def _remote_listing_failed(self, exc, silent: bool = False) -> None:
        self.remote_pane.set_busy(False)
        self.remote_pane.set_path(self.remote_path)
        self.log(f"Cannot open {self.remote_path}: {exc}", "warning" if silent else "error")
        if silent:
            return
        messagebox.showerror("Cannot open folder", str(exc), parent=self)

    def _remote_navigate(self, raw: str) -> None:
        if self.backend is None:
            messagebox.showinfo(
                "Not connected",
                "Connect to a server before browsing remote folders.",
                parent=self,
            )
            self.remote_pane.set_path(self.remote_path)
            return
        text = (raw or "").strip()
        home = self.backend.home or "/"
        if text in ("", "~"):
            target = self.remote_path if not text else home
        elif text.startswith("~/"):
            target = posixpath.join(home, text[2:])
        elif text.startswith("/"):
            target = text
        else:
            target = posixpath.join(self.remote_path, text)
        target = normalise_remote(target)
        if target == self.remote_path:
            self._refresh_remote()
        else:
            self._refresh_remote(target)

    def _remote_new_folder(self) -> None:
        if self.backend is None:
            return
        name = simpledialog.askstring("New Remote Folder", "Folder name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or "/" in name or name in (".", ".."):
            messagebox.showerror("Invalid name", "Enter a single folder name.", parent=self)
            return
        target = join_remote(self.remote_path, name)
        backend = self.backend

        def work():
            backend.mkdir(target)

        self._run_remote(
            work,
            on_ok=lambda _result: self._after_remote_mkdir(target),
            on_error=lambda exc: messagebox.showerror(
                "Cannot create folder", str(exc), parent=self
            ),
        )

    def _after_remote_mkdir(self, target: str) -> None:
        self.log(f"Created remote folder {target}.")
        self._refresh_remote(select_path=target)

    def _remote_rename(self, item) -> None:
        if self.backend is None:
            return
        new_name = simpledialog.askstring(
            "Rename on Server", "New name:", initialvalue=item.name, parent=self
        )
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or "/" in new_name or new_name in (".", ".."):
            messagebox.showerror("Invalid name", "Enter a single name.", parent=self)
            return
        new_path = join_remote(parent_remote(item.path), new_name)
        if new_path == item.path:
            return
        backend = self.backend

        def work():
            backend.rename(item.path, new_path)

        self._run_remote(
            work,
            on_ok=lambda _r: self._after_remote_rename(new_name, new_path),
            on_error=lambda exc: messagebox.showerror("Rename failed", str(exc), parent=self),
        )

    def _after_remote_rename(self, new_name: str, new_path: str) -> None:
        self.log(f"Renamed remote item to {new_name}.")
        self._refresh_remote(select_path=new_path)

    def _remote_delete(self, items: list) -> None:
        if self.backend is None or not items:
            return
        names = ", ".join(item.name for item in items[:5])
        if len(items) > 5:
            names += f" and {len(items) - 5} more"
        if self.settings.get("confirm_delete", True) and not messagebox.askyesno(
            "Delete on server",
            f"Permanently delete {names} from the server?\n\n"
            "Folders are deleted with all their contents.",
            parent=self,
        ):
            return
        backend = self.backend
        targets = list(items)

        def work():
            for item in targets:
                if item.is_dir and not item.is_link:
                    backend.remove_tree(item.path)
                else:
                    backend.remove_file(item.path)

        self._run_remote(
            work,
            on_ok=lambda _r: self._after_remote_delete(len(targets)),
            on_error=lambda exc: self._remote_delete_failed(exc),
        )

    def _after_remote_delete(self, count: int) -> None:
        self.log(f"Deleted {count} item(s) from the server.")
        self._refresh_remote()

    def _remote_delete_failed(self, exc) -> None:
        self.log(f"Delete failed: {exc}", "error")
        messagebox.showerror("Delete failed", str(exc), parent=self)
        self._refresh_remote()

    def _remote_chmod(self, item) -> None:
        if self.backend is None:
            return
        mode = item.mode if item.mode is not None else 0o644
        dialog = PermissionsDialog(self, name=item.name, mode=mode)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        backend = self.backend
        new_mode = dialog.result

        def work():
            backend.chmod(item.path, new_mode)

        self._run_remote(
            work,
            on_ok=lambda _r: self._after_chmod(item, new_mode),
            on_error=lambda exc: messagebox.showerror(
                "Cannot change permissions", str(exc), parent=self
            ),
        )

    def _after_chmod(self, item, mode: int) -> None:
        self.log(f"Permissions for {item.name} set to {mode:03o}.")
        self._refresh_remote(select_path=item.path)

    # -- transfers --------------------------------------------------------
    def _create_transfer_backend(self):
        profile = self.profile
        if profile is None:
            raise BackendError("Not connected")
        backend = create_backend(profile, self.password, self.passphrase)
        backend.connect()
        return backend

    def _emit(self, event: str, payload) -> None:
        self._ui_queue.put((event, payload))

    def _upload_selection(self) -> None:
        self._upload_items(self.local_pane.selected_items())

    def _upload_items(self, items: list) -> None:
        if not items:
            return
        if self.backend is None:
            messagebox.showinfo(
                "Not connected", "Connect to a server before uploading files.", parent=self
            )
            return
        remote_names = {entry.name for entry in self.remote_items}
        conflicts = [item.name for item in items if item.name in remote_names]
        if conflicts and not self._confirm_overwrite(conflicts, "on the server"):
            return
        tasks = self.transfer_manager.add_upload(
            [item.path for item in items], self.remote_path
        )
        if not tasks:
            return
        self._pending_refresh.add("remote")
        self._was_pending = True
        self.log(
            f"Queued {len(tasks)} upload(s) to {self.remote_path}.", "info"
        )
        self.notebook.select(self.transfer_panel)

    def _download_selection(self) -> None:
        self._download_items(self.remote_pane.selected_items())

    def _download_items(self, items: list) -> None:
        if not items:
            return
        if self.backend is None:
            messagebox.showinfo(
                "Not connected", "Connect to a server before downloading files.", parent=self
            )
            return
        destination = self.local_pane.current_path
        conflicts = [
            item.name
            for item in items
            if os.path.exists(os.path.join(destination, item.name))
        ]
        if conflicts and not self._confirm_overwrite(conflicts, "on your Mac"):
            return
        tasks = self.transfer_manager.add_download(items, destination)
        if not tasks:
            return
        self._pending_refresh.add("local")
        self._was_pending = True
        self.log(f"Queued {len(tasks)} download(s) to {destination}.")
        self.notebook.select(self.transfer_panel)

    def _confirm_overwrite(self, names: list[str], where: str) -> bool:
        if not self.settings.get("confirm_overwrite", True):
            return True
        preview = ", ".join(names[:4])
        if len(names) > 4:
            preview += f" and {len(names) - 4} more"
        return messagebox.askyesno(
            "Replace existing items?",
            f"{preview} already exist{'' if len(names) != 1 else 's'} {where}.\n\n"
            "Replace or merge?",
            parent=self,
        )

    def _cancel_task(self, task) -> None:
        self.transfer_manager.cancel(task.task_id)

    def _retry_task(self, task) -> None:
        self.transfer_manager.retry(task.task_id)
        self._was_pending = True
        self._pending_refresh.add(
            "remote" if task.direction == "upload" else "local"
        )

    def _remove_task(self, task) -> None:
        self.transfer_manager.remove(task.task_id)

    def _clear_finished(self) -> None:
        self.transfer_manager.clear_finished()

    def _cancel_all(self) -> None:
        for task in self.transfer_manager.snapshot():
            if task.status in ("queued", "running"):
                self.transfer_manager.cancel(task.task_id)

    def _reveal_task(self, task) -> None:
        if task.direction == "upload":
            _open_in_finder(task.source)
            return
        if task.kind == "tree":
            target = os.path.join(task.destination, task.display_name)
        else:
            target = os.path.join(task.destination, os.path.basename(task.source))
        _open_in_finder(target, reveal=os.path.isfile(target))

    def _show_task_error(self, task) -> None:
        messagebox.showerror(
            "Transfer failed",
            f"{task.display_name}\n\n{task.error or 'Unknown error'}",
            parent=self,
        )

    # -- async helpers ----------------------------------------------------
    def _browser_loop(self) -> None:
        """Serialise remote browsing on one daemon thread."""
        while True:
            item = self._browser_queue.get()
            if item is None:
                return
            token, work, on_ok, on_error = item
            try:
                result = work()
            except Exception as exc:
                self._ui_queue.put(("remote_error", token, exc, on_error))
            else:
                self._ui_queue.put(("remote_ok", token, result, on_ok))

    def _run_remote(
        self,
        work: Callable,
        on_ok: Callable | None = None,
        on_error: Callable | None = None,
    ) -> None:
        self._busy_ops += 1
        self._update_activity()
        self._browser_queue.put((self.session, work, on_ok, on_error))

    def _pump(self) -> None:
        try:
            while True:
                event = self._ui_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self._update_queue_status()
        self._maybe_finish_batch()
        self.after(80, self._pump)

    def _handle_event(self, event) -> None:
        kind = event[0]
        try:
            if kind == "changed":
                self.transfer_panel.update_tasks(self.transfer_manager.snapshot())
            elif kind == "log":
                level, message = event[1]
                self.log(message, level)
            elif kind == "connect_ok":
                _, token, backend, password, passphrase, start, entries = event
                self._handle_connect_ok(
                    token, backend, password, passphrase, start, entries
                )
            elif kind == "connect_error":
                _, token, exc, profile, from_dialog = event
                self._handle_connect_error(token, exc, profile, from_dialog)
            elif kind == "remote_ok":
                _, token, result, callback = event
                self._busy_ops = max(0, self._busy_ops - 1)
                if token == self.session and callback is not None:
                    callback(result)
            elif kind == "remote_error":
                _, token, exc, callback = event
                self._busy_ops = max(0, self._busy_ops - 1)
                if token == self.session:
                    if callback is not None:
                        callback(exc)
                    else:
                        self.log(f"Operation failed: {exc}", "error")
                        messagebox.showerror("Operation failed", str(exc), parent=self)
        except Exception as exc:  # never let the UI loop die
            self.log(f"Unexpected error: {type(exc).__name__}: {exc}", "error")
        finally:
            self._update_activity()

    def _maybe_finish_batch(self) -> None:
        pending = self.transfer_manager.has_pending()
        if self._was_pending and not pending:
            if "local" in self._pending_refresh:
                self._refresh_local(silent=True)
            if "remote" in self._pending_refresh and self.backend is not None:
                self._refresh_remote(silent=True)
            self._pending_refresh.clear()
            tasks = self.transfer_manager.snapshot()
            failed = sum(1 for task in tasks if task.status == "failed")
            if failed:
                self.log(f"Transfer queue finished with {failed} failed item(s).", "warning")
            else:
                self.log("Transfer queue finished.", "success")
        self._was_pending = pending

    def _update_activity(self) -> None:
        if self._connecting:
            text = "Connecting…"
        elif self._busy_ops > 0:
            text = "Working…"
        elif self.transfer_manager.has_pending():
            text = "Transferring…"
        else:
            text = "Ready"
        self.activity_label.configure(text=text)

    def _update_queue_status(self) -> None:
        tasks = self.transfer_manager.snapshot()
        active = [task for task in tasks if task.is_active]
        if active:
            total_speed = sum(task.speed for task in active)
            text = f"{len(active)} active"
            if total_speed:
                text += f" · {format_bytes(total_speed)}/s"
            self.queue_label.configure(text=text)
            return
        failed = sum(1 for task in tasks if task.status == "failed")
        if failed:
            self.queue_label.configure(text=f"{failed} failed")
        elif tasks:
            self.queue_label.configure(text=f"{len(tasks)} completed")
        else:
            self.queue_label.configure(text="")

    # -- logging / misc ---------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self.log_panel.append(message, level)

    def _log_welcome(self) -> None:
        self.log(f"FTP Client {__version__} ready.")
        if self.profiles:
            self.log(
                f"{len(self.profiles)} saved connection(s). Select one and press Connect."
            )
        else:
            self.log("No saved connections yet — click New… to create one.")

    def _refresh_both(self) -> None:
        self._refresh_local()
        if self.backend is not None:
            self._refresh_remote()

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About FTP Client",
            f"FTP Client {__version__}\n\n"
            "FTP, FTPS and SFTP file transfers for macOS.\n\n"
            "• Dual-pane local and remote browsing\n"
            "• Recursive folder upload and download\n"
            "• Queue with progress, speed and cancellation\n"
            "• Passwords stored in the macOS Keychain",
            parent=self,
        )

    def _save_settings(self) -> None:
        try:
            self.config_store.save_settings(self.settings)
        except OSError as exc:
            self.log(f"Could not save settings: {exc}", "warning")

    def _save_state(self) -> None:
        self.settings["window_geometry"] = self.geometry()
        self.settings["show_hidden_local"] = self.local_pane.hidden_var.get()
        self.settings["show_hidden_remote"] = self.remote_pane.hidden_var.get()
        try:
            self.settings["sash_local"] = self.local_pane.master.sashpos(0)
            self.settings["sash_vertical"] = self.vertical_panes.sashpos(0)
        except tk.TclError:
            pass
        if self.local_pane.current_path:
            self.settings["local_path"] = self.local_pane.current_path
        if self.profile is not None:
            self.settings["last_profile"] = self.profile.name
            if self.backend is not None:
                self.profile.remote_path = self.remote_path
                index = next(
                    (i for i, item in enumerate(self.profiles) if item.name == self.profile.name),
                    None,
                )
                if index is not None:
                    self.profiles[index] = self.profile
        try:
            self.config_store.save_profiles(self.profiles)
        except OSError:
            pass
        self._save_settings()

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.transfer_manager.has_pending() and not messagebox.askyesno(
            "Quit FTP Client",
            "Transfers are still running. Quit and cancel them?",
            parent=self,
        ):
            return
        self._closing = True
        self._save_state()
        try:
            self.transfer_manager.stop()
        except Exception:
            pass
        backend = self.backend
        self.backend = None
        if backend is not None:
            threading.Thread(target=self._safe_close, args=(backend,), daemon=True).start()
        self._browser_queue.put(None)
        self.destroy()

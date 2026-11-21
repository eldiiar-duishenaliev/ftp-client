"""Modal dialogs: connection editor and permissions editor."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import keychain
from ..backends import create_backend
from ..models import DEFAULT_PORTS, Profile

PROTOCOL_LABELS = {
    "FTP": "ftp",
    "FTPS (FTP over TLS)": "ftps",
    "SFTP (SSH)": "sftp",
}
LABEL_BY_PROTOCOL = {value: key for key, value in PROTOCOL_LABELS.items()}
TLS_LABELS = {
    "Explicit (AUTH TLS)": "explicit",
    "Implicit (TLS from the start, usually port 990)": "implicit",
}
TLS_BY_VALUE = {value: key for key, value in TLS_LABELS.items()}


class ConnectionDialog(tk.Toplevel):
    def __init__(
        self,
        parent,
        *,
        profile: Profile | None = None,
        password: str = "",
        passphrase: str = "",
        has_saved_password: bool = False,
        initial_error: str = "",
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.existing = profile is not None
        self.result: tuple[Profile, str, str, bool] | None = None
        self.deleted = False
        self._testing = False
        self._test_queue: queue.Queue = queue.Queue()

        self.title("Edit Connection" if profile else "New Connection")
        self.resizable(False, False)
        self.transient(parent)
        self.configure(padx=0, pady=0)

        source = profile or Profile(name="", host="")
        self.protocol_var = tk.StringVar(value=LABEL_BY_PROTOCOL[source.protocol])
        self.name_var = tk.StringVar(value=source.name)
        self.host_var = tk.StringVar(value=source.host)
        self.port_var = tk.StringVar(value=str(source.port))
        self.user_var = tk.StringVar(value=source.username)
        self.password_var = tk.StringVar(value=password)
        self.remote_var = tk.StringVar(value=source.remote_path or "")
        self.tls_var = tk.StringVar(value=TLS_BY_VALUE.get(source.tls_mode, "Explicit (AUTH TLS)"))
        self.passive_var = tk.BooleanVar(value=source.passive)
        self.verify_cert_var = tk.BooleanVar(value=source.verify_cert)
        self.key_var = tk.StringVar(value=source.key_path)
        self.passphrase_var = tk.StringVar(value=passphrase)
        self.verify_host_var = tk.BooleanVar(value=source.verify_host_key)
        self.remember_var = tk.BooleanVar(value=keychain.is_available())
        self.show_password_var = tk.BooleanVar(value=False)

        self._saved_password = has_saved_password

        self._build(initial_error)
        self._update_protocol(initial=False)
        self._center(parent)
        self.grab_set()
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    # -- layout -----------------------------------------------------------
    def _build(self, initial_error: str) -> None:
        container = ttk.Frame(self, padding=18)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        ttk.Label(
            container,
            text="Edit Connection" if self.existing else "New Connection",
            style="Title.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            container,
            text="Credentials are saved in the macOS Keychain, never in plain text.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

        row = 2
        row = self._add_field(container, row, "Name", self.name_var)
        row = self._add_protocol(container, row)
        row = self._add_field(container, row, "Host", self.host_var, width=28)
        row = self._add_field(container, row, "Port", self.port_var, width=8)
        row = self._add_field(container, row, "Username", self.user_var)
        row = self._add_password(container, row)
        row = self._add_field(container, row, "Start folder", self.remote_var, hint="optional")

        self.tls_frame = ttk.LabelFrame(container, text="TLS", padding=10)
        self.tls_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.tls_frame.columnconfigure(1, weight=1)
        ttk.Label(self.tls_frame, text="Mode").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.tls_combo = ttk.Combobox(
            self.tls_frame,
            textvariable=self.tls_var,
            values=list(TLS_LABELS),
            state="readonly",
            width=42,
        )
        self.tls_combo.grid(row=0, column=1, sticky="w")
        self.tls_combo.bind("<<ComboboxSelected>>", self._tls_mode_changed)
        ttk.Checkbutton(
            self.tls_frame,
            text="Verify the server certificate (recommended)",
            variable=self.verify_cert_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            self.tls_frame,
            text="Use passive mode (PASV)",
            variable=self.passive_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.tls_frame.grid_remove()
        row += 1

        self.ftp_frame = ttk.LabelFrame(container, text="FTP", padding=10)
        self.ftp_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Checkbutton(
            self.ftp_frame,
            text="Use passive mode (PASV) — recommended",
            variable=self.passive_var,
        ).pack(anchor="w")
        self.ftp_frame.grid_remove()
        row += 1

        self.sftp_frame = ttk.LabelFrame(container, text="SFTP", padding=10)
        self.sftp_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.sftp_frame.columnconfigure(1, weight=1)
        ttk.Label(self.sftp_frame, text="Private key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        key_row = ttk.Frame(self.sftp_frame)
        key_row.grid(row=0, column=1, sticky="ew")
        key_row.columnconfigure(0, weight=1)
        ttk.Entry(key_row, textvariable=self.key_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(key_row, text="Choose…", command=self._choose_key).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(
            self.sftp_frame,
            text="Optional: leave empty to use a password.",
            style="Muted.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(self.sftp_frame, text="Key passphrase").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.passphrase_entry = ttk.Entry(self.sftp_frame, textvariable=self.passphrase_var, show="•")
        self.passphrase_entry.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            self.sftp_frame,
            text="Verify the host key against known_hosts",
            variable=self.verify_host_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.sftp_frame.grid_remove()
        row += 1

        self.remember_check = ttk.Checkbutton(
            container,
            text="Remember password in the macOS Keychain",
            variable=self.remember_var,
        )
        self.remember_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        if not keychain.is_available():
            self.remember_var.set(False)
            self.remember_check.state(["disabled"])
        self.remember_hint = ttk.Label(
            container, text="", style="Muted.TLabel"
        )
        self.remember_hint.grid(row=row + 1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        row += 2

        self.error_label = tk.Label(
            container,
            text=initial_error,
            fg="#c0392b",
            wraplength=460,
            justify="left",
            anchor="w",
        )
        self.error_label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        row += 1

        self.test_label = ttk.Label(container, text="", style="Muted.TLabel")
        self.test_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1

        buttons = ttk.Frame(container)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        buttons.columnconfigure(0, weight=1)

        left = ttk.Frame(buttons)
        left.grid(row=0, column=0, sticky="w")
        ttk.Button(left, text="Test Connection", command=self._test_connection).pack(side="left")
        self.delete_button = ttk.Button(left, text="Delete Profile", command=self._delete_profile)
        if self.existing:
            self.delete_button.pack(side="left", padx=(6, 0))

        right = ttk.Frame(buttons)
        right.grid(row=0, column=1, sticky="e")
        ttk.Button(right, text="Cancel", command=self._cancel).pack(side="left")
        self.connect_button = ttk.Button(
            right, text="Save & Connect", style="Accent.TButton", command=self._accept
        )
        self.connect_button.pack(side="left", padx=(8, 0))

        self.bind("<Return>", lambda _e: self._accept())

    def _add_field(self, parent, row: int, label: str, variable, width: int | None = None, hint: str = "") -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        entry = ttk.Entry(parent, textvariable=variable, width=width or 32)
        entry.grid(row=row, column=1, sticky="w", pady=(6, 0))
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
                row=row, column=1, sticky="e", pady=(6, 0)
            )
        return row + 1

    def _add_protocol(self, parent, row: int) -> int:
        ttk.Label(parent, text="Protocol").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        combo = ttk.Combobox(
            parent,
            textvariable=self.protocol_var,
            values=list(PROTOCOL_LABELS),
            state="readonly",
            width=30,
        )
        combo.grid(row=row, column=1, sticky="w", pady=(6, 0))
        combo.bind("<<ComboboxSelected>>", self._update_protocol)
        return row + 1

    def _add_password(self, parent, row: int) -> int:
        ttk.Label(parent, text="Password").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        box = ttk.Frame(parent)
        box.grid(row=row, column=1, sticky="w", pady=(6, 0))
        self.password_entry = ttk.Entry(box, textvariable=self.password_var, show="•", width=26)
        self.password_entry.pack(side="left")
        ttk.Checkbutton(
            box,
            text="Show",
            variable=self.show_password_var,
            command=self._toggle_password,
        ).pack(side="left", padx=(8, 0))
        return row + 1

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "•")

    def _tls_mode_changed(self, _event=None) -> None:
        mode = TLS_BY_VALUE.get(self.tls_var.get(), "explicit")
        port = self.port_var.get().strip()
        if mode == "implicit" and port == "21":
            self.port_var.set("990")
        elif mode == "explicit" and port == "990":
            self.port_var.set("21")

    def _choose_key(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose a private key",
            initialdir=os.path.expanduser("~/.ssh"),
            filetypes=[("All files", "*")],
        )
        if path:
            self.key_var.set(path)

    def _update_protocol(self, _event=None, initial: bool = True) -> None:
        protocol = PROTOCOL_LABELS.get(self.protocol_var.get(), "ftp")
        defaults = [str(DEFAULT_PORTS[p]) for p in ("ftp", "ftps", "sftp")]
        current_port = self.port_var.get().strip()
        if current_port in defaults or not current_port:
            self.port_var.set(str(DEFAULT_PORTS[protocol]))

        self.tls_frame.grid_remove()
        self.ftp_frame.grid_remove()
        self.sftp_frame.grid_remove()
        if protocol == "ftps":
            self.tls_frame.grid()
        elif protocol == "sftp":
            self.sftp_frame.grid()
        else:
            self.ftp_frame.grid()

        if self.existing and self._saved_password:
            self.remember_hint.configure(
                text="A saved Keychain password is used when the field is left empty."
            )
        else:
            self.remember_hint.configure(text="")

    # -- actions ----------------------------------------------------------
    def _collect(self) -> Profile | None:
        host = self.host_var.get().strip()
        if not host:
            self._show_error("Enter a server host name or IP address.")
            return None
        protocol = PROTOCOL_LABELS.get(self.protocol_var.get(), "ftp")
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            self._show_error("The port must be a number.")
            return None
        if not 1 <= port <= 65535:
            self._show_error("The port must be between 1 and 65535.")
            return None
        username = self.user_var.get().strip() or "anonymous"
        key_path = self.key_var.get().strip()
        if protocol == "sftp" and key_path and not os.path.isfile(os.path.expanduser(key_path)):
            self._show_error("The selected private key file does not exist.")
            return None
        name = self.name_var.get().strip() or f"{username}@{host}"
        return Profile(
            name=name,
            protocol=protocol,
            host=host,
            port=port,
            username=username,
            tls_mode=TLS_BY_VALUE.get(self.tls_var.get(), "explicit"),
            verify_cert=self.verify_cert_var.get(),
            passive=self.passive_var.get(),
            key_path=os.path.expanduser(key_path) if key_path else "",
            remote_path=self.remote_var.get().strip() or "",
            verify_host_key=self.verify_host_var.get(),
        )

    def _accept(self) -> None:
        profile = self._collect()
        if profile is None:
            return
        self.result = (
            profile,
            self.password_var.get(),
            self.passphrase_var.get(),
            bool(self.remember_var.get()),
        )
        self._close()

    def _test_connection(self) -> None:
        if self._testing:
            return
        profile = self._collect()
        if profile is None:
            return
        self._testing = True
        self.connect_button.state(["disabled"])
        self.test_label.configure(text="Connecting…")

        def work() -> None:
            backend = None
            try:
                backend = create_backend(profile, self.password_var.get(), self.passphrase_var.get())
                backend.connect()
                entries = backend.listdir(backend.home or "/")
                self._test_queue.put(("ok", f"Connected — {len(entries)} items in {backend.home or '/'}"))
            except Exception as exc:  # show the real reason
                self._test_queue.put(("error", str(exc)))
            finally:
                if backend is not None:
                    backend.close()

        threading.Thread(target=work, name="test-connection", daemon=True).start()
        self.after(120, self._poll_test)

    def _poll_test(self) -> None:
        try:
            kind, message = self._test_queue.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(120, self._poll_test)
            return
        self._testing = False
        if not self.winfo_exists():
            return
        self.connect_button.state(["!disabled"])
        if kind == "ok":
            self.test_label.configure(text=message, foreground="#1f8b4c")
        else:
            self.test_label.configure(text=message, foreground="#c0392b")

    def _delete_profile(self) -> None:
        if not messagebox.askyesno(
            "Delete connection",
            "Remove this saved connection? The password will also be removed from the Keychain.",
            parent=self,
        ):
            return
        self.deleted = True
        self.result = None
        self._close()

    def _show_error(self, message: str) -> None:
        self.error_label.configure(text=message)

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _center(self, parent) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
        except tk.TclError:
            return
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 3)
        self.geometry(f"+{x}+{y}")


class PermissionsDialog(tk.Toplevel):
    def __init__(self, parent, *, name: str, mode: int) -> None:
        super().__init__(parent)
        self.result: int | None = None
        self.title("Permissions")
        self.resizable(False, False)
        self.transient(parent)
        self._checks: list[tk.BooleanVar] = []

        container = ttk.Frame(self, padding=18)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Permissions", style="Title.TLabel").pack(anchor="w")
        ttk.Label(container, text=name, style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        grid = ttk.Frame(container)
        grid.pack(anchor="w")
        ttk.Label(grid, text="").grid(row=0, column=0)
        for column, label in enumerate(("Read", "Write", "Execute"), start=1):
            ttk.Label(grid, text=label).grid(row=0, column=column, padx=8, pady=(0, 4))
        for row, label in enumerate(("Owner", "Group", "Others"), start=1):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            for column in range(3):
                shift = 6 - 3 * (row - 1) - column
                var = tk.BooleanVar(value=bool(mode & (1 << shift)))
                check = ttk.Checkbutton(
                    grid,
                    variable=var,
                    command=self._sync_from_checks,
                )
                check.grid(row=row, column=column + 1, padx=8, pady=3)
                self._checks.append(var)

        numeric = ttk.Frame(container)
        numeric.pack(fill="x", pady=(14, 0))
        ttk.Label(numeric, text="Numeric mode").pack(side="left")
        self.mode_var = tk.StringVar(value=f"{mode:03o}")
        entry = ttk.Entry(numeric, textvariable=self.mode_var, width=6)
        entry.pack(side="left", padx=(8, 0))
        entry.bind("<KeyRelease>", self._sync_from_text)
        ttk.Label(numeric, text="e.g. 644, 755", style="Muted.TLabel").pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(
            buttons, text="Apply", style="Accent.TButton", command=self._apply
        ).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._apply())

        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            self.geometry(
                f"+{px + max(0, (pw - self.winfo_width()) // 2)}+{py + max(0, (ph - self.winfo_height()) // 3)}"
            )
        except tk.TclError:
            pass
        self.grab_set()

    def _sync_from_checks(self) -> None:
        mode = 0
        for index, var in enumerate(self._checks):
            if var.get():
                mode |= 1 << (8 - index)
        self.mode_var.set(f"{mode:03o}")

    def _sync_from_text(self, _event=None) -> None:
        text = self.mode_var.get().strip()
        if not text or any(c not in "01234567" for c in text) or len(text) > 3:
            return
        mode = int(text, 8)
        for index, var in enumerate(self._checks):
            var.set(bool(mode & (1 << (8 - index))))

    def _apply(self) -> None:
        text = self.mode_var.get().strip()
        try:
            mode = int(text, 8)
        except ValueError:
            messagebox.showerror("Invalid mode", "Enter an octal mode such as 644 or 755.", parent=self)
            return
        if not 0 <= mode <= 0o777:
            messagebox.showerror("Invalid mode", "Permissions must be between 000 and 777.", parent=self)
            return
        self.result = mode
        self._cancel()

    def _cancel(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

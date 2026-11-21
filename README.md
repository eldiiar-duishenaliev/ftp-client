# FTP Client for macOS

A dual-pane desktop FTP client with FTP, FTPS and SFTP support, built with Python
and Tkinter. It looks and behaves like a native Mac utility: two file browsers,
a background transfer queue, saved connections, and passwords kept in the macOS
Keychain.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Connection [ My server ▾ ] New… Edit… Connect Disconnect   ● Connected   │
├───────────────────────────────┬──────────────────────────────────────────┤
│ LOCAL FILES                   │ SERVER FILES                            │
│ ~/Projects                    │ /var/www                                │
│ ▸ build                       │ ▸ assets                                │
│   index.html                  │   index.html                            │
│ [New Folder] [Rename] [Delete]│ [New Folder] [Rename] [Delete] [Download]│
├───────────────────────────────┴──────────────────────────────────────────┤
│ Transfers │ Log                                                          │
│ ↑ index.html   24 KB  ████████████ 100%   Completed                       │
└──────────────────────────────────────────────────────────────────────────┘
```

## Features

- **Three protocols** — plain FTP, explicit FTPS (`AUTH TLS`), implicit FTPS,
  and SFTP over SSH with password or private-key authentication.
- **Dual-pane browsing** — local files on the left, remote files on the right.
  Enter any path, use `~`, navigate with the up button, sort by any column.
- **Recursive folder transfers** — select folders and upload or download them
  with their whole tree, not just flat files.
- **Transfer queue** — transfers run in the background while you keep browsing.
  Live progress bars, speed, ETA, cancel, retry, error details, reveal in Finder.
- **Full file management** — create folders, rename, delete recursively, and
  change remote permissions (SFTP, or FTP servers that support `SITE CHMOD`).
  Local files can be revealed in Finder.
- **Saved connections** — connection profiles with optional start folder,
  passive mode, TLS verification, and SSH host-key checking.
- **Keychain security** — passwords are stored in the macOS login Keychain, never
  in the application's JSON files. Turning off “Remember password” removes them.
- **Small UX details** — hidden-file toggles, context menus, double-click to
  transfer a file, keyboard shortcuts, native macOS light/dark palettes, and a
  log of everything the client does.

## Quick start

Requires **macOS 12 or newer** and **Python 3.11+** with Tkinter. The universal
installer from [python.org](https://www.python.org/downloads/macos/) includes
Tkinter; Homebrew Python needs the `python-tk` formula as well.

```bash
bash run.sh
```

The first run creates `.venv/`, installs Paramiko and keyring, and opens the
window. You can also set it up manually:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m ftpclient
```

### Connect

1. Click **New…**.
2. Pick FTP, FTPS or SFTP, then fill in host, port, username and password.
3. For SFTP you can choose a private key (`~/.ssh/id_ed25519`, `id_rsa`, …)
   instead of a password; the passphrase field is only used to unlock the key.
4. Optionally set a **Start folder** (for example `/var/www`) so the client
   opens there after connecting.
5. **Test Connection** checks the settings without saving; **Save & Connect**
   stores the profile and opens the session.

Passwords are saved in the Keychain only when **Remember password** is checked.
A profile itself contains no secret:

```
~/Library/Application Support/FTP Client/profiles.json
~/Library/Application Support/FTP Client/settings.json
```

### Try it without a real server

```bash
.venv/bin/python -m pip install pyftpdlib
.venv/bin/python scripts/demo_server.py
```

Connect to `127.0.0.1` port `2121` with user `demo` and password `demo`. The demo
server serves a temporary folder with a few sample files and accepts uploads,
renames and deletes, so every button in the client can be tried safely.

### Transfer

- Double-click a local file to upload it, or a remote file to download it.
- Select several files or a folder and press **Upload →** / **← Download**.
  Folders are transferred recursively.
- Use **New Folder**, **Rename**, **Delete**, and **Permissions…** from the
  toolbar or the right-click menu.
- Watch the **Transfers** tab; the **Log** tab records connections and errors.
  Completed batches refresh the panes automatically.

### Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Cmd+N` | New connection |
| `Cmd+E` | Edit the selected connection |
| `Cmd+D` | Disconnect |
| `Cmd+R` / `F5` | Refresh both panes |
| `Cmd+U` | Upload the local selection |
| `Cmd+Shift+U` | Download the remote selection |
| `Cmd+Shift+N` | New remote folder |
| `Cmd+W` | Close the window |
| `Return` | Open a folder / transfer the selected file |
| `Backspace` | Go to the parent folder |
| `Delete` | Delete the selection |
| `Cmd+A` | Select all in the focused list |
| `Esc` | Cancel path editing |

## Protocol notes

### FTP

Classic FTP sends credentials and data in clear text. Use it only on trusted
networks. Passive mode is enabled by default; turn it off for servers that
require active mode.

### FTPS

Explicit FTPS connects normally, then upgrades with `AUTH TLS`; implicit FTPS
starts TLS immediately (usually port 990). Certificate verification is on by
default. For a server with a self-signed certificate, either install its CA on
the Mac or clear **Verify the server certificate** in the connection settings.

### SFTP

SFTP rides over SSH on port 22. Password auth and RSA/ECDSA/Ed25519 private keys
are supported. If **Verify the host key against known_hosts** is enabled, the
server must already be present in `~/.ssh/known_hosts`; otherwise enable it
after connecting once with verification off. Note that SFTP is a different
protocol from FTPS and usually speaks on port 22.

## Development

```bash
bash test.sh
```

The test suite starts its own FTP, FTPS and SFTP servers on `127.0.0.1`; no
network access or real server is involved. It covers:

- FTP and FTPS uploads, downloads, listings (MLSD and LIST fallback), Unicode
  names, permissions and errors against pyftpdlib.
- SFTP operations against a local Paramiko server.
- Recursive folder transfers, progress reporting, queue cancellation and retry.
- End-to-end window flows: connecting, uploading, downloading, folder
  operations, hidden files and disconnecting.

Python 3.11–3.13 should work; the development environment used Python 3.13 and
Tk 8.6.

### Build a double-clickable app

```bash
bash scripts/build_app.sh
open "dist/FTP Client.app"
```

The generated bundle is a thin launcher that points at this checkout and its
virtualenv, so it is meant for this Mac. To distribute the client to another
machine, package it with PyInstaller and sign/notarize the result.

### Project layout

```
ftpclient/
├── __main__.py            command-line entry point
├── backends/              FTP/FTPS and SFTP implementations
├── ui/                    Tkinter window, panes, dialogs, theme
├── config.py              profiles and UI settings
├── keychain.py            macOS Keychain integration
├── models.py              profiles, remote entries, transfer tasks
└── transfer.py            background queue, recursion, progress
scripts/
├── build_app.sh           .app bundle builder
└── make_icon.py           generated app icon
tests/                     local servers and integration tests
```

## Limitations

- One connection per window. Open a second copy for a second server.
- Interrupted transfers restart on retry; byte-range resume is not implemented.
- Remote files cannot be edited in place — download, edit, then upload.
- FTP permission changes depend on the server supporting `SITE CHMOD`.
- Drag-and-drop between panes is not available (use the buttons or the context
  menu).

## License

MIT — see [LICENSE](LICENSE).

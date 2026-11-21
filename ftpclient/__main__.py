"""Launch the desktop client with ``python -m ftpclient``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ftpclient",
        description="FTP, FTPS and SFTP desktop client for macOS.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"FTP Client {__version__}",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Use an alternative folder for profiles and settings.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    from .config import Config
    from .ui.app import FTPClientApp

    config = Config(args.config_dir) if args.config_dir else None
    app = FTPClientApp(config)
    if args.selftest:
        app.update_idletasks()
        app.update()
        app.after(400, app.destroy)
        app.mainloop()
        return 0
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())

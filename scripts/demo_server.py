#!/usr/bin/env python3
"""Run a small local FTP server for trying the client without a real host.

    .venv/bin/pip install pyftpdlib
    .venv/bin/python scripts/demo_server.py

Then connect the app to 127.0.0.1:2121 with demo / demo.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile


def _make_demo_tree() -> str:
    root = tempfile.mkdtemp(prefix="ftp-client-demo-")
    os.makedirs(os.path.join(root, "documents", "reports"))
    os.makedirs(os.path.join(root, "images"))
    os.makedirs(os.path.join(root, "incoming"))
    with open(os.path.join(root, "README.txt"), "w", encoding="utf-8") as handle:
        handle.write("This is a demo FTP server for the macOS FTP client.\n")
    with open(os.path.join(root, "documents", "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("Upload, download, rename, delete — try it all.\n" * 50)
    with open(os.path.join(root, "documents", "reports", "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write("Quarterly summary\n" * 200)
    with open(os.path.join(root, "images", "placeholder.txt"), "w", encoding="utf-8") as handle:
        handle.write("Put some images here.\n")
    return root


def main() -> int:
    try:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
    except ImportError:
        print(
            "pyftpdlib is required for the demo server:\n"
            "    .venv/bin/python -m pip install pyftpdlib",
            file=sys.stderr,
        )
        return 1

    logging.getLogger("pyftpdlib").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="demo")
    parser.add_argument(
        "--directory",
        default=None,
        help="Serve this folder instead of a temporary demo tree.",
    )
    args = parser.parse_args()

    root = os.path.abspath(os.path.expanduser(args.directory)) if args.directory else _make_demo_tree()
    if not os.path.isdir(root):
        print(f"Not a folder: {root}", file=sys.stderr)
        return 1

    authorizer = DummyAuthorizer()
    authorizer.add_user(args.user, args.password, root, perm="elradfmwMT")

    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    Handler.banner = "FTP Client demo server"
    server = FTPServer(("127.0.0.1", args.port), Handler)
    port = server.socket.getsockname()[1]

    print("Demo FTP server is running.")
    print(f"  Host:    127.0.0.1")
    print(f"  Port:    {port}")
    print(f"  User:    {args.user}")
    print(f"  Password:{args.password}")
    print(f"  Folder:  {root}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())

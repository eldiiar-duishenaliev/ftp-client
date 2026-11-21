#!/bin/bash
# Run the test suite (local FTP/FTPS/SFTP servers are started automatically).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
fi

if ! .venv/bin/python -c "import paramiko, pyftpdlib, OpenSSL, PIL" >/dev/null 2>&1; then
    echo "Installing test dependencies…"
    .venv/bin/python -m pip install -r requirements-dev.txt
fi

exec .venv/bin/python -m unittest discover -s tests -t . -v

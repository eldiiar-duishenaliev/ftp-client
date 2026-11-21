#!/bin/bash
# Launch the GUI, creating the virtualenv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment…"
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
fi

if ! .venv/bin/python -c "import paramiko, keyring" >/dev/null 2>&1; then
    echo "Installing dependencies…"
    .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python -m ftpclient "$@"

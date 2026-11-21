"""Password storage in the macOS login Keychain.

Prefers the ``keyring`` package (which talks to Security.framework directly, so
the password never appears in a process argument list) and falls back to the
``security`` command line tool when the package is unavailable.

Passwords are never written to the profiles file.
"""

from __future__ import annotations

import shutil
import subprocess

SERVICE = "FTPClient"

try:  # pragma: no cover - exercised on the user's Mac, not in CI
    import keyring

    _HAS_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None
    _HAS_KEYRING = False


def _security() -> str | None:
    return shutil.which("security")


def is_available() -> bool:
    return _HAS_KEYRING or _security() is not None


def _run_security(args: list[str]) -> subprocess.CompletedProcess:
    binary = _security()
    if binary is None:
        raise RuntimeError("macOS Keychain tool is not available")
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def store_password(account: str, password: str) -> bool:
    """Add or replace the Keychain item for ``account``."""
    if not password:
        return False
    if _HAS_KEYRING:
        try:
            keyring.set_password(SERVICE, account, password)
            return True
        except Exception:
            pass
    result = _run_security(
        ["add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", password]
    )
    return result.returncode == 0


def load_password(account: str) -> str | None:
    if _HAS_KEYRING:
        try:
            found = keyring.get_password(SERVICE, account)
            if found is not None:
                return found
        except Exception:
            pass
    result = _run_security(["find-generic-password", "-s", SERVICE, "-a", account, "-w"])
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def delete_password(account: str) -> bool:
    deleted = False
    if _HAS_KEYRING:
        try:
            keyring.delete_password(SERVICE, account)
            deleted = True
        except Exception:
            pass
    result = _run_security(["delete-generic-password", "-s", SERVICE, "-a", account])
    return deleted or result.returncode == 0

"""Backend factory shared by the browser and the transfer engine."""

from __future__ import annotations

from ..models import Profile
from .base import AuthError, BackendError, PathError, RemoteBackend, TransferAborted
from .ftp_backend import FTPBackend
from .sftp_backend import SFTPBackend

__all__ = [
    "AuthError",
    "BackendError",
    "PathError",
    "RemoteBackend",
    "TransferAborted",
    "create_backend",
]


def create_backend(profile: Profile, password: str = "", passphrase: str = "") -> RemoteBackend:
    if profile.protocol == "sftp":
        return SFTPBackend(profile, password, passphrase)
    if profile.protocol in ("ftp", "ftps"):
        return FTPBackend(profile, password, passphrase)
    raise ValueError(f"Unsupported protocol: {profile.protocol}")

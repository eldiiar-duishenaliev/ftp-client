"""Shared data structures used by the backends and the UI."""

from __future__ import annotations

import posixpath
from dataclasses import asdict, dataclass, field, fields
from pathlib import PurePosixPath

PROTOCOLS = ("ftp", "ftps", "sftp")
DEFAULT_PORTS = {"ftp": 21, "ftps": 21, "sftp": 22}


def clamp_mode(mode: int) -> int:
    """Normalise a chmod value to the lowest three octal digits."""
    return mode & 0o777


@dataclass
class RemoteEntry:
    """One item in a remote directory listing."""

    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mtime: float | None = None
    mode: int | None = None
    is_link: bool = False

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix.lower()

    @property
    def mode_text(self) -> str:
        if self.mode is None:
            return ""
        bits = []
        for shift in (6, 3, 0):
            chunk = (self.mode >> shift) & 0o7
            bits.append(
                ("r" if chunk & 4 else "-")
                + ("w" if chunk & 2 else "-")
                + ("x" if chunk & 1 else "-")
            )
        return "".join(bits)


@dataclass
class Profile:
    """A saved connection. The password is never written to disk."""

    name: str
    protocol: str = "ftp"
    host: str = ""
    port: int = 21
    username: str = "anonymous"
    tls_mode: str = "explicit"
    verify_cert: bool = True
    passive: bool = True
    key_path: str = ""
    remote_path: str = "/"
    local_path: str = ""
    verify_host_key: bool = False

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {self.protocol}")
        if not self.port:
            self.port = DEFAULT_PORTS[self.protocol]

    @property
    def keychain_account(self) -> str:
        return f"{self.protocol}://{self.username}@{self.host}:{self.port}"

    @property
    def display_name(self) -> str:
        return self.name or self.host

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("key_path_", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def join_remote(directory: str, name: str) -> str:
    """Join a remote POSIX directory and a child name."""
    if not directory or directory == "/":
        return "/" + name
    return posixpath.join(directory.rstrip("/"), name)


def parent_remote(path: str) -> str:
    parent = posixpath.dirname(path.rstrip("/")) or "/"
    return parent


def normalise_remote(path: str) -> str:
    """Collapse ``..`` and duplicate slashes without touching the server."""
    path = (path or "/").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    normalised = posixpath.normpath(path)
    return normalised if normalised.startswith("/") else "/" + normalised


@dataclass
class TransferTask:
    """A queued or running upload/download."""

    task_id: int
    direction: str  # "upload" | "download"
    kind: str  # "file" | "tree"
    source: str
    destination: str
    display_name: str
    total_bytes: int = 0
    transferred: int = 0
    status: str = "queued"
    error: str = ""
    speed: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: object = field(default=None, repr=False, compare=False)
    files_done: int = 0
    files_total: int = 0

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.transferred * 100.0 / self.total_bytes)

    @property
    def eta_seconds(self) -> float | None:
        if self.speed <= 0 or self.total_bytes <= 0:
            return None
        remaining = max(0, self.total_bytes - self.transferred)
        return remaining / self.speed

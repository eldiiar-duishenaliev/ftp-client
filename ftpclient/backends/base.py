"""Protocol-agnostic remote filesystem interface."""

from __future__ import annotations

import stat as stat_module
from abc import ABC, abstractmethod
from typing import Callable

from ..models import Profile, RemoteEntry

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class BackendError(Exception):
    """Any recoverable problem while talking to a server."""


class AuthError(BackendError):
    """Bad credentials or a rejected login."""


class PathError(BackendError):
    """The requested remote path does not exist or is not usable."""


class TransferAborted(BackendError):
    """The user cancelled an in-flight transfer."""


def mode_from_st_mode(st_mode: int) -> int:
    return stat_module.S_IMODE(st_mode)


class RemoteBackend(ABC):
    """One live connection to a server.

    Implementations are not thread-safe: a backend belongs to exactly one
    worker (either the browser or the transfer engine).
    """

    protocol = "ftp"
    display_name = "FTP"

    def __init__(self, profile: Profile, password: str = "", passphrase: str = "") -> None:
        self.profile = profile
        self.password = password
        self.passphrase = passphrase
        self._connected = False
        self.home = "/"

    # -- lifecycle --------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "RemoteBackend":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- browsing ---------------------------------------------------------
    @abstractmethod
    def pwd(self) -> str: ...

    @abstractmethod
    def listdir(self, path: str) -> list[RemoteEntry]: ...

    # -- mutation ---------------------------------------------------------
    @abstractmethod
    def mkdir(self, path: str) -> None: ...

    @abstractmethod
    def remove_file(self, path: str) -> None: ...

    @abstractmethod
    def remove_tree(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> None: ...

    @abstractmethod
    def chmod(self, path: str, mode: int) -> None: ...

    @abstractmethod
    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None: ...

    @abstractmethod
    def download_file(
        self,
        remote_path: str,
        local_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None: ...

    # -- helpers ----------------------------------------------------------
    def path_exists(self, path: str) -> bool:
        try:
            parent = path.rsplit("/", 1)[0] or "/"
            name = path.rsplit("/", 1)[-1]
            return any(entry.name == name for entry in self.listdir(parent))
        except BackendError:
            return False

    def make_dirs(self, path: str) -> None:
        """Create ``path`` and any missing parents (idempotent)."""
        if path in ("", "/"):
            return
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            if self.path_exists(current):
                continue
            try:
                self.mkdir(current)
            except BackendError:
                # A race or a permission quirk: only fail if it is still absent.
                if not self.path_exists(current):
                    raise

    def walk_tree(self, path: str, max_depth: int = 64) -> list[RemoteEntry]:
        """Recursively collect every descendant of ``path``."""
        collected: list[RemoteEntry] = []
        stack: list[tuple[str, int]] = [(path, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                raise BackendError(
                    f"Folder nesting is deeper than {max_depth} levels; refusing to descend."
                )
            for entry in self.listdir(current):
                collected.append(entry)
                if entry.is_dir and not entry.is_link:
                    stack.append((entry.path, depth + 1))
        return collected

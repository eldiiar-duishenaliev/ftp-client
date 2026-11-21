"""FTP and FTPS backend built on :mod:`ftplib`."""

from __future__ import annotations

import ftplib
import os
import posixpath
import re
import socket
import ssl
from datetime import datetime, timezone

from ..models import Profile, RemoteEntry
from .base import (
    AuthError,
    BackendError,
    CancelCheck,
    PathError,
    ProgressCallback,
    RemoteBackend,
    TransferAborted,
)

BLOCK_SIZE = 64 * 1024

_UNIX_LIST_RE = re.compile(
    r"^(?P<mode>[bcdlps-][rwxStTs-]{9})[+.@]?\s+\d+\s+"
    r"(?:\S+\s+){1,2}"
    r"(?P<size>\d+)\s+"
    r"(?:(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})|"
    r"(?P<day2>\d{1,2})\s+(?P<month2>[A-Za-z]{3}))\s+"
    r"(?P<year>\d{4}|\d{1,2}:\d{2})\s+(?P<name>.+)$"
)
_DOS_LIST_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+"
    r"(?P<size><DIR>|\d+)\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_EPLF_LIST_RE = re.compile(r"^\+(?P<facts>[^,]*),.*?\t(?P<name>.+)$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS variant that starts TLS immediately (implicit FTPS)."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if self.timeout is not None and not self.timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        if source_address is not None:
            self.source_address = source_address
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout, source_address=self.source_address
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def _parse_modify(value: str) -> float | None:
    """Parse an MLSD ``modify`` fact (UTC, ``YYYYMMDDHHMMSS[.sss]``)."""
    value = value.strip().split(".")[0]
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def parse_unix_list_line(line: str, directory: str) -> RemoteEntry | None:
    match = _UNIX_LIST_RE.match(line.strip())
    if not match:
        return None
    name = match.group("name")
    if " -> " in name:
        name = name.split(" -> ", 1)[0]
    if name in (".", ".."):
        return None
    mode_text = match.group("mode")
    is_dir = mode_text.startswith("d")
    is_link = mode_text.startswith("l")
    month_name = match.group("month") or match.group("month2")
    day_value = match.group("day") or match.group("day2")
    month = _MONTHS.get(month_name.lower(), 1)
    day = int(day_value)
    stamp = match.group("year")
    now = datetime.now(timezone.utc)
    try:
        if ":" in stamp:
            hour, minute = (int(x) for x in stamp.split(":"))
            mtime = datetime(now.year, month, day, hour, minute, tzinfo=timezone.utc)
            if mtime > now:
                mtime = mtime.replace(year=now.year - 1)
        else:
            mtime = datetime(int(stamp), month, day, tzinfo=timezone.utc)
        mtime_ts = mtime.timestamp()
    except ValueError:
        mtime_ts = None
    return RemoteEntry(
        name=name,
        path=posixpath.join(directory, name) if directory != "/" else "/" + name,
        is_dir=is_dir,
        size=0 if is_dir else int(match.group("size")),
        mtime=mtime_ts,
        mode=_mode_bits(mode_text),
        is_link=is_link,
    )


def parse_dos_list_line(line: str, directory: str) -> RemoteEntry | None:
    match = _DOS_LIST_RE.match(line.strip())
    if not match:
        return None
    name = match.group("name")
    if name in (".", ".."):
        return None
    is_dir = match.group("size").upper() == "<DIR>"
    mtime_ts = None
    try:
        stamp = f"{match.group('date')} {match.group('time').replace(' ', '')}"
        fmt = "%m-%d-%y %I:%M%p" if len(match.group("date").split("-")[2]) == 2 else "%m-%d-%Y %I:%M%p"
        mtime_ts = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    return RemoteEntry(
        name=name,
        path=posixpath.join(directory, name) if directory != "/" else "/" + name,
        is_dir=is_dir,
        size=0 if is_dir else int(match.group("size")),
        mtime=mtime_ts,
    )


def _mode_bits(text: str) -> int:
    mode = 0
    for index, char in enumerate(text[1:10]):
        if char not in "-":
            mode |= 1 << (8 - index)
    return mode


class FTPBackend(RemoteBackend):
    protocol = "ftp"
    display_name = "FTP"

    def __init__(self, profile: Profile, password: str = "", passphrase: str = "") -> None:
        super().__init__(profile, password, passphrase)
        self._ftp: ftplib.FTP | None = None
        self._has_mlsd = False
        self._encoding = "utf-8"

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> None:
        profile = self.profile
        use_tls = profile.protocol == "ftps"
        context = ssl.create_default_context()
        if profile.verify_cert:
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        else:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        ftp: ftplib.FTP | None = None
        ready = False
        try:
            if use_tls and profile.tls_mode == "implicit":
                ftp = ImplicitFTP_TLS(context=context, timeout=45)
                ftp.connect(profile.host, profile.port)
                ftp.login(profile.username, self.password)
            elif use_tls:
                ftp = ftplib.FTP_TLS(context=context, timeout=45)
                ftp.connect(profile.host, profile.port)
                ftp.auth()  # AUTH TLS before the credentials cross the wire
                ftp.login(profile.username, self.password)
                ftp.prot_p()
            else:
                ftp = ftplib.FTP(timeout=45)
                ftp.connect(profile.host, profile.port)
                ftp.login(profile.username, self.password)

            ftp.set_pasv(profile.passive)
            try:
                ftp.voidcmd("TYPE I")
            except ftplib.Error:
                pass
            try:
                features = ftp.sendcmd("FEAT").upper()
                self._has_mlsd = "MLST" in features or "MLSD" in features
                if "UTF8" in features:
                    self._encoding = "utf-8"
            except ftplib.Error:
                self._has_mlsd = False
            self.home = self._safe_pwd()
            ready = True
        except ftplib.error_perm as exc:
            message = str(exc)
            if message.startswith("530") or "login" in message.lower():
                raise AuthError(
                    "Login rejected by the server. Check the username and password."
                ) from exc
            raise BackendError(message) from exc
        except ssl.SSLCertVerificationError as exc:
            raise BackendError(
                f"TLS certificate verification failed: {exc.verify_message or exc}"
            ) from exc
        except ssl.SSLError as exc:
            raise BackendError(f"TLS handshake failed: {exc}") from exc
        except (socket.gaierror, TimeoutError) as exc:
            raise BackendError(f"Cannot reach {profile.host}:{profile.port} ({exc})") from exc
        except OSError as exc:
            raise BackendError(f"Connection failed: {exc}") from exc
        finally:
            if not ready and ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass

        self._ftp = ftp
        self._connected = True

    def close(self) -> None:
        self._connected = False
        ftp, self._ftp = self._ftp, None
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _safe_pwd(self) -> str:
        try:
            return self._ftp.pwd() if self._ftp else "/"
        except ftplib.Error:
            return "/"

    def _conn(self) -> ftplib.FTP:
        if self._ftp is None:
            raise BackendError("Not connected")
        return self._ftp

    # -- browsing ---------------------------------------------------------
    def pwd(self) -> str:
        return self._safe_pwd()

    def listdir(self, path: str) -> list[RemoteEntry]:
        self._check_path(path)
        self._conn()  # fail fast when the control connection is gone
        if self._has_mlsd:
            try:
                return self._list_mlsd(path)
            except (ftplib.Error, ValueError, KeyError):
                self._has_mlsd = False
        return self._list_plain(path)

    def _list_mlsd(self, path: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        for name, facts in self._conn().mlsd(path):
            kind = (facts.get("type") or "").lower()
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            is_dir = kind == "dir"
            is_link = kind in ("slink", "os.unix=slink") or any(
                "slink" in str(value).lower() for value in facts.values()
            )
            try:
                size = int(facts.get("size", 0))
            except (TypeError, ValueError):
                size = 0
            entries.append(
                RemoteEntry(
                    name=name,
                    path=posixpath.join(path, name) if path != "/" else "/" + name,
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    mtime=_parse_modify(facts.get("modify", "")),
                    mode=int(facts["unix.mode"], 8) if facts.get("unix.mode") else None,
                    is_link=is_link,
                )
            )
        return entries

    def _list_plain(self, path: str) -> list[RemoteEntry]:
        ftp = self._conn()
        lines: list[str] = []
        original = self._safe_pwd()
        try:
            ftp.cwd(path)
        except ftplib.error_perm as exc:
            raise PathError(f"Cannot open {path}: {exc}") from exc
        try:
            ftp.retrlines("LIST", lines.append)
        except ftplib.Error as exc:
            raise BackendError(f"Listing failed: {exc}") from exc
        finally:
            try:
                ftp.cwd(original)
            except ftplib.Error:
                pass

        entries: list[RemoteEntry] = []
        for line in lines:
            entry = (
                parse_unix_list_line(line, path)
                or parse_dos_list_line(line, path)
                or self._parse_eplf(line, path)
            )
            if entry is not None:
                entries.append(entry)
        return entries

    @staticmethod
    def _parse_eplf(line: str, directory: str) -> RemoteEntry | None:
        match = _EPLF_LIST_RE.match(line)
        if not match:
            return None
        facts = {}
        for fact in match.group("facts").split(","):
            if fact:
                facts[fact[0]] = fact[1:]
        name = match.group("name")
        if name in (".", ".."):
            return None
        is_dir = facts.get("/") == "r"
        return RemoteEntry(
            name=name,
            path=posixpath.join(directory, name) if directory != "/" else "/" + name,
            is_dir=is_dir,
            size=0 if is_dir else int(facts.get("s") or 0),
            mtime=float(facts["m"]) if "m" in facts else None,
        )

    # -- mutation ---------------------------------------------------------
    @staticmethod
    def _check_path(path: str) -> None:
        if "\r" in path or "\n" in path:
            raise BackendError(
                "File names containing line breaks are not supported over FTP."
            )

    def mkdir(self, path: str) -> None:
        self._check_path(path)
        try:
            self._conn().mkd(path)
        except ftplib.error_perm as exc:
            raise BackendError(f"Cannot create folder {path}: {exc}") from exc

    def remove_file(self, path: str) -> None:
        self._check_path(path)
        try:
            self._conn().delete(path)
        except ftplib.error_perm as exc:
            raise BackendError(f"Cannot delete {path}: {exc}") from exc

    def remove_tree(self, path: str, _depth: int = 0) -> None:
        if _depth > 64:
            raise BackendError("Folder nesting is too deep to remove safely.")
        for entry in self.listdir(path):
            if entry.is_dir and not entry.is_link:
                self.remove_tree(entry.path, _depth + 1)
            else:
                self.remove_file(entry.path)
        self._check_path(path)
        try:
            self._conn().rmd(path)
        except ftplib.error_perm as exc:
            raise BackendError(f"Cannot remove folder {path}: {exc}") from exc

    def rename(self, old_path: str, new_path: str) -> None:
        self._check_path(old_path)
        self._check_path(new_path)
        try:
            self._conn().rename(old_path, new_path)
        except ftplib.error_perm as exc:
            raise BackendError(f"Rename failed: {exc}") from exc

    def chmod(self, path: str, mode: int) -> None:
        self._check_path(path)
        if not 0 <= mode <= 0o777:
            raise BackendError("Permissions must be between 000 and 777.")
        try:
            self._conn().sendcmd(f"SITE CHMOD {mode:03o} {path}")
        except ftplib.Error as exc:
            raise BackendError(
                "This server does not support changing permissions over FTP."
            ) from exc

    # -- transfers --------------------------------------------------------
    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None:
        ftp = self._conn()
        self._check_path(remote_path)
        total = os.path.getsize(local_path)
        remote_dir, remote_name = posixpath.split(remote_path)
        temp_path = posixpath.join(remote_dir, f".{remote_name}.part-{os.getpid()}")
        done = 0

        try:
            handle = open(local_path, "rb")
        except OSError as exc:
            raise BackendError(f"Cannot read local file {local_path}: {exc}") from exc

        try:
            with handle:
                def callback(block: bytes) -> None:
                    nonlocal done
                    if cancel and cancel():
                        raise TransferAborted("Upload cancelled")
                    done += len(block)
                    if progress:
                        progress(done, total)

                ftp.storbinary(
                    f"STOR {temp_path}", handle, blocksize=BLOCK_SIZE, callback=callback
                )
            self._replace_remote(temp_path, remote_path)
        except TransferAborted:
            self._quiet_delete(temp_path)
            raise
        except (ftplib.Error, OSError) as exc:
            self._quiet_delete(temp_path)
            raise BackendError(f"Upload failed: {exc}") from exc

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None:
        ftp = self._conn()
        self._check_path(remote_path)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        temp_path = f"{local_path}.part"
        done = 0
        total = 0
        try:
            total = ftp.size(remote_path) or 0
        except ftplib.Error:
            total = 0
        try:
            handle = open(temp_path, "wb")
        except OSError as exc:
            raise BackendError(f"Cannot write local file {local_path}: {exc}") from exc

        try:
            with handle:
                def callback(block: bytes) -> None:
                    nonlocal done
                    if cancel and cancel():
                        raise TransferAborted("Download cancelled")
                    handle.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)

                ftp.retrbinary(f"RETR {remote_path}", callback, blocksize=BLOCK_SIZE)
        except TransferAborted:
            self._quiet_unlink(temp_path)
            raise
        except (ftplib.Error, OSError) as exc:
            self._quiet_unlink(temp_path)
            raise BackendError(f"Download failed: {exc}") from exc

        try:
            os.replace(temp_path, local_path)
        except OSError as exc:
            self._quiet_unlink(temp_path)
            raise BackendError(f"Cannot save {local_path}: {exc}") from exc

    def _replace_remote(self, temp_path: str, target: str) -> None:
        ftp = self._conn()
        try:
            ftp.rename(temp_path, target)
            return
        except ftplib.Error:
            pass
        # Some servers refuse to overwrite; remove the target and retry once.
        if self.path_exists(target):
            self._quiet_delete(target)
        ftp.rename(temp_path, target)

    def _quiet_delete(self, path: str) -> None:
        try:
            if self._ftp:
                self._ftp.delete(path)
        except ftplib.Error:
            pass

    @staticmethod
    def _quiet_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

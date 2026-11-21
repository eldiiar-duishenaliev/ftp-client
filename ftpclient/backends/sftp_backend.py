"""SFTP backend built on Paramiko."""

from __future__ import annotations

import os
import posixpath
import stat as stat_module

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


def _load_private_key(path: str, passphrase: str):
    import paramiko

    key_classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    legacy_dss = getattr(paramiko, "DSSKey", None)
    if legacy_dss is not None:
        key_classes.append(legacy_dss)

    errors: list[str] = []
    for key_class in key_classes:
        try:
            return key_class.from_private_key_file(path, password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            if passphrase:
                raise AuthError(
                    "The private key could not be unlocked. Check the passphrase."
                ) from exc
            raise AuthError("The private key needs a passphrase.") from exc
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(str(exc))
        except OSError as exc:
            raise BackendError(f"Cannot read key file {path}: {exc}") from exc
    raise AuthError(
        "The selected file is not a supported private key. "
        "Use an OpenSSH or PEM key (RSA, ECDSA or Ed25519)."
    )


class SFTPBackend(RemoteBackend):
    protocol = "sftp"
    display_name = "SFTP"

    def __init__(self, profile: Profile, password: str = "", passphrase: str = "") -> None:
        super().__init__(profile, password, passphrase)
        self._client = None
        self._sftp = None

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> None:
        import paramiko

        profile = self.profile
        client = paramiko.SSHClient()
        sftp = None
        home = "/"
        ready = False
        try:
            if profile.verify_host_key:
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            kwargs = {
                "hostname": profile.host,
                "port": profile.port,
                "username": profile.username,
                "timeout": 25,
                "banner_timeout": 25,
                "auth_timeout": 25,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if profile.key_path:
                kwargs["pkey"] = _load_private_key(profile.key_path, self.passphrase)
                if self.password:
                    kwargs["password"] = self.password
            else:
                kwargs["password"] = self.password

            client.connect(**kwargs)
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
            sftp = client.open_sftp()
            channel = sftp.get_channel()
            if channel is not None:
                channel.settimeout(45)
            try:
                home = sftp.normalize(".")
            except OSError:
                home = "/"
            ready = True
        except paramiko.AuthenticationException as exc:
            raise AuthError(
                "Authentication failed. Check the username, password or key."
            ) from exc
        except paramiko.BadHostKeyException as exc:
            raise AuthError(
                f"Host key verification failed for {exc.hostname}. "
                "The server may have changed, or it is not in known_hosts."
            ) from exc
        except paramiko.SSHException as exc:
            raise BackendError(f"SSH error: {exc}") from exc
        except OSError as exc:
            raise BackendError(f"Cannot reach {profile.host}:{profile.port} ({exc})") from exc
        finally:
            if not ready:
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                try:
                    client.close()
                except Exception:
                    pass

        self._client = client
        self._sftp = sftp
        self.home = home
        self._connected = True

    def close(self) -> None:
        self._connected = False
        sftp, self._sftp = self._sftp, None
        client, self._client = self._client, None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _conn(self):
        if self._sftp is None:
            raise BackendError("Not connected")
        return self._sftp

    # -- browsing ---------------------------------------------------------
    def pwd(self) -> str:
        try:
            return self._conn().normalize(".")
        except OSError:
            return self.home or "/"

    def listdir(self, path: str) -> list[RemoteEntry]:
        try:
            attrs = self._conn().listdir_attr(path)
        except FileNotFoundError as exc:
            raise PathError(f"Folder does not exist: {path}") from exc
        except PermissionError as exc:
            raise PathError(f"Permission denied: {path}") from exc
        except OSError as exc:
            raise BackendError(f"Listing failed: {exc}") from exc

        entries: list[RemoteEntry] = []
        for attr in attrs:
            if attr.filename in (".", ".."):
                continue
            entry_path = posixpath.join(path, attr.filename) if path != "/" else "/" + attr.filename
            mode = attr.st_mode or 0
            is_link = stat_module.S_ISLNK(mode)
            is_dir = stat_module.S_ISDIR(mode)
            if is_link:
                try:
                    target = self._conn().stat(entry_path)
                    is_dir = stat_module.S_ISDIR(target.st_mode or 0)
                except OSError:
                    pass
            entries.append(
                RemoteEntry(
                    name=attr.filename,
                    path=entry_path,
                    is_dir=is_dir,
                    size=0 if is_dir else (attr.st_size or 0),
                    mtime=float(attr.st_mtime) if attr.st_mtime else None,
                    mode=stat_module.S_IMODE(mode) if mode else None,
                    is_link=is_link,
                )
            )
        return entries

    # -- mutation ---------------------------------------------------------
    def mkdir(self, path: str) -> None:
        try:
            self._conn().mkdir(path)
        except OSError as exc:
            raise BackendError(f"Cannot create folder {path}: {exc}") from exc

    def remove_file(self, path: str) -> None:
        try:
            self._conn().remove(path)
        except OSError as exc:
            raise BackendError(f"Cannot delete {path}: {exc}") from exc

    def remove_tree(self, path: str, _depth: int = 0) -> None:
        if _depth > 64:
            raise BackendError("Folder nesting is too deep to remove safely.")
        try:
            for entry in self.listdir(path):
                if entry.is_dir and not entry.is_link:
                    self.remove_tree(entry.path, _depth + 1)
                else:
                    self.remove_file(entry.path)
            self._conn().rmdir(path)
        except OSError as exc:
            raise BackendError(f"Cannot remove folder {path}: {exc}") from exc

    def rename(self, old_path: str, new_path: str) -> None:
        try:
            if self.path_exists(new_path):
                try:
                    self._conn().posix_rename(old_path, new_path)
                    return
                except (OSError, AttributeError):
                    self.remove_file(new_path)
            self._conn().rename(old_path, new_path)
        except OSError as exc:
            raise BackendError(f"Rename failed: {exc}") from exc

    def chmod(self, path: str, mode: int) -> None:
        if not 0 <= mode <= 0o777:
            raise BackendError("Permissions must be between 000 and 777.")
        try:
            self._conn().chmod(path, mode)
        except OSError as exc:
            raise BackendError(f"Cannot change permissions: {exc}") from exc

    # -- transfers --------------------------------------------------------
    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None:
        sftp = self._conn()
        total = os.path.getsize(local_path)
        remote_dir, remote_name = posixpath.split(remote_path)
        if remote_dir:
            self.make_dirs(remote_dir)
        temp_path = posixpath.join(remote_dir, f".{remote_name}.part-{os.getpid()}")

        def callback(done: int, _total: int) -> None:
            if cancel and cancel():
                raise TransferAborted("Upload cancelled")
            if progress:
                progress(min(done, total), total)

        try:
            sftp.put(local_path, temp_path, callback=callback, confirm=True)
            self._replace_remote(temp_path, remote_path)
        except TransferAborted:
            self._quiet_remove(temp_path)
            raise
        except OSError as exc:
            self._quiet_remove(temp_path)
            raise BackendError(f"Upload failed: {exc}") from exc

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
    ) -> None:
        sftp = self._conn()
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        temp_path = f"{local_path}.part"
        try:
            total = sftp.stat(remote_path).st_size or 0
        except OSError:
            total = 0

        def callback(done: int, _total: int) -> None:
            if cancel and cancel():
                raise TransferAborted("Download cancelled")
            if progress:
                progress(min(done, total), total)

        try:
            sftp.get(remote_path, temp_path, callback=callback)
            os.replace(temp_path, local_path)
        except TransferAborted:
            self._quiet_unlink(temp_path)
            raise
        except OSError as exc:
            self._quiet_unlink(temp_path)
            raise BackendError(f"Download failed: {exc}") from exc

    def _replace_remote(self, temp_path: str, target: str) -> None:
        sftp = self._conn()
        try:
            sftp.posix_rename(temp_path, target)
            return
        except (OSError, AttributeError):
            pass
        if self.path_exists(target):
            sftp.remove(target)
        sftp.rename(temp_path, target)

    def _quiet_remove(self, path: str) -> None:
        try:
            if self._sftp:
                self._sftp.remove(path)
        except OSError:
            pass

    @staticmethod
    def _quiet_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

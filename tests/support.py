"""Local test servers: plain FTP, explicit FTPS and SFTP."""

from __future__ import annotations

import datetime
import errno
import ipaddress
import logging
import os
import posixpath
import socket
import threading
import time

import paramiko
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

for _noisy in ("pyftpdlib", "paramiko"):
    _logger = logging.getLogger(_noisy)
    _logger.setLevel(logging.CRITICAL)
    _logger.disabled = True


def make_self_signed_cert(directory: str) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FTP Client Tests"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = os.path.join(directory, "server.key")
    cert_path = os.path.join(directory, "server.crt")
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_path, 0o600)
    return cert_path, key_path


class FtpTestServer:
    def __init__(
        self,
        root: str,
        *,
        username: str = "tester",
        password: str = "secret",
        tls: bool = False,
        certfile: str | None = None,
        keyfile: str | None = None,
        throttle_bps: int | None = None,
    ) -> None:
        self.root = root
        authorizer = DummyAuthorizer()
        authorizer.add_user(username, password, root, perm="elradfmwMT")

        if tls:
            from pyftpdlib.handlers import TLS_FTPHandler

            base = TLS_FTPHandler
        else:
            base = FTPHandler

        class Handler(base):
            pass

        Handler.authorizer = authorizer
        Handler.banner = "FTP Client test server"
        Handler.passive_ports = None
        if throttle_bps:
            from pyftpdlib.handlers import ThrottledDTPHandler

            class SlowDTP(ThrottledDTPHandler):
                read_limit = throttle_bps
                write_limit = throttle_bps

            Handler.dtp_handler = SlowDTP
        if tls:
            Handler.certfile = certfile
            Handler.keyfile = keyfile
        self.handler = Handler
        self.server = FTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.socket.getsockname()[1]
        self._thread: threading.Thread | None = None

    def start(self) -> "FtpTestServer":
        def serve() -> None:
            try:
                self.server.serve_forever(timeout=0.2, handle_exit=False)
            except OSError:
                pass

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        time.sleep(0.1)
        return self

    def stop(self) -> None:
        try:
            self.server.close_all()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        time.sleep(0.05)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()


# -- minimal SFTP server ---------------------------------------------------


class _RootedSFTPInterface(paramiko.SFTPServerInterface):
    ROOT = "/"

    def _real(self, path: str) -> str:
        normalised = posixpath.normpath("/" + (path or "/").lstrip("/"))
        parts = [part for part in normalised.split("/") if part]
        real = os.path.realpath(os.path.join(self.ROOT, *parts))
        root = os.path.realpath(self.ROOT)
        if real != root and not real.startswith(root + os.sep):
            raise OSError(errno.EACCES, "Path escapes the test root")
        return real

    def canonicalize(self, path: str) -> str:
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        return posixpath.normpath(path)

    def list_folder(self, path):
        try:
            real = self._real(path)
            result = []
            for name in os.listdir(real):
                stat = os.lstat(os.path.join(real, name))
                attr = paramiko.SFTPAttributes.from_stat(stat)
                attr.filename = name
                result.append(attr)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        try:
            real = self._real(path)
            mode = 0o666
            if attr is not None and attr.st_mode is not None:
                mode = attr.st_mode
            fd = os.open(real, flags, mode)
            return _LocalHandle(fd, flags)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            os.remove(self._real(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, oldpath, newpath):
        try:
            os.replace(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):
        try:
            mode = 0o777
            if attr is not None and attr.st_mode is not None:
                mode = attr.st_mode
            os.mkdir(self._real(path), mode)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, path, attr):
        try:
            real = self._real(path)
            if attr.st_mode is not None:
                os.chmod(real, attr.st_mode)
            if attr.st_uid is not None or attr.st_gid is not None:
                pass
            if attr.st_size is not None:
                os.truncate(real, attr.st_size)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class _LocalHandle(paramiko.SFTPHandle):
    def __init__(self, fd: int, flags: int) -> None:
        super().__init__(flags)
        if flags & os.O_RDWR:
            mode = "r+b"
        elif flags & os.O_WRONLY:
            mode = "wb"
        else:
            mode = "rb"
        self.fs = os.fdopen(fd, mode)

    def read(self, offset: int, length: int):
        try:
            self.fs.seek(offset)
            return self.fs.read(length)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def write(self, offset: int, data: bytes):
        try:
            self.fs.seek(offset)
            self.fs.write(data)
            self.fs.flush()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.fs.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def close(self):
        try:
            self.fs.close()
        except OSError:
            pass
        return paramiko.SFTP_OK


class _AuthServer(paramiko.ServerInterface):
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"


class SftpTestServer:
    def __init__(
        self,
        root: str,
        *,
        username: str = "tester",
        password: str = "secret",
    ) -> None:
        self.root = root
        self.username = username
        self.password = password
        self.host_key = paramiko.RSAKey.generate(2048)
        self.interface = type(
            "TestSFTPInterface",
            (_RootedSFTPInterface,),
            {"ROOT": root},
        )
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transports: list[paramiko.Transport] = []
        self._channels: list[paramiko.Channel] = []

    def start(self) -> "SftpTestServer":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        time.sleep(0.05)
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._socket.accept()
            except OSError:
                break
            try:
                transport = paramiko.Transport(client)
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler(
                    "sftp", paramiko.SFTPServer, self.interface
                )
                transport.start_server(
                    server=_AuthServer(self.username, self.password)
                )
                channel = transport.accept(5)
                if channel is not None:
                    self._channels.append(channel)
                self._transports.append(transport)
            except Exception:
                try:
                    client.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        for channel in list(self._channels):
            try:
                channel.close()
            except Exception:
                pass
        self._channels.clear()
        for transport in list(self._transports):
            try:
                transport.close()
            except Exception:
                pass
        self._transports.clear()
        if self._thread is not None:
            self._thread.join(timeout=3)
        time.sleep(0.05)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

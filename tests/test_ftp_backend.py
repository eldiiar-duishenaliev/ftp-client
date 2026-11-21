"""FTP and FTPS backend integration tests against a local pyftpdlib server."""
from __future__ import annotations

import os
import tempfile
import unittest

from ftpclient.backends import AuthError, BackendError, create_backend
from ftpclient.models import Profile
from tests.support import FtpTestServer, make_self_signed_cert


class FTPBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = cls._tmp.name
        cls.server = FtpTestServer(cls.root).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.profile = Profile(
            name="test",
            protocol="ftp",
            host="127.0.0.1",
            port=self.server.port,
            username="tester",
        )
        self.backend = create_backend(self.profile, "secret")
        self.backend.connect()

    def tearDown(self):
        self.backend.close()
        self.work.cleanup()

    def test_browse_upload_download_rename_delete(self):
        backend = self.backend
        backend.mkdir("/alpha")
        self.assertTrue(any(e.name == "alpha" and e.is_dir for e in backend.listdir("/")))

        source = os.path.join(self.work.name, "hello.txt")
        payload = b"Hello from the test suite\n"
        with open(source, "wb") as handle:
            handle.write(payload)

        progress = []
        backend.upload_file(
            source, "/alpha/hello.txt", lambda done, total: progress.append((done, total))
        )
        entry = next(e for e in backend.listdir("/alpha") if e.name == "hello.txt")
        self.assertEqual(entry.size, len(payload))
        self.assertEqual(progress[-1], (len(payload), len(payload)))

        destination = os.path.join(self.work.name, "downloaded.txt")
        backend.download_file("/alpha/hello.txt", destination)
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), payload)

        backend.rename("/alpha/hello.txt", "/alpha/renamed.txt")
        names = {e.name for e in backend.listdir("/alpha")}
        self.assertIn("renamed.txt", names)
        self.assertNotIn("hello.txt", names)

        backend.chmod("/alpha/renamed.txt", 0o600)
        on_disk = os.path.join(self.root, "alpha", "renamed.txt")
        self.assertEqual(os.stat(on_disk).st_mode & 0o777, 0o600)

        backend.remove_tree("/alpha")
        with self.assertRaises(BackendError):
            backend.listdir("/alpha")
        self.assertNotIn("alpha", {e.name for e in backend.listdir("/")})

    def test_overwrite_replaces_content(self):
        source = os.path.join(self.work.name, "file.txt")
        with open(source, "wb") as handle:
            handle.write(b"first")
        self.backend.upload_file(source, "/over.txt")
        with open(source, "wb") as handle:
            handle.write(b"second version")
        self.backend.upload_file(source, "/over.txt")
        destination = os.path.join(self.work.name, "check.txt")
        self.backend.download_file("/over.txt", destination)
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"second version")

    def test_unicode_names(self):
        source = os.path.join(self.work.name, "тест.txt")
        with open(source, "wb") as handle:
            handle.write(b"unicode")
        self.backend.upload_file(source, "/тест.txt")
        names = {e.name for e in self.backend.listdir("/")}
        self.assertIn("тест.txt", names)

    def test_listing_fallback_to_list_command(self):
        self.backend._has_mlsd = False
        entries = self.backend.listdir("/")
        self.assertIsInstance(entries, list)
        source = os.path.join(self.work.name, "fallback.txt")
        with open(source, "wb") as handle:
            handle.write(b"x" * 10)
        self.backend.upload_file(source, "/fallback.txt")
        self.backend._has_mlsd = False
        entry = next((e for e in self.backend.listdir("/") if e.name == "fallback.txt"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.size, 10)

    def test_missing_path_raises(self):
        with self.assertRaises(BackendError):
            self.backend.listdir("/does-not-exist")

    def test_bad_password(self):
        profile = Profile(
            name="bad", protocol="ftp", host="127.0.0.1",
            port=self.server.port, username="tester",
        )
        backend = create_backend(profile, "wrong")
        with self.assertRaises(AuthError):
            backend.connect()
        backend.close()

    def test_make_dirs_is_idempotent(self):
        self.backend.make_dirs("/one/two/three")
        self.backend.make_dirs("/one/two/three")
        entries = self.backend.listdir("/one/two")
        self.assertIn("three", {e.name for e in entries})

    def test_active_mode(self):
        profile = Profile(
            name="active", protocol="ftp", host="127.0.0.1",
            port=self.server.port, username="tester", passive=False,
        )
        backend = create_backend(profile, "secret")
        backend.connect()
        try:
            self.assertIsInstance(backend.listdir("/"), list)
            source = os.path.join(self.work.name, "active.txt")
            with open(source, "wb") as handle:
                handle.write(b"active mode")
            backend.upload_file(source, "/active.txt")
            target = os.path.join(self.work.name, "active-copy.txt")
            backend.download_file("/active.txt", target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"active mode")
        finally:
            backend.close()


class FTPSBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls._tmp.name, "files")
        os.makedirs(cls.root)
        cert_dir = os.path.join(cls._tmp.name, "certs")
        os.makedirs(cert_dir)
        cert, key = make_self_signed_cert(cert_dir)
        cls.server = FtpTestServer(cls.root, tls=True, certfile=cert, keyfile=key).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp.cleanup()

    def test_explicit_ftps_transfer_without_verification(self):
        profile = Profile(
            name="tls", protocol="ftps", host="127.0.0.1", port=self.server.port,
            username="tester", tls_mode="explicit", verify_cert=False,
        )
        backend = create_backend(profile, "secret")
        backend.connect()
        try:
            with tempfile.TemporaryDirectory() as work:
                source = os.path.join(work, "secure.txt")
                with open(source, "wb") as handle:
                    handle.write(b"over TLS")
                backend.upload_file(source, "/secure.txt")
                target = os.path.join(work, "secure.copy")
                backend.download_file("/secure.txt", target)
                with open(target, "rb") as handle:
                    self.assertEqual(handle.read(), b"over TLS")
                self.assertTrue(any(e.name == "secure.txt" for e in backend.listdir("/")))
        finally:
            backend.close()

    def test_self_signed_certificate_is_rejected_by_default(self):
        profile = Profile(
            name="strict", protocol="ftps", host="127.0.0.1", port=self.server.port,
            username="tester", tls_mode="explicit", verify_cert=True,
        )
        backend = create_backend(profile, "secret")
        with self.assertRaises(BackendError):
            backend.connect()
        backend.close()


if __name__ == "__main__":
    unittest.main()

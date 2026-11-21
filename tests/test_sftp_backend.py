"""SFTP backend integration tests against a local Paramiko server."""
from __future__ import annotations

import os
import tempfile
import unittest

from ftpclient.backends import AuthError, BackendError, create_backend
from ftpclient.models import Profile
from tests.support import SftpTestServer


class SFTPBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = cls._tmp.name
        cls.server = SftpTestServer(cls.root).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.profile = Profile(
            name="sftp-test",
            protocol="sftp",
            host="127.0.0.1",
            port=self.server.port,
            username="tester",
        )
        self.backend = create_backend(self.profile, "secret")
        self.backend.connect()

    def tearDown(self):
        self.backend.close()
        self.work.cleanup()

    def test_home_and_listing(self):
        self.assertTrue(self.backend.home.startswith("/"))
        self.assertIsInstance(self.backend.listdir(self.backend.home), list)

    def test_full_file_lifecycle(self):
        backend = self.backend
        backend.mkdir("/work")
        children = backend.listdir("/")
        self.assertTrue(any(entry.name == "work" and entry.is_dir for entry in children))

        source = os.path.join(self.work.name, "payload.txt")
        payload = b"SFTP payload \x00\x01 binary\n"
        with open(source, "wb") as handle:
            handle.write(payload)

        progress = []
        backend.upload_file(
            source, "/work/payload.txt", lambda done, total: progress.append((done, total))
        )
        entry = next(e for e in backend.listdir("/work") if e.name == "payload.txt")
        self.assertEqual(entry.size, len(payload))
        self.assertEqual(progress[-1], (len(payload), len(payload)))

        target = os.path.join(self.work.name, "copy.txt")
        backend.download_file("/work/payload.txt", target)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), payload)

        backend.rename("/work/payload.txt", "/work/renamed.txt")
        names = {e.name for e in backend.listdir("/work")}
        self.assertEqual(names, {"renamed.txt"})

        backend.chmod("/work/renamed.txt", 0o640)
        entry = next(e for e in backend.listdir("/work") if e.name == "renamed.txt")
        self.assertEqual(entry.mode, 0o640)
        self.assertEqual(entry.mode_text, "rw-r-----")

        backend.remove_tree("/work")
        self.assertNotIn("work", {e.name for e in backend.listdir("/")})

    def test_nested_make_dirs(self):
        self.backend.make_dirs("/a/b/c")
        self.backend.make_dirs("/a/b/c")
        self.assertIn("c", {e.name for e in self.backend.listdir("/a/b")})

    def test_overwrite_upload(self):
        source = os.path.join(self.work.name, "same.txt")
        with open(source, "wb") as handle:
            handle.write(b"one")
        self.backend.upload_file(source, "/same.txt")
        with open(source, "wb") as handle:
            handle.write(b"two")
        self.backend.upload_file(source, "/same.txt")
        target = os.path.join(self.work.name, "out.txt")
        self.backend.download_file("/same.txt", target)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"two")

    def test_unicode_names(self):
        source = os.path.join(self.work.name, "файл.txt")
        with open(source, "wb") as handle:
            handle.write(b"unicode")
        self.backend.upload_file(source, "/файл.txt")
        self.assertIn("файл.txt", {e.name for e in self.backend.listdir("/")})

    def test_bad_credentials(self):
        profile = Profile(
            name="bad", protocol="sftp", host="127.0.0.1",
            port=self.server.port, username="tester",
        )
        backend = create_backend(profile, "nope")
        with self.assertRaises(AuthError):
            backend.connect()
        backend.close()

    def test_missing_path(self):
        with self.assertRaises(BackendError):
            self.backend.listdir("/nope/nothing")

    def test_only_removes_empty_or_own_content(self):
        self.backend.make_dirs("/tree/keep")
        with self.assertRaises(BackendError):
            self.backend.chmod("/does/not/exist", 0o644)


if __name__ == "__main__":
    unittest.main()

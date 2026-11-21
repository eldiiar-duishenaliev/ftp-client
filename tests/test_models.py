"""Tests for path helpers and FTP listing parsers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ftpclient.backends.ftp_backend import (
    parse_dos_list_line,
    parse_unix_list_line,
)
from ftpclient.config import Config
from ftpclient.models import (
    Profile,
    RemoteEntry,
    join_remote,
    normalise_remote,
    parent_remote,
)


class ConfigTests(unittest.TestCase):
    def test_config_dir_override_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory))
            self.assertEqual(config.profiles_file, Path(directory) / "profiles.json")
            config.save_profiles([Profile(name="x", host="example.com", port=21)])
            self.assertTrue(config.profiles_file.exists())
            self.assertEqual(config.load_profiles()[0].name, "x")


class PathTests(unittest.TestCase):
    def test_join_remote(self):
        self.assertEqual(join_remote("/", "file.txt"), "/file.txt")
        self.assertEqual(join_remote("/home/user", "file.txt"), "/home/user/file.txt")
        self.assertEqual(join_remote("/home/user/", "file.txt"), "/home/user/file.txt")

    def test_parent_remote(self):
        self.assertEqual(parent_remote("/home/user"), "/home")
        self.assertEqual(parent_remote("/home"), "/")
        self.assertEqual(parent_remote("/"), "/")

    def test_normalise_remote(self):
        self.assertEqual(normalise_remote("/home//user/../user/./x"), "/home/user/x")
        self.assertEqual(normalise_remote("relative/path"), "/relative/path")
        self.assertEqual(normalise_remote(""), "/")
        self.assertEqual(normalise_remote("~"), "/~")


class ProfileTests(unittest.TestCase):
    def test_round_trip_has_no_password_field(self):
        profile = Profile(name="work", protocol="ftps", host="example.com", port=21, username="me")
        data = profile.to_dict()
        self.assertNotIn("password", data)
        restored = Profile.from_dict(data)
        self.assertEqual(restored.keychain_account, "ftps://me@example.com:21")
        self.assertEqual(restored.verify_cert, True)

    def test_default_port(self):
        self.assertEqual(Profile(name="x", protocol="sftp", host="h", port=0).port, 22)


class ListingParserTests(unittest.TestCase):
    def test_unix_line_file(self):
        entry = parse_unix_list_line(
            "-rw-r--r--    1 user  staff      1234 12 Mar 2024  report final.pdf",
            "/docs",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "report final.pdf")
        self.assertEqual(entry.path, "/docs/report final.pdf")
        self.assertFalse(entry.is_dir)
        self.assertEqual(entry.size, 1234)
        self.assertEqual(entry.mode, 0o644)

    def test_unix_line_directory_with_time(self):
        entry = parse_unix_list_line(
            "drwxr-xr-x    5 user  staff       160 Nov 21 10:42 Projects", "/"
        )
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_dir)
        self.assertEqual(entry.path, "/Projects")
        self.assertEqual(entry.mode, 0o755)
        self.assertIsNotNone(entry.mtime)

    def test_unix_line_symlink(self):
        entry = parse_unix_list_line(
            "lrwxrwxrwx    1 user  staff         4 Nov 21 10:42 link -> target", "/tmp"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "link")
        self.assertTrue(entry.is_link)

    def test_dos_line(self):
        entry = parse_dos_list_line(
            "03-12-24  04:35PM       <DIR>          Photos", "/"
        )
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_dir)
        self.assertEqual(entry.name, "Photos")

        entry = parse_dos_list_line(
            "03-12-24  04:35PM                2048 notes.txt", "/"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.size, 2048)

    def test_ignores_dot_entries(self):
        self.assertIsNone(parse_unix_list_line("drwxr-xr-x 1 u g 1 Jan 01 2024 .", "/"))
        self.assertIsNone(parse_unix_list_line("drwxr-xr-x 1 u g 1 Jan 01 2024 ..", "/"))

    def test_mode_text(self):
        entry = RemoteEntry(name="x", path="/x", mode=0o644)
        self.assertEqual(entry.mode_text, "rw-r--r--")


if __name__ == "__main__":
    unittest.main()

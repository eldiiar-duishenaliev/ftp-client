"""End-to-end smoke test for the desktop window against a live FTP server.

The whole class shares one Tk window: creating and destroying several roots in
one process is known to crash Tk 8.6 on macOS. Skipped when Tk cannot start.
"""
from __future__ import annotations

import os
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from ftpclient.models import Profile
from tests.support import FtpTestServer


def wait_until(app, predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            app.update()
        except tk.TclError:
            return False
        if predicate():
            return True
        time.sleep(0.02)
    return False


def transaction_finished(app, timeout: float = 25.0) -> list:
    def done():
        tasks = app.transfer_manager.snapshot()
        return bool(tasks) and all(not task.is_active for task in tasks)

    if not wait_until(app, done, timeout):
        raise AssertionError(
            "transfers stuck: "
            + repr([(t.display_name, t.status, t.error) for t in app.transfer_manager.snapshot()])
        )
    return app.transfer_manager.snapshot()


class WindowTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        from ftpclient.config import Config
        from ftpclient.ui.app import FTPClientApp

        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls._tmp.name, "server")
        os.makedirs(cls.root)
        cls.server = FtpTestServer(cls.root).start()
        cls.config_dir = tempfile.TemporaryDirectory()
        try:
            cls.app = FTPClientApp(Config(Path(cls.config_dir.name)))
            cls.app.update()
        except tk.TclError as exc:
            cls.server.stop()
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")

        cls.profile = Profile(
            name="ui-test",
            protocol="ftp",
            host="127.0.0.1",
            port=cls.server.port,
            username="tester",
            remote_path="/",
        )
        cls.app.profiles.append(cls.profile)
        cls.app._reload_profiles("ui-test")
        cls.app._connect(cls.profile, "secret", "", from_dialog=True)
        if not wait_until(cls.app, lambda: cls.app.backend is not None and not cls.app._connecting):
            raise RuntimeError("UI client could not connect to the test server")

    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            try:
                cls.app._on_close()
            except tk.TclError:
                pass
            cls.app = None
        cls.server.stop()
        cls.config_dir.cleanup()
        cls._tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        if self.app.backend is None:
            self.app._connect(self.profile, "secret", "", from_dialog=True)
            self.assertTrue(
                wait_until(
                    self.app,
                    lambda: self.app.backend is not None and not self.app._connecting,
                )
            )
        self.app.transfer_manager.clear_finished()
        self.app._refresh_remote("/")
        wait_until(self.app, lambda: self.app.remote_path == "/")

    def tearDown(self):
        # Let automatic post-transfer refreshes settle, then park both panes in
        # directories that outlive this test.
        self.app._refresh_local(os.path.expanduser("~"), silent=True)
        wait_until(
            self.app,
            lambda: not self.app._pending_refresh and self.app._busy_ops == 0,
            timeout=5,
        )
        self.work.cleanup()

    # -- helpers ---------------------------------------------------------
    def _select(self, pane, name):
        for iid, entry in pane._rows.items():
            if entry.name == name:
                pane.tree.selection_set(iid)
                pane.tree.focus(iid)
                return entry
        self.fail(f"{name} is not visible in the {pane.side} pane")

    def _refresh(self, path):
        self.app._refresh_local(path)
        self.app.update()
        return self.app.local_pane

    def _wait_remote(self, name: str):
        return wait_until(
            self.app,
            lambda: any(item.name == name for item in self.app.remote_items),
        )

    # -- tests -----------------------------------------------------------
    def test_a_connection_populates_panes(self):
        self.assertTrue(self.app.remote_pane._enabled)
        self.assertEqual(self.app.remote_path, "/")
        self.assertEqual(self.app.profile_combo.get(), "ui-test")

    def test_b_upload_download_file_through_window(self):
        local = os.path.join(self.work.name, "local")
        os.makedirs(local)
        payload = b"uploaded from the window\n"
        with open(os.path.join(local, "hello.txt"), "wb") as handle:
            handle.write(payload)
        pane = self._refresh(local)
        self._select(pane, "hello.txt")
        self.app._upload_selection()
        tasks = transaction_finished(self.app)
        self.assertEqual(tasks[-1].status, "done", tasks[-1].error)
        self.assertTrue(self._wait_remote("hello.txt"))

        downloads = os.path.join(self.work.name, "downloads")
        os.makedirs(downloads)
        self._refresh(downloads)
        entry = self._select(self.app.remote_pane, "hello.txt")
        self.app._download_items([entry])
        tasks = transaction_finished(self.app)
        self.assertEqual(tasks[-1].status, "done", tasks[-1].error)
        with open(os.path.join(downloads, "hello.txt"), "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_c_recursive_folder_transfer_through_window(self):
        local = os.path.join(self.work.name, "local2")
        os.makedirs(os.path.join(local, "album", "2024"))
        with open(os.path.join(local, "album", "cover.png"), "wb") as handle:
            handle.write(b"png")
        with open(os.path.join(local, "album", "2024", "photo.jpg"), "wb") as handle:
            handle.write(b"jpeg-data")
        pane = self._refresh(local)
        self._select(pane, "album")
        self.app._upload_selection()
        tasks = transaction_finished(self.app)
        self.assertEqual(tasks[-1].kind, "tree")
        self.assertEqual(tasks[-1].status, "done", tasks[-1].error)
        with open(os.path.join(self.root, "album", "2024", "photo.jpg"), "rb") as handle:
            self.assertEqual(handle.read(), b"jpeg-data")

        downloads = os.path.join(self.work.name, "download2")
        os.makedirs(downloads)
        self._refresh(downloads)
        self._wait_remote("album")
        entry = self._select(self.app.remote_pane, "album")
        self.app._download_items([entry])
        tasks = transaction_finished(self.app)
        self.assertEqual(tasks[-1].status, "done", tasks[-1].error)
        self.assertTrue(
            os.path.isfile(os.path.join(downloads, "album", "2024", "photo.jpg"))
        )

    def test_d_remote_new_rename_and_delete(self):
        with mock.patch("ftpclient.ui.app.simpledialog.askstring", return_value="newdir"):
            self.app._remote_new_folder()
        self.assertTrue(
            wait_until(self.app, lambda: os.path.isdir(os.path.join(self.root, "newdir")))
        )

        wait_until(self.app, lambda: any(e.name == "newdir" for e in self.app.remote_items))
        entry = self._select(self.app.remote_pane, "newdir")
        with mock.patch("ftpclient.ui.app.simpledialog.askstring", return_value="renamed"):
            self.app._remote_rename(entry)
        self.assertTrue(
            wait_until(self.app, lambda: os.path.isdir(os.path.join(self.root, "renamed")))
        )

        wait_until(self.app, lambda: any(e.name == "renamed" for e in self.app.remote_items))
        entry = next(item for item in self.app.remote_items if item.name == "renamed")
        with mock.patch("ftpclient.ui.app.messagebox.askyesno", return_value=True):
            self.app._remote_delete([entry])
        self.assertTrue(
            wait_until(self.app, lambda: not os.path.isdir(os.path.join(self.root, "renamed")))
        )

    def test_e_local_new_rename_and_delete(self):
        local = os.path.join(self.work.name, "localfs")
        os.makedirs(local)
        self._refresh(local)
        with mock.patch("ftpclient.ui.app.simpledialog.askstring", return_value="folder-a"):
            self.app._local_new_folder()
        self.assertTrue(os.path.isdir(os.path.join(local, "folder-a")))

        entry = self._select(self.app.local_pane, "folder-a")
        with mock.patch("ftpclient.ui.app.simpledialog.askstring", return_value="folder-b"):
            self.app._local_rename(entry)
        self.assertTrue(os.path.isdir(os.path.join(local, "folder-b")))

        entry = next(item for item in self.app.local_pane._items if item.name == "folder-b")
        with mock.patch("ftpclient.ui.app.messagebox.askyesno", return_value=True):
            self.app._local_delete([entry])
        self.assertFalse(os.path.exists(os.path.join(local, "folder-b")))

    def test_f_hidden_files_toggle(self):
        local = os.path.join(self.work.name, "hidden")
        os.makedirs(local)
        with open(os.path.join(local, ".secret"), "wb") as handle:
            handle.write(b"hidden")
        self._refresh(local)
        self.assertFalse(any(item.name == ".secret" for item in self.app.local_pane._items))
        self.app.local_hidden_var.set(True)
        self.app._hidden_menu_changed("local")
        self.assertTrue(any(item.name == ".secret" for item in self.app.local_pane._items))
        self.app.local_hidden_var.set(False)
        self.app._hidden_menu_changed("local")

    def test_g_server_side_new_folder_visible(self):
        with mock.patch("ftpclient.ui.app.simpledialog.askstring", return_value="listed"):
            self.app._remote_new_folder()
        self.assertTrue(
            wait_until(self.app, lambda: os.path.isdir(os.path.join(self.root, "listed")))
        )
        self.assertTrue(self._wait_remote("listed"))

    def test_z_disconnect_clears_remote_pane(self):
        self.app._disconnect(interactive=False)
        self.app.update()
        self.assertIsNone(self.app.backend)
        self.assertFalse(self.app.remote_pane._enabled)
        self.assertIn("Not connected", self.app.remote_pane.footer_label.cget("text"))


if __name__ == "__main__":
    unittest.main()

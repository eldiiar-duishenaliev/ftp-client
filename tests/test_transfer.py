"""Transfer queue integration tests (recursive upload/download, cancellation)."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

from ftpclient.backends import TransferAborted, create_backend
from ftpclient.models import Profile, RemoteEntry
from ftpclient.transfer import TransferManager
from tests.support import FtpTestServer


class SlowUploadBackend:
    """Duck-typed backend that uploads slowly so cancellation is deterministic."""

    connected = True

    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()

    def make_dirs(self, path: str) -> None:
        return None

    def upload_file(self, local_path, remote_path, progress=None, cancel=None) -> None:
        total = os.path.getsize(local_path)
        done = 0
        while done < total:
            if self.closed.is_set() or (cancel and cancel()):
                raise TransferAborted("cancelled by test")
            time.sleep(0.01)
            done = min(total, done + 4096)
            if progress:
                progress(done, total)


def wait_for_finish(manager: TransferManager, timeout: float = 40.0) -> list:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = manager.snapshot()
        if tasks and all(not task.is_active for task in tasks):
            return tasks
        time.sleep(0.05)
    raise AssertionError(
        "Transfer did not finish: "
        + repr([(t.display_name, t.status, t.error) for t in manager.snapshot()])
    )


class TransferManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls._tmp.name, "server")
        os.makedirs(cls.root)
        cls.server = FtpTestServer(cls.root).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.profile = Profile(
            name="queue", protocol="ftp", host="127.0.0.1",
            port=self.server.port, username="tester",
        )
        self.events: list[tuple] = []
        self.manager = TransferManager(
            lambda: self._connect(), lambda event, payload: self.events.append((event, payload))
        )
        self.manager.start()

    def tearDown(self):
        self.manager.stop()
        self.work.cleanup()

    def _connect(self):
        backend = create_backend(self.profile, "secret")
        backend.connect()
        return backend

    def _make_source_tree(self) -> str:
        root = os.path.join(self.work.name, "package")
        os.makedirs(os.path.join(root, "nested", "deeper"))
        with open(os.path.join(root, "readme.txt"), "wb") as handle:
            handle.write(b"top level\n")
        with open(os.path.join(root, "nested", "data.bin"), "wb") as handle:
            handle.write(bytes(range(256)) * 40)
        with open(os.path.join(root, "nested", "deeper", "note.txt"), "wb") as handle:
            handle.write(b"deep\n")
        return root

    def test_recursive_upload_and_download(self):
        source = self._make_source_tree()
        tasks = self.manager.add_upload([source], "/")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].kind, "tree")
        finished = wait_for_finish(self.manager)
        self.assertEqual(finished[0].status, "done", finished[0].error)
        self.assertEqual(finished[0].transferred, finished[0].total_bytes)
        self.assertEqual(finished[0].files_done, 3)

        uploaded_root = os.path.join(self.root, "package")
        with open(os.path.join(uploaded_root, "nested", "data.bin"), "rb") as handle:
            self.assertEqual(handle.read(), bytes(range(256)) * 40)
        with open(os.path.join(uploaded_root, "nested", "deeper", "note.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"deep\n")

        destination = os.path.join(self.work.name, "downloads")
        os.makedirs(destination)
        entry = RemoteEntry(name="package", path="/package", is_dir=True)
        tasks = self.manager.add_download([entry], destination)
        self.assertEqual(tasks[0].kind, "tree")
        finished = wait_for_finish(self.manager)
        download_task = finished[-1]
        self.assertEqual(download_task.status, "done", download_task.error)
        self.assertEqual(download_task.files_done, 3)
        local_root = os.path.join(destination, "package")
        with open(os.path.join(local_root, "nested", "deeper", "note.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"deep\n")
        self.assertFalse(
            any(name.endswith(".part") for name in os.listdir(os.path.join(local_root, "nested")))
        )

    def test_single_file_upload_and_progress_events(self):
        source = os.path.join(self.work.name, "single.txt")
        payload = b"x" * 5000
        with open(source, "wb") as handle:
            handle.write(payload)
        tasks = self.manager.add_upload([source], "/")
        wait_for_finish(self.manager)
        self.assertEqual(tasks[0].status, "done")
        with open(os.path.join(self.root, "single.txt"), "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertTrue(any(event == "changed" for event, _ in self.events))

    def test_queued_task_can_be_cancelled(self):
        # A fresh manager without a worker keeps tasks queued.
        idle = TransferManager(lambda: self._connect(), lambda *_: None)
        source = os.path.join(self.work.name, "queued.txt")
        with open(source, "wb") as handle:
            handle.write(b"queued")
        first = idle.add_upload([source], "/")[0]
        second = idle.add_upload([source], "/")[0]
        idle.cancel(second.task_id)
        self.assertEqual(second.status, "cancelled")
        idle.cancel(first.task_id)
        self.assertEqual(first.status, "cancelled")
        idle.clear_finished()
        self.assertEqual(idle.snapshot(), [])

    def test_retry_cancelled_task(self):
        idle = TransferManager(lambda: self._connect(), lambda *_: None)
        source = os.path.join(self.work.name, "retry.txt")
        with open(source, "wb") as handle:
            handle.write(b"retry")
        task = idle.add_upload([source], "/")[0]
        idle.cancel(task.task_id)
        self.assertEqual(task.status, "cancelled")
        idle.retry(task.task_id)
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.transferred, 0)
        idle.stop()

    def test_active_upload_can_be_cancelled(self):
        manager = TransferManager(lambda: SlowUploadBackend(), lambda *_: None)
        manager.start()
        try:
            big = os.path.join(self.work.name, "big.bin")
            with open(big, "wb") as handle:
                handle.write(os.urandom(512 * 1024))
            task = manager.add_upload([big], "/")[0]
            deadline = time.time() + 10
            while time.time() < deadline and task.status != "running":
                time.sleep(0.01)
            self.assertEqual(task.status, "running")
            time.sleep(0.08)
            manager.cancel(task.task_id)
            finished = wait_for_finish(manager, timeout=10)
            self.assertEqual(finished[0].status, "cancelled")
            self.assertLess(finished[0].transferred, finished[0].total_bytes)
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()

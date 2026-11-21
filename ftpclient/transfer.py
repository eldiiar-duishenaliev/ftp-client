"""Background transfer queue.

All network work happens on one worker thread so that transfers never block
the UI. The engine owns its own server connection, separate from the browser,
which keeps browsing responsive while a transfer is running.
"""

from __future__ import annotations

import itertools
import os
import posixpath
import threading
import time
import traceback
from typing import Callable

from .backends import BackendError, RemoteBackend, TransferAborted
from .models import RemoteEntry, TransferTask, join_remote

EmitFn = Callable[[str, object], None]
FactoryFn = Callable[[], RemoteBackend]

MAX_EMIT_INTERVAL = 0.1


def format_bytes(value: float | int | None) -> str:
    if not value:
        return "0 B"
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[index]}"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class TransferManager:
    def __init__(self, factory: FactoryFn, emit: EmitFn) -> None:
        self._factory = factory
        self._emit = emit
        self._tasks: list[TransferTask] = []
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._backend: RemoteBackend | None = None
        self._thread: threading.Thread | None = None
        self._active: TransferTask | None = None
        self._next_id = itertools.count(1)
        self._stopping = False
        self._samples: dict[int, tuple[float, int]] = {}
        self._last_emit = 0.0
        self._last_emit_force = 0.0

    # -- public API -------------------------------------------------------
    @property
    def active_task(self) -> TransferTask | None:
        return self._active

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="transfers", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            now = time.time()
            for task in self._tasks:
                if task.status == "queued":
                    task.status = "cancelled"
                    task.finished_at = now
                    task.cancel_event.set()
            active = self._active
            if active is not None:
                active.cancel_event.set()
            backend = self._backend
            self._backend = None
            self._cond.notify_all()
        if backend is not None:
            self._safe_close(backend)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> list[TransferTask]:
        with self._lock:
            return list(self._tasks)

    def has_pending(self) -> bool:
        with self._lock:
            return any(t.status in ("queued", "running") for t in self._tasks)

    def add_upload(self, local_paths: list[str], remote_dir: str) -> list[TransferTask]:
        tasks: list[TransferTask] = []
        for local_path in local_paths:
            if not os.path.exists(local_path):
                continue
            name = os.path.basename(local_path.rstrip("/"))
            if os.path.isdir(local_path):
                # Sizes are measured by the worker so huge folders never block
                # the UI; the row shows "Scanning folder…" until then.
                task = self._new_task(
                    "upload", "tree", local_path, remote_dir, name, 0, 0
                )
            else:
                task = self._new_task(
                    "upload", "file", local_path, remote_dir, name,
                    os.path.getsize(local_path), 1,
                )
            if task is not None:
                tasks.append(task)
        self._enqueue(tasks)
        return tasks

    def add_download(self, entries: list[RemoteEntry], local_dir: str) -> list[TransferTask]:
        tasks: list[TransferTask] = []
        for entry in entries:
            kind = "tree" if entry.is_dir else "file"
            size = 0 if entry.is_dir else entry.size
            files = 0 if entry.is_dir else 1
            task = self._new_task(
                "download", kind, entry.path, local_dir, entry.name, size, files
            )
            if task is not None:
                tasks.append(task)
        self._enqueue(tasks)
        return tasks

    def cancel(self, task_id: int) -> None:
        backend_to_close = None
        with self._cond:
            task = next((t for t in self._tasks if t.task_id == task_id), None)
            if task is None or task.status not in ("queued", "running", "failed"):
                return
            task.cancel_event.set()
            if task.status in ("queued", "failed"):
                task.status = "cancelled"
                task.error = "" if task.status == "cancelled" else task.error
                task.finished_at = time.time()
                task.speed = 0.0
            if self._active is task:
                backend_to_close, self._backend = self._backend, None
            self._cond.notify_all()
        if backend_to_close is not None:
            self._safe_close(backend_to_close)
        self._emit("changed", None)

    def retry(self, task_id: int) -> None:
        with self._cond:
            task = next((t for t in self._tasks if t.task_id == task_id), None)
            if task is None or task.status not in ("failed", "cancelled"):
                return
            task.status = "queued"
            task.error = ""
            task.transferred = 0
            task.speed = 0.0
            task.started_at = None
            task.finished_at = None
            task.files_done = 0
            task.cancel_event.clear()
            self._cond.notify_all()
        self._emit("changed", None)

    def clear_finished(self) -> None:
        with self._lock:
            self._tasks = [t for t in self._tasks if t.status in ("queued", "running")]
        self._emit("changed", None)

    def remove(self, task_id: int) -> None:
        with self._lock:
            task = next((t for t in self._tasks if t.task_id == task_id), None)
            if task is None or task.is_active:
                return
            self._tasks.remove(task)
        self._emit("changed", None)

    # -- internals --------------------------------------------------------
    def _new_task(
        self,
        direction: str,
        kind: str,
        source: str,
        destination: str,
        name: str,
        total_bytes: int,
        files_total: int,
    ) -> TransferTask | None:
        with self._lock:
            task = TransferTask(
                task_id=next(self._next_id),
                direction=direction,
                kind=kind,
                source=source,
                destination=destination,
                display_name=name,
                total_bytes=total_bytes,
                files_total=files_total,
                cancel_event=threading.Event(),
            )
            self._tasks.append(task)
            return task

    def _enqueue(self, tasks: list[TransferTask]) -> None:
        if not tasks:
            return
        with self._cond:
            self._cond.notify_all()
        self._emit("changed", None)

    def _next_queued(self) -> TransferTask | None:
        for task in self._tasks:
            if task.status == "queued":
                return task
        return None

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopping and self._next_queued() is None:
                    self._cond.wait(timeout=1.0)
                if self._stopping:
                    break
                task = self._next_queued()
                if task is None:
                    continue
                task.status = "running"
                task.started_at = time.time()
                task.finished_at = None
                task.error = ""
                task.transferred = 0
                task.speed = 0.0
                self._active = task
                self._samples[task.task_id] = (time.monotonic(), 0)
            self._emit("changed", None)

            failure: str | None = None
            cancelled = False
            try:
                backend = self._ensure_backend()
                self._execute(task, backend)
            except TransferAborted:
                cancelled = True
            except BackendError as exc:
                failure = str(exc)
                self._drop_backend()
            except Exception as exc:  # pragma: no cover - defensive
                failure = f"{type(exc).__name__}: {exc}"
                self._emit("log", ("error", traceback.format_exc()))
                self._drop_backend()
            finally:
                with self._lock:
                    if cancelled or task.cancel_event.is_set():
                        task.status = "cancelled"
                    elif failure:
                        task.status = "failed"
                        task.error = failure
                    elif task.files_total and task.files_done < task.files_total and task.kind == "tree":
                        task.status = "failed"
                        task.error = task.error or (
                            f"{task.files_total - task.files_done} of "
                            f"{task.files_total} files were not transferred"
                        )
                    else:
                        task.status = "done"
                    if task.transferred >= task.total_bytes and task.status == "done":
                        task.transferred = task.total_bytes
                    task.finished_at = time.time()
                    task.speed = 0.0
                    self._active = None
                    self._samples.pop(task.task_id, None)
                self._emit("changed", None)

            if failure:
                self._emit("log", ("error", f"{task.display_name}: {failure}"))

    def _ensure_backend(self) -> RemoteBackend:
        if self._backend is not None and self._backend.connected:
            return self._backend
        self._emit("log", ("info", "Connecting for transfer…"))
        backend = self._factory()
        self._backend = backend
        return backend

    def _drop_backend(self) -> None:
        with self._lock:
            backend, self._backend = self._backend, None
        if backend is not None:
            self._safe_close(backend)

    def _execute(self, task: TransferTask, backend: RemoteBackend) -> None:
        if task.direction == "upload":
            if task.kind == "tree":
                self._upload_tree(task, backend)
            else:
                self._upload_file(task, backend)
        else:
            if task.kind == "tree":
                self._download_tree(task, backend)
            else:
                self._download_file(task, backend)

    def _upload_file(self, task: TransferTask, backend: RemoteBackend) -> None:
        remote_path = join_remote(task.destination, os.path.basename(task.source.rstrip("/")))
        task.total_bytes = max(task.total_bytes, os.path.getsize(task.source))
        progress = self._progress_fn(task, 0, task.total_bytes)
        backend.upload_file(task.source, remote_path, progress, task.cancel_event.is_set)
        task.transferred = task.total_bytes
        task.files_done = 1
        self._emit("changed", None)

    def _upload_tree(self, task: TransferTask, backend: RemoteBackend) -> None:
        root = task.source.rstrip("/")
        base_name = os.path.basename(root)
        remote_root = join_remote(task.destination, base_name)
        backend.make_dirs(remote_root)

        total = 0
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            if task.cancel_event.is_set():
                raise TransferAborted("Upload cancelled")
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            total += sum(
                os.path.getsize(os.path.join(dirpath, f))
                for f in filenames
                if os.path.isfile(os.path.join(dirpath, f))
            )
            file_count += len(filenames)

        with self._lock:
            task.total_bytes = total
            task.files_total = file_count
        self._emit("changed", None)

        done_bytes = 0
        for dirpath, dirnames, filenames in os.walk(root):
            relative = os.path.relpath(dirpath, root)
            remote_dir = remote_root if relative == "." else posixpath.join(
                remote_root, *relative.split(os.sep)
            )
            backend.make_dirs(remote_dir)

            for dirname in sorted(dirnames):
                local_child = os.path.join(dirpath, dirname)
                if os.path.islink(local_child):
                    continue
                backend.make_dirs(posixpath.join(remote_dir, dirname))

            for filename in sorted(filenames):
                if task.cancel_event.is_set():
                    raise TransferAborted("Upload cancelled")
                local_file = os.path.join(dirpath, filename)
                if not os.path.isfile(local_file):
                    continue
                size = os.path.getsize(local_file)
                progress = self._progress_fn(task, done_bytes, size)
                try:
                    backend.upload_file(
                        local_file,
                        posixpath.join(remote_dir, filename),
                        progress,
                        task.cancel_event.is_set,
                    )
                except BackendError as exc:
                    task.error = f"{filename}: {exc}"
                    self._emit("log", ("error", f"Could not upload {filename}: {exc}"))
                else:
                    with self._lock:
                        task.files_done += 1
                done_bytes += size
                with self._lock:
                    task.transferred = done_bytes
                self._emit("changed", None)

    def _download_file(self, task: TransferTask, backend: RemoteBackend) -> None:
        local_path = os.path.join(task.destination, os.path.basename(task.source.rstrip("/")))
        progress = self._progress_fn(task, 0, task.total_bytes)
        backend.download_file(task.source, local_path, progress, task.cancel_event.is_set)
        task.transferred = task.total_bytes
        task.files_done = 1
        self._emit("changed", None)

    def _download_tree(self, task: TransferTask, backend: RemoteBackend) -> None:
        local_root = os.path.join(task.destination, task.display_name)
        entries = backend.walk_tree(task.source)
        total = sum(e.size for e in entries if not e.is_dir)
        files_total = sum(1 for e in entries if not e.is_dir)
        with self._lock:
            task.total_bytes = total
            task.files_total = files_total
            task.error = ""
        self._emit("changed", None)

        os.makedirs(local_root, exist_ok=True)
        for entry in entries:
            if task.cancel_event.is_set():
                raise TransferAborted("Download cancelled")
            relative = posixpath.relpath(entry.path, task.source)
            parts = relative.split("/")
            local_path = os.path.join(local_root, *parts)
            if entry.is_dir and not entry.is_link:
                os.makedirs(local_path, exist_ok=True)
                continue
            if entry.is_link and entry.is_dir:
                continue
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            base = task.transferred
            progress = self._progress_fn(task, base, entry.size)
            try:
                backend.download_file(
                    entry.path, local_path, progress, task.cancel_event.is_set
                )
            except BackendError as exc:
                task.error = f"{entry.name}: {exc}"
                self._emit("log", ("error", f"Could not download {entry.name}: {exc}"))
            else:
                with self._lock:
                    task.files_done += 1
            with self._lock:
                task.transferred = base + entry.size
            self._emit("changed", None)

    def _progress_fn(self, task: TransferTask, base: int, file_total: int):
        def report(done: int, total: int) -> None:
            with self._lock:
                task.transferred = base + done
            self._sample_speed(task)
            self._throttled_emit()

        return report

    def _sample_speed(self, task: TransferTask) -> None:
        now = time.monotonic()
        with self._lock:
            previous = self._samples.get(task.task_id)
            if previous is None:
                self._samples[task.task_id] = (now, task.transferred)
                return
            last_time, last_bytes = previous
            elapsed = now - last_time
            if elapsed < 0.4:
                return
            instant = max(0.0, (task.transferred - last_bytes) / elapsed)
            task.speed = instant if task.speed <= 0 else (task.speed * 0.6 + instant * 0.4)
            self._samples[task.task_id] = (now, task.transferred)

    def _throttled_emit(self) -> None:
        now = time.monotonic()
        if now - self._last_emit >= MAX_EMIT_INTERVAL:
            self._last_emit = now
            self._emit("changed", None)

    @staticmethod
    def _safe_close(backend: RemoteBackend) -> None:
        try:
            backend.close()
        except Exception:
            pass

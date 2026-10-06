"""Job manager: runs offload / verify jobs on a QThreadPool.

* Global concurrency cap (configurable, default 2).
* Jobs reading from the same physical volume are serialised, because two
  readers on one SD card thrash the reader and halve throughput.
* System sleep is inhibited while any job is active.
"""

from __future__ import annotations

import itertools
import logging
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from lumdit.core import devices
from lumdit.core.hashing import Cancelled
from lumdit.core.mhl import VerifyResult, manifest_total_bytes, verify_manifest
from lumdit.core.offload import OffloadEngine, OffloadProgress, OffloadRequest, OffloadResult, wait_while_paused

_job_ids = itertools.count(1)
log = logging.getLogger(__name__)


class SleepInhibitor:
    """Keeps the machine awake while copies run (Windows + macOS)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._active = False

    def acquire(self) -> None:
        if self._active:
            return
        self._active = True
        if sys.platform.startswith("win"):
            import ctypes

            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            try:
                self._proc = subprocess.Popen(["caffeinate", "-i", "-s"])
            except OSError:
                self._proc = None

    def release(self) -> None:
        if not self._active:
            return
        self._active = False
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # type: ignore[attr-defined]
        elif self._proc is not None:
            self._proc.terminate()
            self._proc = None


class JobSignals(QObject):
    progress = Signal(int, object)  # job_id, OffloadProgress
    finished = Signal(int, object)  # job_id, OffloadResult | VerifyResult
    failed = Signal(int, str)  # job_id, message


@dataclass
class Job:
    id: int
    kind: str  # "offload" | "verify"
    title: str
    device_key: str
    payload: Any  # OffloadRequest | Path (manifest)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"  # queued | running | done | failed | cancelled
    progress: OffloadProgress = field(default_factory=OffloadProgress)
    result: Any = None
    message: str = ""

    @property
    def paused(self) -> bool:
        """Pause requested (worker blocks at the next chunk). Only meaningful while active."""
        return self.pause_event.is_set() and self.state in ("queued", "running")

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


class _OffloadRunnable(QRunnable):
    def __init__(self, job: Job, signals: JobSignals) -> None:
        super().__init__()
        self.job = job
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        job = self.job
        try:
            engine = OffloadEngine(
                job.payload,
                progress=lambda p: self.signals.progress.emit(job.id, _snapshot(p)),
                cancel_event=job.cancel_event,
                pause_event=job.pause_event,
            )
            result = engine.run()
            self.signals.finished.emit(job.id, result)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Offload job %s (%s) crashed", job.id, job.title)
            self.signals.failed.emit(job.id, f"{exc.__class__.__name__}: {exc}")


class _VerifyRunnable(QRunnable):
    def __init__(self, job: Job, signals: JobSignals) -> None:
        super().__init__()
        self.job = job
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        job = self.job
        manifest: Path = job.payload
        prog = OffloadProgress(phase="verifying")
        try:
            prog.bytes_total = manifest_total_bytes(manifest)
            self.signals.progress.emit(job.id, _snapshot(prog))

            def on_progress(n: int, rel: str) -> None:
                prog.bytes_done += n
                prog.current_file = rel
                self.signals.progress.emit(job.id, _snapshot(prog))

            def on_pause(paused: bool) -> None:
                prog.paused = paused
                self.signals.progress.emit(job.id, _snapshot(prog))

            def gate() -> bool:
                wait_while_paused(job.pause_event, job.cancel_event, on_pause)
                return job.cancel_event.is_set()

            result = verify_manifest(manifest, on_progress, gate)
            prog.phase = "done"
            self.signals.progress.emit(job.id, _snapshot(prog))
            self.signals.finished.emit(job.id, result)
        except Cancelled:
            prog.phase = "cancelled"
            self.signals.progress.emit(job.id, _snapshot(prog))
            self.signals.failed.emit(job.id, "Cancelled by user")
        except Exception as exc:
            log.exception("Verify job %s (%s) failed", job.id, manifest)
            self.signals.failed.emit(job.id, f"{exc.__class__.__name__}: {exc}")


def _snapshot(p: OffloadProgress) -> OffloadProgress:
    return OffloadProgress(**{k: getattr(p, k) for k in p.__dataclass_fields__})


class JobManager(QObject):
    job_added = Signal(object)  # Job
    job_updated = Signal(object)  # Job
    job_finished = Signal(object)  # Job

    def __init__(self, max_concurrent: int = 2, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self._max_concurrent = max(1, max_concurrent)
        self.pool.setMaxThreadCount(self._max_concurrent)
        self.signals = JobSignals()
        self.signals.progress.connect(self._on_progress)
        self.signals.finished.connect(self._on_finished)
        self.signals.failed.connect(self._on_failed)
        self.jobs: dict[int, Job] = {}
        self._waiting: dict[str, deque[Job]] = {}
        self._busy_devices: set[str] = set()
        self._sleep = SleepInhibitor()

    # ---- config ------------------------------------------------------------
    def set_max_concurrent(self, n: int) -> None:
        self._max_concurrent = max(1, n)
        self._sync_pool_size()

    def _sync_pool_size(self) -> None:
        # A paused job still occupies its pool thread (it is blocked inside the engine), so
        # widen the pool by the number of paused workers to let other jobs keep going.
        paused_running = sum(1 for j in self.jobs.values() if j.state == "running" and j.pause_event.is_set())
        self.pool.setMaxThreadCount(self._max_concurrent + paused_running)

    def _sync_sleep(self) -> None:
        if any(j.state == "running" and not j.pause_event.is_set() for j in self.jobs.values()):
            self._sleep.acquire()
        else:
            self._sleep.release()

    # ---- submit ------------------------------------------------------------
    def submit_offload(self, request: OffloadRequest) -> Job:
        job = Job(
            id=next(_job_ids),
            kind="offload",
            title=request.title,
            device_key=devices.device_key(request.source),
            payload=request,
        )
        return self._enqueue(job)

    def submit_verify(self, manifest: Path) -> Job:
        job = Job(
            id=next(_job_ids),
            kind="verify",
            title=f"Verify {manifest.parent.name}",
            device_key=devices.device_key(manifest),
            payload=manifest,
        )
        return self._enqueue(job)

    def _enqueue(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self.job_added.emit(job)
        if job.device_key in self._busy_devices:
            self._waiting.setdefault(job.device_key, deque()).append(job)
            job.message = "Waiting for another job on the same card"
            self.job_updated.emit(job)
        else:
            self._start(job)
        return job

    def _start(self, job: Job) -> None:
        self._busy_devices.add(job.device_key)
        job.state = "running"
        job.message = "Paused" if job.pause_event.is_set() else ""
        self._sync_pool_size()
        self._sync_sleep()
        self.job_updated.emit(job)
        runnable = _OffloadRunnable(job, self.signals) if job.kind == "offload" else _VerifyRunnable(job, self.signals)
        self.pool.start(runnable)

    def cancel(self, job_id: int) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.cancel_event.set()
        if job.state == "queued":
            q = self._waiting.get(job.device_key)
            if q and job in q:
                q.remove(job)
            job.state = "cancelled"
            job.message = "Cancelled"
            self.job_finished.emit(job)

    def cancel_all(self) -> None:
        for jid in list(self.jobs):
            self.cancel(jid)

    # ---- pause / resume ----------------------------------------------------
    def pause(self, job_id: int) -> None:
        """Pause a running or queued job. The worker blocks at the next chunk boundary with its
        files still open, so resuming continues exactly where it left off."""
        job = self.jobs.get(job_id)
        if not job or not job.active or job.pause_event.is_set():
            return
        job.pause_event.set()
        job.progress.paused = True
        if job.state == "queued":
            job.message = "Paused (will not start until resumed)"
        log.info("Job %s paused: %s", job.id, job.title)
        self._sync_pool_size()
        self._sync_sleep()
        self.job_updated.emit(job)

    def resume(self, job_id: int) -> None:
        job = self.jobs.get(job_id)
        if not job or not job.pause_event.is_set():
            return
        job.pause_event.clear()
        job.progress.paused = False
        if job.state == "queued":
            job.message = "Waiting for another job on the same card" if job.device_key in self._busy_devices else ""
        log.info("Job %s resumed: %s", job.id, job.title)
        self._sync_pool_size()
        self._sync_sleep()
        self.job_updated.emit(job)
        if job.state == "queued" and job.device_key not in self._busy_devices:
            q = self._waiting.get(job.device_key)
            if q and job in q:
                q.remove(job)
            self._start(job)

    def pause_all(self) -> None:
        for job in list(self.jobs.values()):
            self.pause(job.id)

    def resume_all(self) -> None:
        for job in list(self.jobs.values()):
            self.resume(job.id)

    @property
    def paused_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.paused)

    @property
    def active_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.state in ("running", "queued"))

    # ---- slots -------------------------------------------------------------
    @Slot(int, object)
    def _on_progress(self, job_id: int, progress: OffloadProgress) -> None:
        job = self.jobs.get(job_id)
        if job:
            # The manager's pause flag is authoritative; a snapshot emitted just before the
            # worker reached the gate would otherwise flicker the UI.
            progress.paused = job.pause_event.is_set()
            job.progress = progress
            self.job_updated.emit(job)

    @Slot(int, object)
    def _on_finished(self, job_id: int, result: object) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.result = result
        if isinstance(result, OffloadResult):
            job.state = {"verified": "done", "issues": "done", "cancelled": "cancelled"}.get(result.status, "failed")
            job.message = result.message
        elif isinstance(result, VerifyResult):
            job.state = "done" if result.passed else "failed"
            job.message = (
                f"{result.ok}/{result.checked} files OK"
                if result.passed
                else f"{len(result.issues)} problem(s) in {result.checked} files"
            )
        self._release(job)

    @Slot(int, str)
    def _on_failed(self, job_id: int, message: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.state = "cancelled" if job.cancel_event.is_set() else "failed"
        job.message = message
        self._release(job)

    def _release(self, job: Job) -> None:
        self._busy_devices.discard(job.device_key)
        job.pause_event.clear()
        job.progress.paused = False
        self.job_finished.emit(job)
        q = self._waiting.get(job.device_key)
        if q:
            # Skip over paused queued jobs; they start when the user resumes them.
            for nxt in list(q):
                if not nxt.pause_event.is_set():
                    q.remove(nxt)
                    self._start(nxt)
                    break
        self._sync_pool_size()
        self._sync_sleep()

    def shutdown(self) -> None:
        self.cancel_all()
        self.pool.waitForDone(5000)
        self._sleep.release()

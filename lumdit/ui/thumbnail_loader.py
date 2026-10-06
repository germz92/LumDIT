"""Qt wrapper that produces thumbnails on a background pool and emits QImages."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage

from lumdit.core.thumbnails import ThumbnailCache, thumbnail_file


class _Signals(QObject):
    ready = Signal(str, int, QImage)  # path, generation, image
    failed = Signal(str, int)


class _Task(QRunnable):
    def __init__(self, path: Path, size: int, cache: ThumbnailCache, generation: int, signals: _Signals) -> None:
        super().__init__()
        self.path, self.size, self.cache, self.generation, self.signals = path, size, cache, generation, signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            out = thumbnail_file(self.path, self.size, self.cache)
            if out is None:
                self.signals.failed.emit(str(self.path), self.generation)
                return
            img = QImage(str(out))
            if img.isNull():
                self.signals.failed.emit(str(self.path), self.generation)
            else:
                self.signals.ready.emit(str(self.path), self.generation, img)
        except Exception:
            self.signals.failed.emit(str(self.path), self.generation)


class ThumbnailLoader(QObject):
    ready = Signal(str, QImage)
    failed = Signal(str)

    def __init__(self, cache_dir: Path, threads: int = 3, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache = ThumbnailCache(cache_dir)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, threads))
        self.pool.setThreadPriority(QThread.Priority.LowPriority)
        self._signals = _Signals()
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self._on_failed)
        self._generation = 0
        self._pending: set[str] = set()

    def reset(self) -> None:
        """Invalidate outstanding requests (e.g. when the folder changes)."""
        self._generation += 1
        self._pending.clear()
        self.pool.clear()

    def request(self, path: Path, size: int) -> None:
        key = str(path)
        if key in self._pending:
            return
        self._pending.add(key)
        self.pool.start(_Task(path, size, self.cache, self._generation, self._signals))

    def _on_ready(self, path: str, generation: int, image: QImage) -> None:
        self._pending.discard(path)
        if generation == self._generation:
            self.ready.emit(path, image)

    def _on_failed(self, path: str, generation: int) -> None:
        self._pending.discard(path)
        if generation == self._generation:
            self.failed.emit(path)

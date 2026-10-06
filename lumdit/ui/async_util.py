"""Run blocking calls (MongoDB, scans) on the global thread pool and deliver results on the UI thread."""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set["_Task"] = set()  # keep tasks alive until they finish


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self.fn = fn
        self.signals = signals
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            logging.getLogger(__name__).warning(
                "Background task %s failed: %s: %s", getattr(self.fn, "__qualname__", self.fn), exc.__class__.__name__, exc,
                exc_info=True,
            )
            self.signals.error.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.done.emit(result)
        finally:
            _live.discard(self)


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    signals = _Signals()
    if on_done is not None:
        signals.done.connect(on_done)
    if on_error is not None:
        signals.error.connect(on_error)
    task = _Task(fn, signals)
    task._signals_ref = signals  # type: ignore[attr-defined]
    _live.add(task)
    QThreadPool.globalInstance().start(task)

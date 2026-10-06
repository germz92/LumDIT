"""Application log file with rotating history and crash capture.

Location
--------
Windows : %LOCALAPPDATA%\\LumDIT\\lumdit.log
macOS   : ~/Library/Logs/LumDIT/lumdit.log
other   : ~/.lumdit/lumdit.log

Set ``LUMDIT_DEBUG=1`` for DEBUG-level output. Uncaught exceptions on the main thread,
worker threads and inside Qt slots are written with full tracebacks. MongoDB connection
strings are redacted before anything reaches the file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
from pathlib import Path

from lumdit import APP_NAME, __version__

LOG_NAME = "lumdit.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 5
_URI_RE = re.compile(r"(mongodb(?:\+srv)?://)([^/\s@]+)@", re.IGNORECASE)

_installed = False


def log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".lumdit")


def log_path() -> Path:
    return log_dir() / LOG_NAME


def redact(text: str) -> str:
    """Strip credentials from anything that looks like a MongoDB URI."""
    return _URI_RE.sub(r"\1***@", text)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int | None = None) -> Path | None:
    """Install the rotating file handler and crash hooks. Idempotent. Returns the log path."""
    global _installed
    if _installed:
        return log_path()
    _installed = True

    if level is None:
        level = logging.DEBUG if os.environ.get("LUMDIT_DEBUG") else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = _RedactingFormatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")

    path: Path | None = None
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        path = log_path()
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        path = None
    if not getattr(sys, "frozen", False) and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)

    # Quieten chatty third parties.
    for noisy in ("pymongo", "PIL", "rawpy", "av"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_crash_hooks()
    logging.getLogger(__name__).info(
        "%s %s starting | python %s | %s %s (%s) | frozen=%s | log=%s",
        APP_NAME,
        __version__,
        platform.python_version(),
        platform.system(),
        platform.release(),
        platform.machine(),
        bool(getattr(sys, "frozen", False)),
        path,
    )
    return path


def _install_crash_hooks() -> None:
    log = logging.getLogger("lumdit.crash")

    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_hook

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        qt_log = logging.getLogger("qt")
        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def qt_handler(msg_type, context, message) -> None:
            qt_log.log(levels.get(msg_type, logging.WARNING), message)

        qInstallMessageHandler(qt_handler)
    except Exception:  # pragma: no cover - Qt not available in some test contexts
        pass

"""Application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from lumdit import APP_NAME, ORG_NAME, __version__
from lumdit.logging_setup import setup_logging


def app_icon() -> QIcon:
    """Bundled app icon (lumdit/resources/lumdit.png), with a theme fallback."""
    path = Path(__file__).with_name("resources") / "lumdit.png"
    if path.is_file():
        return QIcon(str(path))
    return QIcon.fromTheme("camera-photo")


def _set_windows_app_id() -> None:
    """Give the process its own taskbar identity on Windows.

    Without this, Windows groups the window under the host executable's AppUserModelID
    (python.exe when running from source) and shows *that* exe's icon in the taskbar
    instead of the window icon. Must run before any window is created.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{ORG_NAME}.{APP_NAME}")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - cosmetic only
        logging.getLogger(__name__).debug("Could not set AppUserModelID", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    setup_logging()
    _set_windows_app_id()
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())

    from lumdit.ui.main_window import MainWindow

    window = MainWindow()
    # Optional: open a production folder passed on the command line.
    for arg in argv[1:]:
        if not arg.startswith("-"):
            window.open_production_path(arg)
            break
    window.show()
    code = app.exec()
    logging.getLogger(__name__).info("%s exiting with code %s", APP_NAME, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

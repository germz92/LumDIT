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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    setup_logging()
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

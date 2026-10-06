"""Small UI helpers shared across widgets."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import QStyle, QWidget


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_rate(bps: float) -> str:
    return f"{human_size(bps)}/s" if bps > 0 else "-"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def open_with_system(path: Path | str) -> bool:
    """Open a file/folder with the OS default application (system player/viewer)."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal_in_file_manager(path: Path | str) -> None:
    path = Path(path)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            open_with_system(path.parent if path.is_file() else path)
    except OSError:
        open_with_system(path.parent if path.is_file() else path)


def std_icon(widget: QWidget, sp: QStyle.StandardPixmap):
    return widget.style().standardIcon(sp)


def muted_hex(widget: QWidget) -> str:
    """A secondary-text colour that stays readable in both light and dark themes."""
    pal = widget.palette()
    text = pal.color(QPalette.ColorRole.WindowText)
    window = pal.color(QPalette.ColorRole.Window)
    mix = QColor(
        int(text.red() * 0.6 + window.red() * 0.4),
        int(text.green() * 0.6 + window.green() * 0.4),
        int(text.blue() * 0.6 + window.blue() * 0.4),
    )
    return mix.name()


def muted_css(widget: QWidget) -> str:
    return f"color: {muted_hex(widget)};"

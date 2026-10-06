"""Drag-and-drop target for card folders."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QVBoxLayout, QWidget

from lumdit.ui.util import muted_css

_BASE = """
QFrame#dropZone {{
    border: 2px dashed {border};
    border-radius: 10px;
    background: {bg};
}}
"""


class DropZone(QFrame):
    folders_dropped = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumSize(200, 120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel("<b>Drop card folder here to offload</b>")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel("Drag a folder from a card (or the browser above),\nor click to choose a folder.")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet(muted_css(self))
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        self._armed(False)

    def set_enabled_state(self, enabled: bool, reason: str = "") -> None:
        self.setEnabled(enabled)
        self.hint.setText(
            reason if (not enabled and reason) else
            "Drag a folder from a card (or the browser above),\nor click to choose a folder."
        )

    def _armed(self, on: bool) -> None:
        self.setStyleSheet(
            _BASE.format(
                border="palette(highlight)" if on else "palette(mid)",
                bg="palette(alternate-base)" if on else "transparent",
            )
        )

    @staticmethod
    def _folders(event) -> list[Path]:
        md = event.mimeData()
        if not md.hasUrls():
            return []
        out = []
        for url in md.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_dir():
                    out.append(p)
        return out

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.isEnabled() and self._folders(event):
            event.acceptProposedAction()
            self._armed(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._armed(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._armed(False)
        folders = self._folders(event)
        if folders:
            event.acceptProposedAction()
            self.folders_dropped.emit(folders)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            chosen = QFileDialog.getExistingDirectory(self, "Choose a card folder to offload")
            if chosen:
                self.folders_dropped.emit([Path(chosen)])
        super().mouseReleaseEvent(event)

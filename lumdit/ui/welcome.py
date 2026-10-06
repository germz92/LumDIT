"""Start page shown until a production is created or opened."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lumdit import APP_NAME, __version__
from lumdit.core.production import Production
from lumdit.ui.util import muted_hex


def _label(path: str) -> str:
    try:
        return Production.load(Path(path)).display_name
    except Exception:
        return Path(path).name


class WelcomePage(QWidget):
    create_requested = Signal()
    open_requested = Signal()
    open_recent = Signal(object)  # Path
    event_backup_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box = QVBoxLayout()
        box.setSpacing(12)
        title = QLabel(
            f"<h1 style='margin:0'>{APP_NAME}</h1>"
            f"<span style='color: {muted_hex(self)}'>v{__version__} - verified media offload</span>"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(title)

        btns = QHBoxLayout()
        self.event_btn = QPushButton("Start Event Backup...")
        self.event_btn.setMinimumSize(200, 48)
        self.event_btn.setDefault(True)
        self.event_btn.setToolTip("Pick an event from the crew app's card log; LumDIT walks you through each card")
        self.event_btn.clicked.connect(self.event_backup_requested)
        self.create_btn = QPushButton("Create Production")
        self.create_btn.setMinimumSize(200, 48)
        self.create_btn.clicked.connect(self.create_requested)
        self.open_btn = QPushButton("Open Production...")
        self.open_btn.setMinimumSize(200, 48)
        self.open_btn.clicked.connect(self.open_requested)
        btns.addWidget(self.event_btn)
        btns.addWidget(self.create_btn)
        btns.addWidget(self.open_btn)
        box.addLayout(btns)

        self.recent_label = QLabel("<b>Recent productions</b>")
        box.addWidget(self.recent_label)
        self.recent = QListWidget()
        self.recent.setMinimumWidth(440)
        self.recent.setMaximumHeight(200)
        self.recent.itemActivated.connect(self._recent_chosen)
        box.addWidget(self.recent)
        outer.addLayout(box)

    def set_recent(self, paths: list[str]) -> None:
        self.recent.clear()
        for p in paths:
            item = QListWidgetItem(f"{_label(p)}   -   {p}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.recent.addItem(item)
        has = bool(paths)
        self.recent.setVisible(has)
        self.recent_label.setVisible(has)

    def _recent_chosen(self, item: QListWidgetItem) -> None:
        self.open_recent.emit(Path(item.data(Qt.ItemDataRole.UserRole)))

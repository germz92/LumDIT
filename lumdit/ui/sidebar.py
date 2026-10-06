"""Left sidebar: removable devices list + Explorer/Finder style folder tree."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from lumdit.core import devices
from lumdit.ui.util import human_size


class DeviceList(QListWidget):
    """Shows removable volumes (SD cards, readers, shuttle drives) with eject."""

    volume_selected = Signal(object)  # Volume
    offload_requested = Signal(object)  # Path
    volume_added = Signal(object)  # Volume newly detected since the previous poll
    volume_removed = Signal(object)  # Volume

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemClicked.connect(self._clicked)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._volumes: list[devices.Volume] = []
        self._primed = False
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        vols = devices.removable_volumes()
        old = {v.mountpoint: v for v in self._volumes}
        new = {v.mountpoint: v for v in vols}
        if list(old) == list(new):
            # Update free-space text without rebuilding.
            for i, v in enumerate(vols):
                if i < self.count():
                    self.item(i).setText(self._label(v))
            self._volumes = vols
            self._primed = True
            return
        added = [v for mp, v in new.items() if mp not in old]
        removed = [v for mp, v in old.items() if mp not in new]
        self._volumes = vols
        self.clear()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveFDIcon)
        for v in vols:
            item = QListWidgetItem(icon, self._label(v))
            item.setToolTip(f"{v.mountpoint}\n{v.fstype}\n{human_size(v.free)} free of {human_size(v.total)}")
            item.setData(Qt.ItemDataRole.UserRole, v.mountpoint)
            self.addItem(item)
        if not vols:
            placeholder = QListWidgetItem("No cards detected")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(placeholder)
        self.setFixedHeight(max(1, self.count()) * 26 + 6)
        # Only announce volumes that appear after start-up, not the ones already mounted.
        if self._primed:
            for v in added:
                self.volume_added.emit(v)
            for v in removed:
                self.volume_removed.emit(v)
        self._primed = True

    @staticmethod
    def _label(v: devices.Volume) -> str:
        return f"{v.display_name}  -  {human_size(v.total - v.free)} used"

    def _volume_for_item(self, item: QListWidgetItem | None) -> devices.Volume | None:
        if item is None:
            return None
        mp = item.data(Qt.ItemDataRole.UserRole)
        return next((v for v in self._volumes if v.mountpoint == mp), None)

    def _clicked(self, item: QListWidgetItem) -> None:
        v = self._volume_for_item(item)
        if v:
            self.volume_selected.emit(v)

    def _menu(self, pos) -> None:
        item = self.itemAt(pos)
        v = self._volume_for_item(item)
        if not v:
            return
        menu = QMenu(self)
        act_browse = menu.addAction("Browse")
        act_offload = menu.addAction("Offload entire card...")
        menu.addSeparator()
        act_eject = menu.addAction("Eject")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == act_browse:
            self.volume_selected.emit(v)
        elif chosen == act_offload:
            self.offload_requested.emit(v.path)
        elif chosen == act_eject:
            self.eject(v)

    def eject(self, v: devices.Volume) -> None:
        ok, msg = devices.eject(v)
        if ok:
            QTimer.singleShot(500, self.refresh)
        else:
            QMessageBox.warning(self, "Eject failed", msg or "Could not eject the volume. Make sure no files are open.")


class Sidebar(QWidget):
    folder_selected = Signal(object)  # Path
    offload_requested = Signal(object)  # Path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Cards &amp; Devices</b>"))
        header.addStretch(1)
        self.eject_btn = QPushButton("Eject")
        self.eject_btn.setToolTip("Safely eject the selected card")
        self.eject_btn.clicked.connect(self._eject_selected)
        header.addWidget(self.eject_btn)
        layout.addLayout(header)

        self.devices = DeviceList(self)
        self.devices.volume_selected.connect(lambda v: self._goto(v.path))
        self.devices.offload_requested.connect(self.offload_requested)
        layout.addWidget(self.devices)

        layout.addWidget(QLabel("<b>Folders</b>"))
        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.model.setReadOnly(True)
        self.model.setRootPath("")

        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(""))
        for col in range(1, self.model.columnCount()):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.selectionModel().currentChanged.connect(self._current_changed)
        layout.addWidget(self.tree, 1)

        if sys.platform == "darwin":
            vol = self.model.index("/Volumes")
            if vol.isValid():
                self.tree.expand(vol)
        else:
            home = self.model.index(str(Path.home()))
            if home.isValid():
                self.tree.expand(home.parent())

    # ---- api --------------------------------------------------------------
    def select_path(self, path: Path) -> None:
        idx = self.model.index(str(path))
        if idx.isValid():
            self.tree.blockSignals(True)
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)
            self.tree.blockSignals(False)
            self.tree.expand(idx)

    def current_path(self) -> Path | None:
        idx = self.tree.currentIndex()
        return Path(self.model.filePath(idx)) if idx.isValid() else None

    # ---- internals ---------------------------------------------------------
    def _goto(self, path: Path) -> None:
        self.select_path(path)
        self.folder_selected.emit(path)

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self.folder_selected.emit(Path(self.model.filePath(current)))

    def _eject_selected(self) -> None:
        item = self.devices.currentItem()
        v = self.devices._volume_for_item(item)
        if v is None:
            path = self.current_path()
            v = devices.volume_for_path(path) if path else None
            if v is None or not v.removable:
                QMessageBox.information(self, "Eject", "Select a card in the device list first.")
                return
        self.devices.eject(v)

    def _tree_menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        path = Path(self.model.filePath(idx))
        menu = QMenu(self)
        act_offload = menu.addAction("Offload this folder...")
        act_reveal = menu.addAction("Reveal in Explorer" if sys.platform.startswith("win") else "Reveal in Finder")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == act_offload:
            self.offload_requested.emit(path)
        elif chosen == act_reveal:
            from lumdit.ui.util import reveal_in_file_manager

            reveal_in_file_manager(path)

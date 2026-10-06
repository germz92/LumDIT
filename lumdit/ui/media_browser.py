"""Icon-view media browser with lazy thumbnails and hand-off to the system player."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.offload import is_junk
from lumdit.core.thumbnails import MEDIA_EXTS, SIDECAR_EXTS, media_kind
from lumdit.ui.thumbnail_loader import ThumbnailLoader
from lumdit.ui.util import human_size, open_with_system, reveal_in_file_manager


@dataclass
class Entry:
    path: Path
    is_dir: bool
    size: int
    kind: str  # "folder" | "image" | "raw" | "video" | "audio" | "other"


class FolderModel(QAbstractListModel):
    def __init__(self, loader: ThumbnailLoader, icon_size: int, parent=None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.icon_size = icon_size
        self.entries: list[Entry] = []
        self._thumbs: dict[str, QIcon] = {}
        self._failed: set[str] = set()
        self._generic: dict[str, QIcon] = {}
        loader.ready.connect(self._thumb_ready)
        loader.failed.connect(self._thumb_failed)

    def set_generic_icons(self, icons: dict[str, QIcon]) -> None:
        self._generic = icons

    def set_entries(self, entries: list[Entry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self._thumbs.clear()
        self._failed.clear()
        self.loader.reset()
        self.endResetModel()

    def set_icon_size(self, size: int) -> None:
        if size == self.icon_size:
            return
        self.icon_size = size
        self._thumbs.clear()
        self._failed.clear()
        self.loader.reset()
        if self.entries:
            self.dataChanged.emit(self.index(0), self.index(len(self.entries) - 1))

    # ---- Qt model API -------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.entries):
            return None
        e = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return e.path.name
        if role == Qt.ItemDataRole.DecorationRole:
            key = str(e.path)
            if key in self._thumbs:
                return self._thumbs[key]
            if not e.is_dir and e.kind in ("image", "raw", "video") and key not in self._failed:
                self.loader.request(e.path, self.icon_size)
            return self._generic.get("folder" if e.is_dir else e.kind, self._generic.get("other"))
        if role == Qt.ItemDataRole.ToolTipRole:
            if e.is_dir:
                return str(e.path)
            return f"{e.path.name}\n{human_size(e.size)}\n{e.path}"
        if role == Qt.ItemDataRole.UserRole:
            return e
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid():
            base |= Qt.ItemFlag.ItemIsDragEnabled
        return base

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(self.entries[i.row()].path)) for i in indexes if i.isValid()])
        return md

    # ---- thumbnails -----------------------------------------------------------
    def _thumb_ready(self, path: str, image: QImage) -> None:
        self._thumbs[path] = QIcon(QPixmap.fromImage(image))
        self._refresh(path)

    def _thumb_failed(self, path: str) -> None:
        self._failed.add(path)

    def _refresh(self, path: str) -> None:
        for row, e in enumerate(self.entries):
            if str(e.path) == path:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                return


class MediaBrowser(QWidget):
    folder_activated = Signal(object)  # Path - user navigated into a folder
    offload_requested = Signal(object)  # Path

    def __init__(self, loader: ThumbnailLoader, icon_size: int = 160, show_hidden: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.show_hidden = show_hidden
        self.current: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.up_btn = QToolButton()
        self.up_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.up_btn.setToolTip("Up one folder")
        self.up_btn.clicked.connect(self.go_up)
        bar.addWidget(self.up_btn)
        self.path_label = QLabel("Select a folder or card on the left")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.path_label, 1)
        self.count_label = QLabel("")
        bar.addWidget(self.count_label)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(64, 320)
        self.size_slider.setValue(icon_size)
        self.size_slider.setFixedWidth(120)
        self.size_slider.setToolTip("Thumbnail size")
        self.size_slider.valueChanged.connect(self._size_changed)
        bar.addWidget(self.size_slider)
        layout.addLayout(bar)

        self.model = FolderModel(loader, icon_size, self)
        st = self.style()
        self.model.set_generic_icons(
            {
                "folder": st.standardIcon(QStyle.StandardPixmap.SP_DirIcon),
                "image": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
                "raw": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
                "video": st.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
                "audio": st.standardIcon(QStyle.StandardPixmap.SP_MediaVolume),
                "other": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
            }
        )

        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setWrapping(True)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(8)
        self.view.setWordWrap(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.doubleClicked.connect(self._activated)
        self._apply_icon_size(icon_size)
        layout.addWidget(self.view, 1)

    # ---- navigation -----------------------------------------------------------
    def set_folder(self, path: Path | None) -> None:
        self.current = Path(path) if path else None
        entries: list[Entry] = []
        if self.current is None:
            self.path_label.setText("Select a folder or card on the left")
        else:
            self.path_label.setText(str(self.current))
            try:
                with os.scandir(self.current) as it:
                    for de in it:
                        name = de.name
                        if not self.show_hidden and (name.startswith(".") or is_junk(name)):
                            continue
                        try:
                            is_dir = de.is_dir(follow_symlinks=False)
                            size = 0 if is_dir else de.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
                        p = Path(de.path)
                        ext = p.suffix.lower()
                        if not is_dir and not self.show_hidden and ext not in MEDIA_EXTS and ext not in SIDECAR_EXTS:
                            # Hide camera database junk but keep sidecars and media.
                            if ext in {".bin", ".dat", ".idx", ".db", ".ini", ".inf"}:
                                continue
                        entries.append(Entry(p, is_dir, size, "folder" if is_dir else media_kind(p)))
            except OSError as exc:
                self.path_label.setText(f"{self.current}  -  cannot read: {exc.strerror or exc}")
        entries.sort(key=lambda e: (not e.is_dir, e.path.name.lower()))
        self.model.set_entries(entries)
        n_dirs = sum(1 for e in entries if e.is_dir)
        n_files = len(entries) - n_dirs
        total = sum(e.size for e in entries if not e.is_dir)
        self.count_label.setText(f"{n_dirs} folders, {n_files} files, {human_size(total)}" if entries else "")

    def go_up(self) -> None:
        if self.current and self.current.parent != self.current:
            self.folder_activated.emit(self.current.parent)

    def refresh(self) -> None:
        self.set_folder(self.current)

    # ---- internals --------------------------------------------------------------
    def _apply_icon_size(self, size: int) -> None:
        self.view.setIconSize(QSize(size, size))
        self.view.setGridSize(QSize(size + 24, size + 44))
        self.model.set_icon_size(size)

    def _size_changed(self, value: int) -> None:
        self._apply_icon_size(value)

    def _activated(self, index: QModelIndex) -> None:
        e: Entry = index.data(Qt.ItemDataRole.UserRole)
        if e is None:
            return
        if e.is_dir:
            self.folder_activated.emit(e.path)
        else:
            open_with_system(e.path)

    def _menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        menu = QMenu(self)
        e: Entry | None = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        act_open = act_reveal = act_offload = None
        if e is not None:
            act_open = menu.addAction("Open" if e.is_dir else "Open with system player/viewer")
            act_reveal = menu.addAction("Reveal in Explorer" if sys.platform.startswith("win") else "Reveal in Finder")
            if e.is_dir:
                menu.addSeparator()
                act_offload = menu.addAction("Offload this folder...")
        elif self.current is not None:
            act_offload = menu.addAction(f"Offload '{self.current.name}'...")
        act_refresh = menu.addAction("Refresh")
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_open and e is not None:
            self._activated(index)
        elif chosen == act_reveal and e is not None:
            reveal_in_file_manager(e.path)
        elif chosen == act_offload:
            self.offload_requested.emit(e.path if e is not None else self.current)
        elif chosen == act_refresh:
            self.refresh()

"""Permanent bottom panel: the production's folder tree with verified-folder badges."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.production import Production
from lumdit.ui.util import muted_hex, open_with_system, reveal_in_file_manager


class ProductionFsModel(QFileSystemModel):
    """QFileSystemModel that marks folders containing an MHL manifest as verified."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._verified_cache: dict[str, bool] = {}
        self._badge_icon: QIcon | None = None

    def invalidate(self) -> None:
        self._verified_cache.clear()
        root = self.index(self.rootPath())
        if root.isValid():
            self.dataChanged.emit(root, root)
            self.layoutChanged.emit()

    def is_verified_folder(self, path: str) -> bool:
        v = self._verified_cache.get(path)
        if v is None:
            try:
                v = any(name.lower().endswith(".mhl") for name in QDir(path).entryList(QDir.Filter.Files))
            except Exception:
                v = False
            self._verified_cache[path] = v
        return v

    def _badge(self, base: QIcon) -> QIcon:
        size = 16
        pm = base.pixmap(size, size)
        if pm.isNull():
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(46, 160, 67))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(size - 9, size - 9, 9, 9)
        painter.setPen(QColor("white"))
        painter.drawLine(size - 7, size - 5, size - 5, size - 3)
        painter.drawLine(size - 5, size - 3, size - 2, size - 8)
        painter.end()
        return QIcon(pm)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if index.isValid() and index.column() == 0 and self.isDir(index):
            path = self.filePath(index)
            if role == Qt.ItemDataRole.DecorationRole and self.is_verified_folder(path):
                return self._badge(super().data(index, role))
            if role == Qt.ItemDataRole.ToolTipRole and self.is_verified_folder(path):
                return f"{path}\nVerified offload (MHL manifest present)"
        return super().data(index, role)


class ProductionView(QWidget):
    folder_activated = Signal(object)  # Path (show in browser)
    verify_requested = Signal(object)  # Path (folder to verify)
    add_category_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.production: Production | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.title = QLabel("<b>No production open</b>")
        header.addWidget(self.title, 1)
        self.open_btn = QPushButton("Open Folder")
        self.open_btn.clicked.connect(self._open_root)
        self.verify_btn = QPushButton("Verify...")
        self.verify_btn.setToolTip("Re-check files against their MHL manifests")
        self.verify_btn.clicked.connect(self._verify_selected)
        self.add_cat_btn = QPushButton("Add Folder")
        self.add_cat_btn.setToolTip("Add another top-level folder (e.g. BTS, Audio) to this production")
        self.add_cat_btn.clicked.connect(self.add_category_requested)
        for b in (self.open_btn, self.verify_btn, self.add_cat_btn):
            header.addWidget(b)
        layout.addLayout(header)

        self.model = ProductionFsModel(self)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.model.setReadOnly(True)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 360)
        self.tree.hideColumn(2)  # type
        self.tree.doubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.tree, 1)
        self.set_production(None)

    # ---- api --------------------------------------------------------------
    def set_production(self, production: Production | None) -> None:
        self.production = production
        enabled = production is not None
        for b in (self.open_btn, self.verify_btn, self.add_cat_btn):
            b.setEnabled(enabled)
        if production is None:
            self.title.setText("<b>No production open</b>")
            self.model.setRootPath("")
            self.tree.setRootIndex(QModelIndex())
            return
        self.title.setText(
            f"<b>{production.display_name}</b>  "
            f"<span style='color: {muted_hex(self)}'>{production.start_date:%b %d, %Y} - {production.end_date:%b %d, %Y}"
            f"  |  {', '.join(production.categories)}</span>"
        )
        self.model.setRootPath(str(production.root))
        self.tree.setRootIndex(self.model.index(str(production.root)))
        self.refresh()
        for cat in production.categories:
            idx = self.model.index(str(production.category_dir(cat)))
            if idx.isValid():
                self.tree.expand(idx)

    def refresh(self) -> None:
        self.model.invalidate()

    def reveal(self, path: Path) -> None:
        idx = self.model.index(str(path))
        if idx.isValid():
            self.tree.scrollTo(idx)
            self.tree.setCurrentIndex(idx)
            self.tree.expand(idx)

    # ---- internals ---------------------------------------------------------
    def _selected_path(self) -> Path | None:
        idx = self.tree.currentIndex()
        if idx.isValid():
            return Path(self.model.filePath(idx))
        return self.production.root if self.production else None

    def _open_root(self) -> None:
        if self.production:
            open_with_system(self.production.root)

    def _verify_selected(self) -> None:
        p = self._selected_path()
        if p is not None:
            self.verify_requested.emit(p if p.is_dir() else p.parent)

    def _double_clicked(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_dir():
            self.folder_activated.emit(path)
        else:
            open_with_system(path)

    def _menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        path = Path(self.model.filePath(idx))
        menu = QMenu(self)
        act_show = menu.addAction("Show in browser" if path.is_dir() else "Open")
        act_reveal = menu.addAction("Reveal in Explorer" if sys.platform.startswith("win") else "Reveal in Finder")
        act_verify = None
        if path.is_dir():
            menu.addSeparator()
            act_verify = menu.addAction("Verify against MHL...")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == act_show:
            self._double_clicked(idx)
        elif chosen == act_reveal:
            reveal_in_file_manager(path)
        elif chosen is not None and chosen == act_verify:
            self.verify_requested.emit(path)

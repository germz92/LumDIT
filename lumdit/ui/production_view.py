"""Permanent bottom panel: the production destination as a tree or icon view."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QSize, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStyle,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.offload import is_destination_hidden, is_junk
from lumdit.core.production import Production
from lumdit.core.thumbnails import media_kind
from lumdit.ui.media_browser import Entry, FolderModel
from lumdit.ui.util import muted_css, muted_hex, open_with_system, reveal_in_file_manager


def _system_path(root: Path) -> str:
    """Resolved filesystem path so the drive / volume is obvious (D:\\..., /Volumes/...)."""
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


class ProductionFsModel(QFileSystemModel):
    """QFileSystemModel that marks folders containing an MHL manifest as verified."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._verified_cache: dict[str, bool] = {}
        self._badge_icon: QIcon | None = None

    def invalidate(self) -> None:
        self._verified_cache.clear()

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


class DestinationFilterModel(QSortFilterProxyModel):
    """Hides .mhl / reports / junk in the tree unless 'Show hidden files' is on."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.show_hidden = False

    def set_show_hidden(self, on: bool) -> None:
        self.show_hidden = on
        self.invalidate()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        if self.show_hidden:
            return True
        src = self.sourceModel()
        if not isinstance(src, QFileSystemModel):
            return True
        index = src.index(row, 0, parent)
        name = src.fileName(index)
        if src.isDir(index):
            return not (name.startswith(".") or is_junk(name))
        return not is_destination_hidden(name)


class ProductionView(QWidget):
    folder_activated = Signal(object)  # Path (show in browser)
    verify_requested = Signal(object)  # Path (folder to verify)
    add_category_requested = Signal()
    edit_requested = Signal()

    def __init__(self, parent: QWidget | None = None, loader=None, settings=None) -> None:
        super().__init__(parent)
        self.production: Production | None = None
        self.settings = settings
        self._folder: Path | None = None
        show_hidden = bool(settings.dest_show_hidden) if settings is not None else False
        icon_view = bool(settings.dest_icon_view) if settings is not None else True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(1)
        self.title = QLabel("<b>No production open</b>")
        self.path = QLabel("")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setStyleSheet(muted_css(self))
        self.path.setToolTip("Full destination path — which drive and folder this production writes to")
        titles.addWidget(self.title)
        titles.addWidget(self.path)
        header.addLayout(titles, 1)
        self.open_btn = QPushButton("Open Folder")
        self.open_btn.clicked.connect(self._open_root)
        self.verify_btn = QPushButton("Verify...")
        self.verify_btn.setToolTip("Re-check files against their MHL manifests")
        self.verify_btn.clicked.connect(self._verify_selected)
        self.add_cat_btn = QPushButton("Add Folder")
        self.add_cat_btn.setToolTip("Add another top-level folder (e.g. BTS, Audio) to this production")
        self.add_cat_btn.clicked.connect(self.add_category_requested)
        self.edit_btn = QPushButton("Edit...")
        self.edit_btn.setToolTip("Rename the production, change the shoot dates or add/remove folders")
        self.edit_btn.clicked.connect(self.edit_requested)
        for b in (self.open_btn, self.verify_btn, self.add_cat_btn, self.edit_btn):
            header.addWidget(b)
        layout.addLayout(header)

        tools = QHBoxLayout()
        self.up_btn = QToolButton()
        self.up_btn.setText("Up")
        self.up_btn.setToolTip("Up one folder")
        self.up_btn.clicked.connect(self._go_up)
        tools.addWidget(self.up_btn)
        self.crumb = QLabel("")
        self.crumb.setStyleSheet(muted_css(self))
        tools.addWidget(self.crumb, 1)
        self.hidden_box = QCheckBox("Show hidden files")
        self.hidden_box.setToolTip("Show .mhl manifests, hash sidecars, reports and OS junk")
        self.hidden_box.setChecked(show_hidden)
        self.hidden_box.toggled.connect(self._set_show_hidden)
        tools.addWidget(self.hidden_box)
        self.icons_btn = QToolButton()
        self.icons_btn.setText("Icons")
        self.icons_btn.setCheckable(True)
        self.icons_btn.setChecked(icon_view)
        self.icons_btn.clicked.connect(lambda: self._set_icon_view(True))
        self.list_btn = QToolButton()
        self.list_btn.setText("List")
        self.list_btn.setCheckable(True)
        self.list_btn.setChecked(not icon_view)
        self.list_btn.clicked.connect(lambda: self._set_icon_view(False))
        tools.addWidget(self.icons_btn)
        tools.addWidget(self.list_btn)
        layout.addLayout(tools)

        self.model = ProductionFsModel(self)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.model.setReadOnly(True)
        self.proxy = DestinationFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.set_show_hidden(show_hidden)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 360)
        self.tree.hideColumn(2)
        self.tree.doubleClicked.connect(self._tree_activated)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)

        if loader is None:
            from PySide6.QtCore import QObject

            class _Stub(QObject):
                ready = Signal(str, object)
                failed = Signal(str)

                def request(self, *_a) -> None:
                    return None

                def reset(self) -> None:
                    return None

            loader = _Stub(self)
        self.icon_model = FolderModel(loader, 80, self)
        st = self.style()
        self.icon_model.set_generic_icons(
            {
                "folder": st.standardIcon(QStyle.StandardPixmap.SP_DirIcon),
                "image": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
                "raw": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
                "video": st.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
                "audio": st.standardIcon(QStyle.StandardPixmap.SP_MediaVolume),
                "other": st.standardIcon(QStyle.StandardPixmap.SP_FileIcon),
            }
        )
        self.icons = QListView()
        self.icons.setModel(self.icon_model)
        self.icons.setViewMode(QListView.ViewMode.IconMode)
        self.icons.setResizeMode(QListView.ResizeMode.Adjust)
        self.icons.setMovement(QListView.Movement.Static)
        self.icons.setWrapping(True)
        self.icons.setUniformItemSizes(True)
        self.icons.setSpacing(8)
        self.icons.setWordWrap(True)
        self.icons.setIconSize(QSize(80, 80))
        self.icons.setGridSize(QSize(104, 124))
        self.icons.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.icons.setDragEnabled(True)
        self.icons.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.icons.doubleClicked.connect(self._icon_activated)
        self.icons.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.icons.customContextMenuRequested.connect(self._icon_menu)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.icons)
        self.stack.addWidget(self.tree)
        self.stack.setCurrentIndex(0 if icon_view else 1)
        layout.addWidget(self.stack, 1)
        self._sync_view_buttons()
        self.set_production(None)

    # ---- api --------------------------------------------------------------
    def set_production(self, production: Production | None) -> None:
        self.production = production
        enabled = production is not None
        for b in (self.open_btn, self.verify_btn, self.add_cat_btn, self.edit_btn, self.up_btn, self.hidden_box):
            b.setEnabled(enabled)
        if production is None:
            self.title.setText("<b>No production open</b>")
            self.path.clear()
            self.path.hide()
            self.crumb.clear()
            self.model.setRootPath("")
            self.tree.setRootIndex(QModelIndex())
            self.icon_model.set_entries([])
            self._folder = None
            return
        dest = _system_path(production.root)
        self.title.setText(
            f"<b>{production.display_name}</b>  "
            f"<span style='color: {muted_hex(self)}'>{production.start_date:%b %d, %Y} - {production.end_date:%b %d, %Y}"
            f"  |  {', '.join(production.categories)}</span>"
        )
        self.path.setText(dest)
        self.path.setToolTip(f"Destination: {dest}")
        self.path.show()
        self.model.setRootPath(str(production.root))
        root_src = self.model.index(str(production.root))
        self.tree.setRootIndex(self.proxy.mapFromSource(root_src))
        self._set_icon_folder(production.root)
        self.refresh()
        for cat in production.categories:
            src = self.model.index(str(production.category_dir(cat)))
            idx = self.proxy.mapFromSource(src)
            if idx.isValid():
                self.tree.expand(idx)

    def refresh(self) -> None:
        self.model.invalidate()
        if self._folder is not None:
            self._fill_icons(self._folder)

    def reveal(self, path: Path) -> None:
        path = Path(path)
        src = self.model.index(str(path))
        if src.isValid():
            idx = self.proxy.mapFromSource(src)
            self.tree.scrollTo(idx)
            self.tree.setCurrentIndex(idx)
            self.tree.expand(idx)
        folder = path if path.is_dir() else path.parent
        self._set_icon_folder(folder)

    # ---- internals ---------------------------------------------------------
    def _selected_path(self) -> Path | None:
        if self.stack.currentWidget() is self.icons:
            idx = self.icons.currentIndex()
            entry = idx.data(Qt.ItemDataRole.UserRole) if idx.isValid() else None
            if isinstance(entry, Entry):
                return entry.path
            return self._folder or (self.production.root if self.production else None)
        idx = self.tree.currentIndex()
        if idx.isValid():
            src = self.proxy.mapToSource(idx)
            return Path(self.model.filePath(src))
        return self.production.root if self.production else None

    def _open_root(self) -> None:
        if self.production:
            open_with_system(self.production.root)

    def _verify_selected(self) -> None:
        p = self._selected_path()
        if p is not None:
            self.verify_requested.emit(p if p.is_dir() else p.parent)

    def _set_show_hidden(self, on: bool) -> None:
        self.proxy.set_show_hidden(on)
        if self.settings is not None:
            self.settings.dest_show_hidden = on
        self.refresh()

    def _set_icon_view(self, on: bool) -> None:
        self.stack.setCurrentIndex(0 if on else 1)
        if self.settings is not None:
            self.settings.dest_icon_view = on
        self._sync_view_buttons()
        self._update_crumb()

    def _sync_view_buttons(self) -> None:
        icons = self.stack.currentWidget() is self.icons
        self.icons_btn.setChecked(icons)
        self.list_btn.setChecked(not icons)
        self.up_btn.setVisible(icons)

    def _set_icon_folder(self, path: Path) -> None:
        if self.production is None:
            self._folder = None
            self.icon_model.set_entries([])
            self.crumb.clear()
            return
        root = self.production.root.resolve()
        try:
            path = path.resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            path = root
        self._folder = path
        self._fill_icons(path)
        self._update_crumb()

    def _fill_icons(self, folder: Path) -> None:
        entries: list[Entry] = []
        show_hidden = self.hidden_box.isChecked()
        try:
            with os.scandir(folder) as it:
                for de in it:
                    name = de.name
                    if not show_hidden and is_destination_hidden(name):
                        continue
                    try:
                        is_dir = de.is_dir(follow_symlinks=False)
                        size = 0 if is_dir else de.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if is_dir and not show_hidden and (name.startswith(".") or is_junk(name)):
                        continue
                    p = Path(de.path)
                    entries.append(Entry(p, is_dir, size, "folder" if is_dir else media_kind(p)))
        except OSError:
            entries = []
        entries.sort(key=lambda e: (not e.is_dir, e.path.name.lower()))
        self.icon_model.set_entries(entries)

    def _update_crumb(self) -> None:
        if self.production is None or self._folder is None:
            self.crumb.clear()
            return
        try:
            rel = self._folder.resolve().relative_to(self.production.root.resolve())
            text = rel.as_posix() if str(rel) != "." else self.production.root.name
        except (OSError, ValueError):
            text = self._folder.name
        self.crumb.setText(text)
        at_root = self._folder.resolve() == self.production.root.resolve()
        self.up_btn.setEnabled(self.production is not None and not at_root)

    def _go_up(self) -> None:
        if self.production is None or self._folder is None:
            return
        parent = self._folder.parent
        try:
            parent.resolve().relative_to(self.production.root.resolve())
        except ValueError:
            return
        self._set_icon_folder(parent)

    def _tree_activated(self, index: QModelIndex) -> None:
        src = self.proxy.mapToSource(index)
        path = Path(self.model.filePath(src))
        if path.is_dir():
            self.folder_activated.emit(path)
            self._set_icon_folder(path)
        else:
            open_with_system(path)

    def _icon_activated(self, index: QModelIndex) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, Entry):
            return
        if entry.is_dir:
            self._set_icon_folder(entry.path)
        else:
            open_with_system(entry.path)

    def _tree_menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        src = self.proxy.mapToSource(idx)
        self._path_menu(Path(self.model.filePath(src)), self.tree.viewport().mapToGlobal(pos), in_icons=False)

    def _icon_menu(self, pos) -> None:
        idx = self.icons.indexAt(pos)
        entry = idx.data(Qt.ItemDataRole.UserRole) if idx.isValid() else None
        path = entry.path if isinstance(entry, Entry) else self._folder
        if path is None:
            return
        self._path_menu(path, self.icons.viewport().mapToGlobal(pos), in_icons=True)

    def _path_menu(self, path: Path, global_pos, in_icons: bool) -> None:
        menu = QMenu(self)
        if path.is_dir():
            act_open = menu.addAction("Open folder" if in_icons else "Show in browser")
        else:
            act_open = menu.addAction("Open")
        act_reveal = menu.addAction("Reveal in Explorer" if sys.platform.startswith("win") else "Reveal in Finder")
        act_verify = None
        if path.is_dir():
            menu.addSeparator()
            act_verify = menu.addAction("Verify against MHL...")
        chosen = menu.exec(global_pos)
        if chosen == act_open:
            if path.is_dir() and in_icons:
                self._set_icon_folder(path)
            elif path.is_dir():
                self.folder_activated.emit(path)
            else:
                open_with_system(path)
        elif chosen == act_reveal:
            reveal_in_file_manager(path)
        elif chosen is not None and chosen == act_verify:
            self.verify_requested.emit(path)

"""Right-hand Card Log panel: the event's card log, per-card status and the "insert card" prompt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.cardlog import CardLogEntry, CardLogEvent
from lumdit.ui.util import muted_css, muted_hex

COL_CAMERA, COL_CARD1, COL_CARD2, COL_USER, COL_CATEGORY, COL_STATUS = range(6)

STATUS_TEXT = {
    "backed_up": "Backed up",
    "running": "Offloading...",
    "queued": "Queued",
    "paused": "Paused",
    "verified_unsynced": "Verified (log not updated)",
    "issues": "Needs attention",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "skipped": "Skipped",
    "pending": "Pending",
    "internal": "Internal storage",
    "none": "-",
}
STATUS_COLOUR = {
    "backed_up": "#2ea043",
    "running": "#3b82f6",
    "queued": "#3b82f6",
    "paused": "#e0a800",
    "verified_unsynced": "#e0a800",
    "issues": "#e0a800",
    "failed": "#d9534f",
    "cancelled": "#999999",
    "skipped": "#999999",
}


_CALLOUT_STYLE = """
QFrame#callout {{
    border: 2px {style} {border};
    border-radius: 8px;
    background: {bg};
}}
"""


class _CalloutFrame(QFrame):
    """The "Insert card #..." box; also a drop target so a card folder can be dropped straight onto it."""

    folders_dropped = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("callout")
        self.setAcceptDrops(True)
        self._armed(False)

    def _armed(self, on: bool) -> None:
        self.setStyleSheet(
            _CALLOUT_STYLE.format(
                style="dashed" if on else "solid",
                border="palette(highlight)" if on else "palette(mid)",
                bg="palette(alternate-base)" if on else "transparent",
            )
        )

    @staticmethod
    def _folders(event) -> list[Path]:
        md = event.mimeData()
        if not md.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]

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


class CardLogPanel(QWidget):
    start_event_requested = Signal()
    folders_dropped = Signal(list)  # list[Path] dropped on the "insert card" callout
    refresh_requested = Signal()
    target_changed = Signal(str, int)  # entry_id, slot
    choose_folder_requested = Signal()
    skip_requested = Signal()
    category_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log_event: CardLogEvent | None = None
        self.statuses: dict[tuple[str, int], str] = {}
        self.target: tuple[str, int] | None = None
        self._items: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        head = QHBoxLayout()
        self.title = QLabel("<b>Card Log</b>")
        head.addWidget(self.title, 1)
        self.progress = QLabel("")
        head.addWidget(self.progress)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Reload the card log from the crew app")
        self.refresh_btn.clicked.connect(self.refresh_requested)
        head.addWidget(self.refresh_btn)
        layout.addLayout(head)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(muted_css(self))
        layout.addWidget(self.note)

        # Empty state
        self.empty = QFrame()
        self.empty.setFrameShape(QFrame.Shape.StyledPanel)
        e_layout = QVBoxLayout(self.empty)
        e_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_label = QLabel("No event linked.\nStart an event backup to be walked through the card log.")
        e_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_label.setStyleSheet(muted_css(self))
        self.start_btn = QPushButton("Start Event Backup...")
        self.start_btn.clicked.connect(self.start_event_requested)
        e_layout.addWidget(e_label)
        e_layout.addWidget(self.start_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty, 1)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Camera", "Card 1", "Card 2", "User", "Folder", "Status"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_USER, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(False)
        self.tree.itemClicked.connect(self._item_clicked)
        layout.addWidget(self.tree, 1)

        # Next-card callout
        self.callout = _CalloutFrame()
        self.callout.folders_dropped.connect(self.folders_dropped)
        c_layout = QVBoxLayout(self.callout)
        c_layout.setSpacing(4)
        self.callout_title = QLabel("")
        f = QFont()
        f.setPointSizeF(f.pointSizeF() + 2)
        f.setBold(True)
        self.callout_title.setFont(f)
        self.callout_title.setWordWrap(True)
        self.callout_detail = QLabel("")
        self.callout_detail.setWordWrap(True)
        self.callout_detail.setStyleSheet(muted_css(self))
        c_layout.addWidget(self.callout_title)
        c_layout.addWidget(self.callout_detail)
        cat_row = QHBoxLayout()
        self.cat_label = QLabel("Folder:")
        self.category = QComboBox()
        self.category.currentTextChanged.connect(self.category_changed)
        cat_row.addWidget(self.cat_label)
        cat_row.addWidget(self.category, 1)
        self.cat_row_widget = QWidget()
        self.cat_row_widget.setLayout(cat_row)
        c_layout.addWidget(self.cat_row_widget)
        btn_row = QHBoxLayout()
        self.choose_btn = QPushButton("Choose folder...")
        self.choose_btn.setToolTip("Pick the card's folder manually instead of waiting for it to be detected")
        self.choose_btn.clicked.connect(self.choose_folder_requested)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setToolTip("Skip this card for now and move to the next one")
        self.skip_btn.clicked.connect(self.skip_requested)
        btn_row.addStretch(1)
        btn_row.addWidget(self.choose_btn)
        btn_row.addWidget(self.skip_btn)
        c_layout.addLayout(btn_row)
        layout.addWidget(self.callout)

        self.set_event(None)

    # ---- api --------------------------------------------------------------------
    def set_event(self, event: CardLogEvent | None, statuses: dict[tuple[str, int], str] | None = None) -> None:
        self.log_event = event
        if statuses is not None:
            self.statuses = dict(statuses)
        has = event is not None
        self.empty.setVisible(not has)
        self.tree.setVisible(has)
        self.callout.setVisible(has)
        self.refresh_btn.setEnabled(has)
        if event is None:
            self.title.setText("<b>Card Log</b>")
            self.progress.setText("")
            self.note.setText("")
            self.tree.clear()
            self._items.clear()
            return
        self.title.setText(f"<b>{event.title}</b>")
        self._rebuild()

    def set_status(self, entry_id: str, slot: int, status: str) -> None:
        self.statuses[(entry_id, slot)] = status
        item = self._items.get(entry_id)
        if item is not None and self.log_event is not None:
            entry = self.log_event.find_entry(entry_id)
            if entry is not None:
                self._fill_item(item, entry)
        self._update_progress()

    def set_target(self, entry_id: str | None, slot: int = 1, prompt: str = "insert") -> None:
        """Highlight the card the DIT should insert next and update the callout."""
        self.target = (entry_id, slot) if entry_id else None
        for item in self._items.values():
            self._style_item(item, targeted=False)
        if self.log_event is None:
            return
        if entry_id is None:
            done, total = self.log_event.progress(1)
            if total and done >= total:
                self.callout_title.setText("All card 1s are backed up")
                self.callout_detail.setText("Card 2s stay in the case. Click a Card 2 cell if you need to offload one.")
            else:
                self.callout_title.setText("Nothing left to prompt for")
                self.callout_detail.setText("Remaining cards are skipped, internal storage, or already in progress. Click a card to target it.")
            self.cat_row_widget.hide()
            self.choose_btn.setEnabled(False)
            self.skip_btn.setEnabled(False)
            return
        entry = self.log_event.find_entry(entry_id)
        item = self._items.get(entry_id)
        if entry is None:
            return
        if item is not None:
            self._style_item(item, targeted=True)
            self.tree.scrollToItem(item)
            self.tree.setCurrentItem(item)
        lead = "Offload next card?  Insert" if prompt == "next" else "Insert"
        self.callout_title.setText(f"{lead} card {entry.display_card(slot)}")
        day = entry.day.strftime("%m.%d.%Y") if entry.day else "date?"
        self.callout_detail.setText(
            f"{entry.camera}  |  {entry.user}  |  {entry.category or 'folder not set'}  |  {day}"
            + (f"\nNotes: {entry.notes}" if entry.notes else "")
            + "\nInsert the card (it will be detected) or drop its folder here."
        )
        self.cat_row_widget.setVisible(entry.category is None)
        if entry.category is None:
            self.category.blockSignals(True)
            self.category.clear()
            self.category.addItems(self._production_categories)
            self.category.setCurrentText("Photo" if "Photo" in self._production_categories else "")
            self.category.blockSignals(False)
        self.choose_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)

    _production_categories: list[str] = ["Photo", "Video", "Headshot Booth"]

    def set_production_categories(self, cats: list[str]) -> None:
        self._production_categories = list(cats)

    def chosen_category(self) -> str:
        return self.category.currentText()

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    # ---- internals ----------------------------------------------------------------
    def status_for(self, entry: CardLogEntry, slot: int) -> str:
        if not entry.has_card(slot):
            return "none"
        if entry.is_internal(slot):
            return "internal"
        if entry.backed_up(slot):
            return "backed_up"
        return self.statuses.get((entry.id, slot), "pending")

    def _rebuild(self) -> None:
        self.tree.clear()
        self._items.clear()
        if self.log_event is None:
            return
        for day in self.log_event.days:
            label = day.day.strftime("%m.%d.%Y") if day.day else "No date"
            top = QTreeWidgetItem([f"{label}  ({len(day.entries)})", "", "", "", "", ""])
            top.setFirstColumnSpanned(True)
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            bold = top.font(0)
            bold.setBold(True)
            top.setFont(0, bold)
            self.tree.addTopLevelItem(top)
            for entry in day.entries:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, entry.id)
                top.addChild(item)
                self._items[entry.id] = item
                self._fill_item(item, entry)
            top.setExpanded(True)
        self._update_progress()
        if self.target:
            self.set_target(*self.target)

    def _fill_item(self, item: QTreeWidgetItem, entry: CardLogEntry) -> None:
        item.setText(COL_CAMERA, entry.camera or "?")
        item.setText(COL_USER, entry.user)
        item.setText(COL_CATEGORY, entry.category or (entry.raw_category or "?"))
        for slot, col in ((1, COL_CARD1), (2, COL_CARD2)):
            st = self.status_for(entry, slot)
            mark = {"backed_up": "  \u2713", "running": "  \u2026", "queued": "  \u2026", "verified_unsynced": "  \u2713?"}.get(st, "")
            item.setText(col, entry.display_card(slot) + mark)
            colour = STATUS_COLOUR.get(st)
            item.setForeground(col, QBrush(QColor(colour)) if colour else QBrush())
            item.setToolTip(col, f"Card {slot}: {STATUS_TEXT.get(st, st)}")
        primary = self.status_for(entry, 1)
        item.setText(COL_STATUS, STATUS_TEXT.get(primary, primary))
        colour = STATUS_COLOUR.get(primary)
        item.setForeground(COL_STATUS, QBrush(QColor(colour)) if colour else QBrush(QColor(muted_hex(self))))
        if entry.category is None and entry.has_card(1) and not entry.is_internal(1):
            item.setForeground(COL_CATEGORY, QBrush(QColor("#e0a800")))
            item.setToolTip(COL_CATEGORY, "Folder not set in the card log - you will be asked when offloading")
        self._style_item(item, targeted=self.target is not None and self.target[0] == entry.id)

    def _style_item(self, item: QTreeWidgetItem, targeted: bool) -> None:
        font = item.font(COL_CAMERA)
        font.setBold(targeted)
        for col in range(6):
            item.setFont(col, font)
            item.setBackground(col, QBrush(QColor(59, 130, 246, 50)) if targeted else QBrush())

    def _update_progress(self) -> None:
        if self.log_event is None:
            return
        done, total = self.log_event.progress(1)
        running = sum(1 for s in self.statuses.values() if s in ("running", "queued"))
        text = f"{done} / {total} card 1s backed up"
        if running:
            text += f"  |  {running} in progress"
        self.progress.setText(text)

    def _item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        entry_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not entry_id or self.log_event is None:
            return
        entry = self.log_event.find_entry(entry_id)
        if entry is None:
            return
        slot = 2 if column == COL_CARD2 else 1
        if not entry.offloadable(slot):
            return
        self.target_changed.emit(entry_id, slot)

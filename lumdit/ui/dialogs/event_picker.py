"""Pick a crew-app event to back up from its card log."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.cardlog import CardLogClient, CardLogEvent
from lumdit.core.naming import sanitize_name
from lumdit.settings import Settings
from lumdit.ui.async_util import run_async
from lumdit.ui.util import muted_css


@dataclass
class EventChoice:
    event: CardLogEvent
    client: str
    name: str
    destination_root: Path


class EventPickerDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.choice: EventChoice | None = None
        self._events: list[CardLogEvent] = []
        self._client: CardLogClient | None = None
        self._names_touched = False
        self.setWindowTitle("Event Backup - choose an event")
        self.resize(820, 560)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by event, client or date...")
        self.search.textChanged.connect(self._fill_table)
        top.addWidget(self.search, 1)
        self.archived = QCheckBox("Include archived")
        self.archived.toggled.connect(lambda _c: self.reload())
        top.addWidget(self.archived)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self.reload)
        top.addWidget(self.reload_btn)
        layout.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Event", "Client", "Dates", "Cards pending", "Backed up"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _i: self._accept())
        layout.addWidget(self.table, 1)

        self.status = QLabel("Connecting to card log database...")
        self.status.setStyleSheet(muted_css(self))
        layout.addWidget(self.status)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.client_edit = QLineEdit()
        self.client_edit.textEdited.connect(self._touched)
        form.addRow("Client folder", self.client_edit)
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(self._touched)
        form.addRow("Production folder", self.name_edit)
        root_row = QHBoxLayout()
        self.root = QLineEdit(settings.default_destination_root)
        self.root.textChanged.connect(self._update_preview)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        root_row.addWidget(self.root, 1)
        root_row.addWidget(browse)
        form.addRow("Create in", root_row)
        self.preview = QLabel("")
        self.preview.setStyleSheet(muted_css(self) + " font-family: monospace;")
        self.preview.setWordWrap(True)
        form.addRow("", self.preview)
        layout.addLayout(form)
        for w in (self.client_edit, self.name_edit):
            w.textChanged.connect(self._update_preview)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Start Event Backup")
        self.ok_btn.setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.reload()

    # ---- loading --------------------------------------------------------------
    def reload(self) -> None:
        self.status.setText("Loading events...")
        self.reload_btn.setEnabled(False)
        include_archived = self.archived.isChecked()
        settings = self.settings

        def work():
            client = CardLogClient(settings.mongo_uri, settings.mongo_db, settings.mongo_collection)
            try:
                return client.list_events(include_archived=include_archived)
            finally:
                client.close()

        run_async(work, self._loaded, self._load_failed)

    def _loaded(self, events: list[CardLogEvent]) -> None:
        self._events = events
        self.reload_btn.setEnabled(True)
        self.status.setText(f"{len(events)} event(s) with card logs")
        self._fill_table()

    def _load_failed(self, msg: str) -> None:
        self.reload_btn.setEnabled(True)
        self.status.setText(f"Could not load events: {msg}")

    def _fill_table(self) -> None:
        q = self.search.text().strip().lower()
        self.table.setRowCount(0)
        for ev in self._events:
            hay = " ".join([ev.title, ev.client_name, str(ev.start or ""), str(ev.end or "")]).lower()
            if q and q not in hay:
                continue
            done, total = ev.progress(1)
            row = self.table.rowCount()
            self.table.insertRow(row)
            dates = f"{ev.start:%m.%d.%Y}" if ev.start else "?"
            if ev.end and ev.end != ev.start:
                dates += f" - {ev.end:%m.%d.%Y}"
            cells = [
                ev.title + ("  (archived)" if ev.archived else ""),
                ev.client_name,
                dates,
                str(total - done),
                f"{done} / {total}",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, ev.id)
                if col >= 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    # ---- selection --------------------------------------------------------------
    def selected_event(self) -> CardLogEvent | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        ev_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((e for e in self._events if e.id == ev_id), None)

    def _selection_changed(self) -> None:
        ev = self.selected_event()
        if ev is None:
            self.ok_btn.setEnabled(False)
            return
        if not self._names_touched:
            self.client_edit.setText(ev.client_name or "Client")
            self.name_edit.setText(ev.production_name)
        self._update_preview()

    def _touched(self, _text: str) -> None:
        self._names_touched = True

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Where should productions be created?", self.root.text())
        if chosen:
            self.root.setText(chosen)

    def _update_preview(self, *_args) -> None:
        ev = self.selected_event()
        client = sanitize_name(self.client_edit.text()) if self.client_edit.text().strip() else ""
        name = sanitize_name(self.name_edit.text()) if self.name_edit.text().strip() else ""
        ok = ev is not None and bool(client) and bool(name) and bool(self.root.text().strip())
        self.ok_btn.setEnabled(ok)
        if ok:
            root = Path(self.root.text()) / client / name
            note = "  (exists - will be reused)" if (root / "production.json").is_file() else ""
            self.preview.setText(f"{root}{note}")
        else:
            self.preview.setText("")

    def _accept(self) -> None:
        ev = self.selected_event()
        if ev is None or not self.ok_btn.isEnabled():
            return
        self.settings.default_destination_root = self.root.text().strip()
        self.choice = EventChoice(
            event=ev,
            client=self.client_edit.text().strip(),
            name=self.name_edit.text().strip(),
            destination_root=Path(self.root.text().strip()),
        )
        self.accept()

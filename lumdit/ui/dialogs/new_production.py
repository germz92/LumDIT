"""Create / Edit Production dialog."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.naming import sanitize_name
from lumdit.core.production import (
    PRESET_CATEGORIES,
    Production,
    ProductionError,
    create_production,
)
from lumdit.settings import Settings
from lumdit.ui.util import muted_css


def _qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _pydate(q: QDate) -> date:
    return date(q.year(), q.month(), q.day())


class NewProductionDialog(QDialog):
    """Create a production, or edit an existing one when *production* is given."""

    def __init__(self, settings: Settings, parent: QWidget | None = None, production: Production | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.editing = production
        self.production: Production | None = None
        self.changes: list[str] = []
        self.setWindowTitle("Edit Production" if production else "Create Production")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.client = QComboBox()
        self.client.setEditable(True)
        self.client.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.client.addItems(settings.history("client"))
        self.client.setCurrentText("")
        self.client.lineEdit().setPlaceholderText("e.g. Acme Corp  (becomes the top-level folder)")
        self.client.currentTextChanged.connect(self._update_preview)
        form.addRow("Client name", self.client)

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Product Launch")
        self.name.textChanged.connect(self._update_preview)
        form.addRow("Production name", self.name)

        today = QDate.currentDate()
        self.start = QDateEdit(today)
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("MM.dd.yyyy")
        self.end = QDateEdit(today)
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("MM.dd.yyyy")
        self.start.dateChanged.connect(self._start_changed)
        self.end.dateChanged.connect(self._update_preview)
        dates = QHBoxLayout()
        dates.addWidget(self.start)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self.end)
        self.days_label = QLabel("")
        dates.addWidget(self.days_label)
        dates.addStretch(1)
        form.addRow("Shoot dates", dates)

        folders_box = QVBoxLayout()
        self.folders = QListWidget()
        self.folders.setMaximumHeight(120)
        if production:
            for cat in production.categories:
                self._add_folder_item(cat, checked=True)
            for preset in PRESET_CATEGORIES:
                if preset not in production.categories:
                    self._add_folder_item(preset, checked=False)
        else:
            for preset in PRESET_CATEGORIES:
                # Only Photo is on by default; tick Video / Headshot Booth per production.
                self._add_folder_item(preset, checked=(preset == PRESET_CATEGORIES[0]))
        self.folders.itemChanged.connect(self._update_preview)
        folders_box.addWidget(self.folders)
        custom = QHBoxLayout()
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("Custom folder, e.g. BTS, Audio, Drone")
        self.custom_edit.returnPressed.connect(self._add_custom)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_custom)
        custom.addWidget(self.custom_edit, 1)
        custom.addWidget(add_btn)
        folders_box.addLayout(custom)
        form.addRow("Folders", folders_box)

        root_row = QHBoxLayout()
        self.root = QLineEdit(settings.default_destination_root)
        self.root.textChanged.connect(self._update_preview)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        root_row.addWidget(self.root, 1)
        root_row.addWidget(browse)
        if production:
            # Location is fixed when editing; renaming moves the folder within it.
            base = production.root.parent.parent if production.client else production.root.parent
            self.root.setText(str(base))
            self.root.setReadOnly(True)
            browse.hide()
            form.addRow("Location", root_row)
        else:
            form.addRow("Create in", root_row)
        layout.addLayout(form)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(muted_css(self) + " font-family: monospace;")
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.preview)

        if production:
            note = QLabel(
                "Renaming moves the production folder on disk. Unticked folders and dates outside the new range "
                "are only deleted when they contain no files."
                + (
                    "\nThis production is linked to a card-log event; Event Backup keeps working after a rename."
                    if production.event_id
                    else ""
                )
            )
            note.setWordWrap(True)
            note.setStyleSheet(muted_css(self))
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save Changes" if production else "Create Production")
        buttons.accepted.connect(self._save if production else self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if production:
            self.client.setCurrentText(production.client)
            self.name.setText(production.name)
            self.start.setDate(_qdate(production.start_date))
            self.end.setDate(_qdate(production.end_date))
        self._update_preview()

    # ---- helpers ------------------------------------------------------------
    def _add_folder_item(self, name: str, checked: bool) -> None:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.folders.addItem(item)

    def _add_custom(self) -> None:
        name = sanitize_name(self.custom_edit.text())
        if not self.custom_edit.text().strip():
            return
        existing = [self.folders.item(i).text().lower() for i in range(self.folders.count())]
        if name.lower() in existing:
            QMessageBox.information(self, "Folder exists", f"'{name}' is already in the list.")
            return
        self._add_folder_item(name, checked=True)
        self.custom_edit.clear()
        self._update_preview()

    def _start_changed(self, d: QDate) -> None:
        if self.end.date() < d:
            self.end.setDate(d)
        self._update_preview()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to create the production", self.root.text())
        if chosen:
            self.root.setText(chosen)

    def selected_categories(self) -> list[str]:
        return [
            self.folders.item(i).text()
            for i in range(self.folders.count())
            if self.folders.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _update_preview(self, *_args) -> None:
        if not hasattr(self, "preview"):
            return  # signals fire while the form is still being built
        start, end = _pydate(self.start.date()), _pydate(self.end.date())
        days = (end - start).days + 1 if end >= start else 0
        self.days_label.setText(f"{days} day{'s' if days != 1 else ''}")
        client = sanitize_name(self.client.currentText()) if self.client.currentText().strip() else "<Client Name>"
        name = sanitize_name(self.name.text()) if self.name.text().strip() else "<Production Name>"
        cats = self.selected_categories() or ["<folder>"]
        root = Path(self.root.text() or ".") / client / name
        lines = [str(root)]
        first = cats[0]
        lines.append(f"  {first}/")
        lines.append(f"    {start:%m.%d.%Y}/ ... {end:%m.%d.%Y}/  ({days} date folders)")
        lines.append("    Project Files/")
        lines.append("    Deliverables/")
        if len(cats) > 1:
            lines.append(f"  {', '.join(cats[1:])}/  (same layout)")
        self.preview.setText("\n".join(lines))

    def _create(self) -> None:
        try:
            self.production = create_production(
                self.root.text().strip(),
                self.client.currentText(),
                self.name.text(),
                _pydate(self.start.date()),
                _pydate(self.end.date()),
                self.selected_categories(),
                self.settings.card_folder_template,
            )
        except (ProductionError, OSError) as exc:
            QMessageBox.critical(self, "Cannot create production", str(exc))
            return
        self.settings.default_destination_root = self.root.text().strip()
        self.settings.add_history("client", self.client.currentText())
        self.accept()

    def _save(self) -> None:
        prod = self.editing
        assert prod is not None
        dropped = [c for c in prod.categories if c not in self.selected_categories()]
        if dropped:
            res = QMessageBox.question(
                self,
                "Remove folders?",
                "Remove these folders from the production?\n\n  " + "\n  ".join(dropped)
                + "\n\nFolders that contain files are kept on disk; only empty ones are deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                return
        try:
            self.changes = prod.update(
                self.client.currentText(),
                self.name.text(),
                _pydate(self.start.date()),
                _pydate(self.end.date()),
                self.selected_categories(),
            )
        except (ProductionError, OSError) as exc:
            QMessageBox.critical(self, "Cannot save changes", str(exc))
            return
        self.production = prod
        self.settings.add_history("client", self.client.currentText())
        self.accept()

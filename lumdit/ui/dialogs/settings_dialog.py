"""Preferences dialog."""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.cardlog import CardLogClient
from lumdit.core.naming import format_card_folder, validate_template
from lumdit.core.thumbnails import ThumbnailCache
from lumdit.settings import Settings, cache_dir
from lumdit.ui.async_util import run_async
from lumdit.ui.util import muted_css


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.template = QLineEdit(settings.card_folder_template)
        self.template.textChanged.connect(self._preview_template)
        form.addRow("Card folder template", self.template)
        self.template_preview = QLabel("")
        self.template_preview.setStyleSheet(muted_css(self))
        form.addRow("", self.template_preview)
        form.addRow("", QLabel("Fields: {camera}  {operator}  {card}  - card renders as #12 or a label like Internal  (new productions only)"))

        self.max_jobs = QSpinBox()
        self.max_jobs.setRange(1, 8)
        self.max_jobs.setValue(settings.max_concurrent_jobs)
        self.max_jobs.setToolTip("Simultaneous offloads. Jobs from the same card are always run one at a time.")
        form.addRow("Max simultaneous jobs", self.max_jobs)

        self.thumb_size = QSpinBox()
        self.thumb_size.setRange(64, 320)
        self.thumb_size.setSingleStep(16)
        self.thumb_size.setValue(settings.thumbnail_size)
        form.addRow("Default thumbnail size", self.thumb_size)

        root_row = QHBoxLayout()
        self.root = QLineEdit(settings.default_destination_root)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        root_row.addWidget(self.root, 1)
        root_row.addWidget(browse)
        form.addRow("Default production location", root_row)

        self.eject = QCheckBox("Eject card automatically after a fully verified offload")
        self.eject.setChecked(settings.eject_after_offload)
        form.addRow("", self.eject)

        self.hidden = QCheckBox("Show hidden and system files in the media browser")
        self.hidden.setChecked(settings.show_hidden_files)
        form.addRow("", self.hidden)

        cache_row = QHBoxLayout()
        self.cache_label = QLabel(str(cache_dir()))
        self.cache_label.setStyleSheet(muted_css(self))
        clear = QPushButton("Clear thumbnail cache")
        clear.clicked.connect(self._clear_cache)
        cache_row.addWidget(self.cache_label, 1)
        cache_row.addWidget(clear)
        form.addRow("Thumbnail cache", cache_row)
        layout.addLayout(form)

        # ---- card log (MongoDB) ----
        group = QGroupBox("Card Log (crew app MongoDB)")
        gform = QFormLayout(group)
        gform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        uri_row = QHBoxLayout()
        self.mongo_uri = QLineEdit(settings.value("cardlog/mongo_uri", "", str))
        self.mongo_uri.setEchoMode(QLineEdit.EchoMode.Password)
        self.mongo_uri.setPlaceholderText(
            "(using MONGO_URI from .env)" if settings.mongo_uri_from_env else "mongodb+srv://user:password@cluster/..."
        )
        self.show_uri = QPushButton("Show")
        self.show_uri.setCheckable(True)
        self.show_uri.toggled.connect(
            lambda on: self.mongo_uri.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        uri_row.addWidget(self.mongo_uri, 1)
        uri_row.addWidget(self.show_uri)
        gform.addRow("Connection string", uri_row)
        env_note = QLabel(
            "Leave blank to use MONGO_URI from lumdit/.env. Use a database user limited to read/write on this database."
        )
        env_note.setWordWrap(True)
        env_note.setStyleSheet(muted_css(self))
        gform.addRow("", env_note)
        self.mongo_db = QLineEdit(settings.mongo_db)
        gform.addRow("Database", self.mongo_db)
        self.mongo_collection = QLineEdit(settings.mongo_collection)
        gform.addRow("Collection", self.mongo_collection)
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        gform.addRow("", test_row)
        layout.addWidget(group)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._preview_template()

    def _preview_template(self) -> None:
        t = self.template.text()
        if validate_template(t):
            self.template_preview.setText(f"Example: {format_card_folder('FX3', 'Germaine', 12, t)}")
        else:
            self.template_preview.setText("Invalid template")

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Default production location", self.root.text())
        if chosen:
            self.root.setText(chosen)

    def _clear_cache(self) -> None:
        n = ThumbnailCache(cache_dir()).clear()
        QMessageBox.information(self, "Thumbnail cache", f"Removed {n} cached thumbnails.")

    def _test_connection(self) -> None:
        uri = self.mongo_uri.text().strip() or os.environ.get("MONGO_URI", "")
        if not uri:
            self.test_result.setText("No connection string (enter one or add MONGO_URI to lumdit/.env).")
            return
        db, coll = self.mongo_db.text().strip() or "test", self.mongo_collection.text().strip() or "tables"
        self.test_btn.setEnabled(False)
        self.test_result.setText("Connecting...")

        def work() -> int:
            client = CardLogClient(uri, db, coll, timeout_ms=8000)
            try:
                client.test()
                return len(client.list_events(include_archived=True))
            finally:
                client.close()

        def done(n: int) -> None:
            self.test_btn.setEnabled(True)
            self.test_result.setText(f"Connected. {n} event(s) with card logs in {db}.{coll}.")

        def failed(exc: Exception) -> None:
            self.test_btn.setEnabled(True)
            self.test_result.setText(f"Failed: {exc}")

        run_async(work, done, failed)

    def _save(self) -> None:
        if not validate_template(self.template.text()):
            QMessageBox.warning(self, "Invalid template", "The card folder template must use {camera}, {operator} and {card}.")
            return
        s = self.settings
        s.card_folder_template = self.template.text()
        s.max_concurrent_jobs = self.max_jobs.value()
        s.thumbnail_size = self.thumb_size.value()
        s.default_destination_root = self.root.text()
        s.eject_after_offload = self.eject.isChecked()
        s.show_hidden_files = self.hidden.isChecked()
        s.mongo_uri = self.mongo_uri.text()
        s.mongo_db = self.mongo_db.text() or "test"
        s.mongo_collection = self.mongo_collection.text() or "tables"
        self.accept()

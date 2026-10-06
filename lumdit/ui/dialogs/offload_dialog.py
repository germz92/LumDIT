"""Offload dialog: asks Camera / Operator / Card # / Folder / Date, pre-filled from history."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lumdit.core import devices
from lumdit.core.naming import format_card_number, sanitize_name
from lumdit.core.offload import OffloadRequest, ScanResult, scan_source
from lumdit.core.production import Production
from lumdit.settings import Settings
from lumdit.ui.util import human_size, muted_css


class _ScanSignals(QObject):
    done = Signal(object)  # ScanResult
    error = Signal(str)


class _ScanTask(QRunnable):
    def __init__(self, source: Path, label: str, signals: _ScanSignals) -> None:
        super().__init__()
        self.source, self.label, self.signals = source, label, signals

    def run(self) -> None:
        try:
            self.signals.done.emit(scan_source(self.source, self.label))
        except Exception as exc:
            self.signals.error.emit(str(exc))


class OffloadDialog(QDialog):
    def __init__(
        self,
        production: Production,
        source: Path,
        settings: Settings,
        parent: QWidget | None = None,
        preferred_category: str | None = None,
        prefill: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.production = production
        self.source = Path(source)
        self.settings = settings
        self.request: OffloadRequest | None = None
        self.scan: ScanResult | None = None
        self._card_touched = False
        self.prefill = prefill or {}
        self.log_entry_id = str(self.prefill.get("log_entry_id") or "")
        self.log_slot = int(self.prefill.get("log_slot") or 0)
        self.setWindowTitle("Offload Card")
        self.setMinimumWidth(560)

        vol = devices.volume_for_path(self.source)
        self.volume_label = vol.label if vol and vol.removable else (vol.label if vol else "")

        layout = QVBoxLayout(self)
        src_box = QVBoxLayout()
        src_title = QLabel(f"<b>Source:</b> {self.source}")
        src_title.setWordWrap(True)
        src_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.src_info = QLabel("Scanning source...")
        self.src_info.setStyleSheet(muted_css(self))
        src_box.addWidget(src_title)
        src_box.addWidget(self.src_info)
        layout.addLayout(src_box)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.category = QComboBox()
        self.category.addItems(production.categories)
        cat = preferred_category or self._guess_category()
        if cat in production.categories:
            self.category.setCurrentText(cat)
        self.category.currentTextChanged.connect(self._category_changed)
        form.addRow("Folder", self.category)

        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("MM.dd.yyyy")
        today = date.today()
        default_day = today if production.start_date <= today <= production.end_date else production.start_date
        self.date.setDate(QDate(default_day.year, default_day.month, default_day.day))
        self.date.dateChanged.connect(self._update_preview)
        date_row = QHBoxLayout()
        date_row.addWidget(self.date)
        self.date_hint = QLabel("")
        self.date_hint.setStyleSheet(muted_css(self))
        date_row.addWidget(self.date_hint, 1)
        form.addRow("Date", date_row)

        defaults = production.defaults_for(self.category.currentText())
        self.camera = self._history_combo("camera", defaults["camera"])
        self.camera.setToolTip("Camera body, e.g. FX3, A7IV, C70")
        form.addRow("Camera name", self.camera)

        self.operator = self._history_combo("operator", defaults["operator"])
        self.operator.setToolTip("Shooter's first name")
        form.addRow("Operator", self.operator)

        self.card = QSpinBox()
        self.card.setRange(1, 9999)
        self.card.setPrefix("#")
        self.card.setToolTip("The number written on the physical card")
        self.card.setValue(max(1, defaults["card"]))
        self.card.valueChanged.connect(self._card_edited)
        card_row = QHBoxLayout()
        card_row.addWidget(self.card)
        self.card_hint = QLabel("")
        self.card_hint.setStyleSheet(muted_css(self))
        card_row.addWidget(self.card_hint, 1)
        form.addRow("Card number", card_row)
        layout.addLayout(form)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.preview)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #d9534f;")
        self.warning.hide()
        layout.addWidget(self.warning)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Start Offload")
        self.ok_btn.setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for combo in (self.camera, self.operator):
            combo.currentTextChanged.connect(self._names_changed)
        self._names_changed()
        if self.prefill:
            self._apply_prefill()

        self._scan_signals = _ScanSignals()
        self._scan_signals.done.connect(self._scan_done)
        self._scan_signals.error.connect(self._scan_error)
        QThreadPool.globalInstance().start(_ScanTask(self.source, self.volume_label, self._scan_signals))

    # ---- helpers ------------------------------------------------------------
    def _apply_prefill(self) -> None:
        """Fill every field from a card-log entry; the user can still edit them."""
        p = self.prefill
        if p.get("category") in self.production.categories:
            self.category.setCurrentText(p["category"])
        if p.get("date"):
            d = p["date"]
            self.date.setDate(QDate(d.year, d.month, d.day))
        if p.get("camera"):
            self.camera.setCurrentText(p["camera"])
        if p.get("operator"):
            self.operator.setCurrentText(p["operator"])
        card = p.get("card")
        if isinstance(card, int):
            self._card_touched = True
            self.card.setValue(card)
        elif card:
            # Text label such as "Internal": keep it as the card label, hide the spinner.
            self._text_card = str(card)
            self.card.setEnabled(False)
            self.card_hint.setText(f"card label from log: {card}")
        if p.get("note"):
            self.src_info.setText(p["note"])
        self._update_preview()

    _text_card: str | None = None

    def _card_value(self) -> int | str:
        return self._text_card if self._text_card else self.card.value()

    def _history_combo(self, key: str, current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        items = self.settings.history(key)
        if current and current not in items:
            items.insert(0, current)
        combo.addItems(items)
        combo.setCurrentText(current)
        return combo

    def _guess_category(self) -> str:
        """Guess Photo vs Video from the dropped folder name."""
        name = self.source.name.upper()
        cats = {c.lower(): c for c in self.production.categories}
        if any(k in name for k in ("CLIP", "M4ROOT", "XDROOT", "AVCHD", "BDMV", "PRIVATE", "MOVIE")):
            return cats.get("video", self.production.categories[0])
        if "DCIM" in name or name.endswith(("MSDCF", "CANON", "NIKON", "FUJI")):
            return cats.get("photo", self.production.categories[0])
        return self.production.categories[0]

    def _category_changed(self, cat: str) -> None:
        d = self.production.defaults_for(cat)
        if d["camera"] and not self.camera.currentText().strip():
            self.camera.setCurrentText(d["camera"])
        if d["operator"] and not self.operator.currentText().strip():
            self.operator.setCurrentText(d["operator"])
        self._names_changed()

    def _names_changed(self, *_args) -> None:
        camera, operator = self.camera.currentText(), self.operator.currentText()
        if not self._card_touched:
            last = self.production.last_card_number(camera, operator)
            if isinstance(last, int):
                self.card.blockSignals(True)
                self.card.setValue(last)
                self.card.blockSignals(False)
        self._update_card_hint()
        self._update_preview()

    def _card_edited(self, _v: int) -> None:
        self._card_touched = True
        self._update_card_hint()
        self._update_preview()

    def _update_card_hint(self) -> None:
        camera, operator = self.camera.currentText(), self.operator.currentText()
        history = self.production.card_history(camera, operator)
        previous = self.production.offloads_for_card(camera, operator, self._card_value())
        parts = ["physical card label"]
        if previous:
            parts.append(f"card {format_card_number(self._card_value())} already offloaded {len(previous)}x for {camera or 'this camera'}")
        elif history:
            parts.append("used so far: " + ", ".join(format_card_number(n) for n in history[:8]))
        self.card_hint.setText("  |  ".join(parts))

    def _shoot_date(self) -> date:
        q = self.date.date()
        return date(q.year(), q.month(), q.day())

    def destination(self) -> Path:
        return self.production.card_dir(
            self.category.currentText(),
            self._shoot_date(),
            self.camera.currentText(),
            self.operator.currentText(),
            self._card_value(),
        )

    def _update_preview(self, *_args) -> None:
        day = self._shoot_date()
        in_range = self.production.start_date <= day <= self.production.end_date
        self.date_hint.setText("" if in_range else "outside the production's dates - folder will be created")
        dest = self.destination()
        try:
            rel = dest.relative_to(self.production.root)
        except ValueError:
            rel = dest
        exists = dest.exists()
        prefix = f"{self.production.client}/" if self.production.client else ""
        self.preview.setText(
            f"Destination: {prefix}{self.production.root.name}/{rel.as_posix()}"
            + ("\n(folder already exists - identical files will be skipped, different ones flagged)" if exists else "")
        )
        self._validate()

    def _validate(self) -> None:
        ok = bool(self.camera.currentText().strip()) and bool(self.operator.currentText().strip()) and self.scan is not None
        self.ok_btn.setEnabled(ok and bool(self.scan and self.scan.files))

    def _scan_done(self, scan: ScanResult) -> None:
        self.scan = scan
        if not scan.files:
            self.src_info.setText("This folder contains no files to copy.")
        else:
            self.src_info.setText(
                f"{len(scan.files):,} files, {human_size(scan.total_bytes)}"
                + (f", {scan.skipped_junk} system/junk items will be skipped" if scan.skipped_junk else "")
                + (f"  |  Volume: {self.volume_label}" if self.volume_label else "")
            )
        dup = self.production.find_duplicate(scan.fingerprint)
        if dup:
            self.warning.setText(
                f"This card content was already offloaded on {dup.timestamp[:16].replace('T', ' ')} to "
                f"{Path(dup.destination).name} ({dup.file_count} files). Starting again will skip identical files."
            )
            self.warning.show()
        self._validate()

    def _scan_error(self, msg: str) -> None:
        self.src_info.setText(f"Could not read source: {msg}")

    def _accept(self) -> None:
        camera = self.camera.currentText().strip()
        operator = self.operator.currentText().strip()
        if not camera or not operator:
            QMessageBox.warning(self, "Missing information", "Camera name and operator are required.")
            return
        dest = self.destination()
        if dest.exists() and any(dest.iterdir()):
            res = QMessageBox.question(
                self,
                "Destination exists",
                f"{dest.name} already contains files.\n\nIdentical files will be skipped and different files "
                "flagged; nothing will be overwritten. Continue?",
            )
            if res != QMessageBox.StandardButton.Yes:
                return
        self.settings.add_history("camera", camera)
        self.settings.add_history("operator", operator)
        self.request = OffloadRequest(
            source=self.source,
            destination=dest,
            client_name=self.production.client,
            production_name=self.production.name,
            category=self.category.currentText(),
            shoot_date=self._shoot_date(),
            camera=sanitize_name(camera),
            operator=sanitize_name(operator),
            card_number=self._card_value(),
            source_label=self.volume_label,
            log_entry_id=self.log_entry_id,
            log_slot=self.log_slot,
        )
        self.accept()

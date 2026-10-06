"""Dialogs that present offload and verification results."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.mhl import VerifyResult
from lumdit.core.offload import OffloadResult
from lumdit.ui.util import human_size, muted_hex, open_with_system, reveal_in_file_manager


def _table(headers: list[str], rows: list[list[str]]) -> QTableWidget:
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(text))
    t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    t.resizeColumnsToContents()
    return t


class OffloadResultDialog(QDialog):
    def __init__(self, result: OffloadResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result = result
        self.setWindowTitle(f"Offload {result.status.title()} - {result.request.title}")
        self.resize(760, 480)
        layout = QVBoxLayout(self)

        colour = {"verified": "#2ea043", "issues": "#e0a800", "cancelled": "#999", "error": "#d9534f"}[result.status]
        summary = QLabel(
            f"<h3 style='color:{colour}; margin:0'>{result.status.upper()}</h3>"
            f"{result.message}<br>"
            f"<b>{result.copied}</b> copied, <b>{result.skipped}</b> already present, "
            f"<b>{len(result.problems)}</b> problems  |  {human_size(result.copied_bytes)} in {result.duration:.0f}s"
            + (f"  ({human_size(result.copied_bytes / result.duration)}/s)" if result.duration > 0 and result.copied_bytes else "")
            + f"<br><span style='color: {muted_hex(self)}'>{result.request.destination}</span>"
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary.setWordWrap(True)
        layout.addWidget(summary)

        rows = [[f.status, f.xxh64 or "-", f.relative, f.detail] for f in result.files]
        # Problems first.
        rows.sort(key=lambda r: 0 if r[0] in ("conflict", "error") else 1)
        layout.addWidget(_table(["Status", "xxHash64", "File", "Detail"], rows), 1)

        btns = QHBoxLayout()
        if result.report_path:
            open_report = QPushButton("Open report")
            open_report.clicked.connect(lambda: open_with_system(result.report_path))
            btns.addWidget(open_report)
        reveal = QPushButton("Reveal destination")
        reveal.clicked.connect(lambda: reveal_in_file_manager(result.request.destination))
        btns.addWidget(reveal)
        btns.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        btns.addWidget(close)
        layout.addLayout(btns)


class VerifyResultDialog(QDialog):
    def __init__(self, results: list[VerifyResult], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Verification Results")
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        total_checked = sum(r.checked for r in results)
        total_ok = sum(r.ok for r in results)
        issues = [(r, i) for r in results for i in (r.issues or [])]
        colour = "#2ea043" if not issues else "#d9534f"
        head = QLabel(
            f"<h3 style='color:{colour}; margin:0'>{'ALL FILES VERIFIED' if not issues else f'{len(issues)} PROBLEM(S)'}</h3>"
            f"{len(results)} manifest(s), {total_checked} files checked, {total_ok} OK"
        )
        layout.addWidget(head)
        rows = [[r.manifest.parent.name, i.kind, i.relative_path, i.detail] for r, i in issues]
        if rows:
            layout.addWidget(_table(["Card folder", "Problem", "File", "Detail"], rows), 1)
        else:
            rows = [[r.manifest.parent.name, r.manifest.name, str(r.checked), "OK"] for r in results]
            layout.addWidget(_table(["Card folder", "Manifest", "Files", "Status"], rows), 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

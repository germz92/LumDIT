"""Top-right job progress panel: compact summary in the toolbar + popup with every job."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumdit.core.jobs import Job, JobManager
from lumdit.settings import Settings
from lumdit.ui.util import human_eta, human_rate, muted_css

_STATE_TEXT = {
    "queued": "Queued",
    "running": "",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


class JobRow(QFrame):
    cancel_clicked = Signal(int)
    show_result = Signal(int)
    retry_clicked = Signal(int)
    pause_clicked = Signal(int)
    resume_clicked = Signal(int)

    def __init__(self, job: Job, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job_id = job.id
        self.kind = job.kind
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        top = QHBoxLayout()
        self.title = QLabel(f"<b>{job.title}</b>")
        top.addWidget(self.title, 1)
        self.state = QLabel("")
        top.addWidget(self.state)
        self.pause_btn = QToolButton()
        self.pause_btn.setText("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Pause this job. It continues exactly where it stopped when resumed.")
        self.pause_btn.clicked.connect(self._pause_toggled)
        top.addWidget(self.pause_btn)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.clicked.connect(lambda: self.cancel_clicked.emit(self.job_id))
        top.addWidget(self.cancel_btn)
        self.retry_btn = QToolButton()
        self.retry_btn.setText("Retry")
        self.retry_btn.clicked.connect(lambda: self.retry_clicked.emit(self.job_id))
        self.retry_btn.hide()
        top.addWidget(self.retry_btn)
        self.result_btn = QToolButton()
        self.result_btn.setText("Details")
        self.result_btn.clicked.connect(lambda: self.show_result.emit(self.job_id))
        self.result_btn.hide()
        top.addWidget(self.result_btn)
        layout.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        layout.addWidget(self.bar)

        self.detail = QLabel("")
        self.detail.setStyleSheet(muted_css(self))
        layout.addWidget(self.detail)
        self.update_job(job)

    def _pause_toggled(self, checked: bool) -> None:
        (self.pause_clicked if checked else self.resume_clicked).emit(self.job_id)

    def update_job(self, job: Job) -> None:
        p = job.progress
        paused = job.paused
        self.bar.setValue(int(p.fraction * 1000))
        if job.state == "running":
            self.state.setText("Paused" if paused else p.phase.capitalize())
            if paused:
                self.detail.setText(
                    f"{p.files_done}/{p.files_total} files  |  paused" + (f"\n{p.current_file}" if p.current_file else "")
                )
            else:
                self.detail.setText(
                    f"{p.files_done}/{p.files_total} files  |  {human_rate(p.speed_bps)}  |  ETA {human_eta(p.eta_seconds)}"
                    + (f"\n{p.current_file}" if p.current_file else "")
                )
        elif job.state == "queued":
            self.state.setText("Paused" if paused else "Queued")
            self.detail.setText(job.message)
        else:
            self.state.setText(_STATE_TEXT.get(job.state, job.state))
            self.detail.setText(job.message)
            if job.state == "done":
                self.bar.setValue(1000)
        finished = job.state in ("done", "failed", "cancelled")
        self.cancel_btn.setVisible(not finished)
        self.pause_btn.setVisible(not finished)
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(paused)
        self.pause_btn.setText("Resume" if paused else "Pause")
        self.pause_btn.blockSignals(False)
        self.result_btn.setVisible(finished and job.result is not None)
        # Offloads that did not finish clean can be re-run; verified files are skipped by the engine.
        result_status = getattr(job.result, "status", None)
        needs_retry = finished and self.kind == "offload" and (
            job.state in ("failed", "cancelled") or result_status in ("issues", "error", "cancelled")
        )
        self.retry_btn.setVisible(needs_retry)
        if job.state == "cancelled":
            self.retry_btn.setText("Resume")
            self.retry_btn.setToolTip("Continue this offload. Files already copied and verified are skipped.")
        else:
            self.retry_btn.setText("Retry")
            self.retry_btn.setToolTip("Run this offload again. Verified files are skipped; missing or failed files are copied.")
        colour = {"done": "#2ea043", "failed": "#d9534f", "cancelled": "#999999"}.get(job.state, "palette(highlight)")
        if job.state == "done" and result_status in ("issues", "error"):
            colour = "#e0a800"
        elif paused:
            colour = "#e0a800"
        self.setStyleSheet("QFrame { border-left: 3px solid %s; }" % colour)


class JobListPopup(QFrame):
    """Resizable floating window listing every job. Size/position are remembered between runs."""

    GEOMETRY_KEY = "jobs_window"

    def __init__(self, manager: JobManager, parent: QWidget | None = None, settings: Settings | None = None) -> None:
        # A Tool window (not a Popup) so the user can resize it and keep it open while working.
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowCloseButtonHint)
        self.manager = manager
        self.settings = settings
        self.setWindowTitle("Jobs")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumSize(420, 260)
        self.resize(560, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        self.head_label = QLabel("<b>Jobs</b>")
        head.addWidget(self.head_label, 1)
        self.pause_all_btn = QPushButton("Pause all")
        self.pause_all_btn.setToolTip("Pause every running and queued job; press again to resume them.")
        self.pause_all_btn.clicked.connect(self._pause_all_clicked)
        self.pause_all_btn.hide()
        head.addWidget(self.pause_all_btn)
        clear = QPushButton("Clear finished")
        clear.clicked.connect(self.clear_finished)
        head.addWidget(clear)
        layout.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)
        self.empty = QLabel("No jobs yet. Drop a card folder onto the production panel to start an offload.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(muted_css(self))
        layout.addWidget(self.empty)
        self.rows: dict[int, JobRow] = {}
        self._restored = False

    def add_job(self, job: Job, on_show_result, on_retry) -> JobRow:
        row = JobRow(job)
        row.cancel_clicked.connect(self.manager.cancel)
        row.pause_clicked.connect(self.manager.pause)
        row.resume_clicked.connect(self.manager.resume)
        row.show_result.connect(on_show_result)
        row.retry_clicked.connect(on_retry)
        self.rows_layout.insertWidget(0, row)
        self.rows[job.id] = row
        self.empty.hide()
        self._update_head()
        return row

    def update_job(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row:
            row.update_job(job)
        self._update_head()

    def clear_finished(self) -> None:
        for jid, row in list(self.rows.items()):
            job = self.manager.jobs.get(jid)
            if job and job.state in ("done", "failed", "cancelled"):
                self.rows_layout.removeWidget(row)
                row.deleteLater()
                del self.rows[jid]
        self.empty.setVisible(not self.rows)
        self._update_head()

    def _update_head(self) -> None:
        jobs = [j for j in (self.manager.jobs.get(i) for i in self.rows) if j]
        active = [j for j in jobs if j.active]
        paused = [j for j in active if j.paused]
        text = f"<b>Jobs</b> ({len(self.rows)})" if self.rows else "<b>Jobs</b>"
        if active:
            text += f" - {len(active)} active"
            if paused:
                text += f", {len(paused)} paused"
        self.head_label.setText(text)
        self.pause_all_btn.setVisible(bool(active))
        all_paused = bool(active) and len(paused) == len(active)
        self.pause_all_btn.setText("Resume all" if all_paused else "Pause all")

    def _pause_all_clicked(self) -> None:
        active = [j for j in self.manager.jobs.values() if j.active]
        if active and all(j.paused for j in active):
            self.manager.resume_all()
        else:
            self.manager.pause_all()

    # ---- geometry persistence ---------------------------------------------------------
    def restore_geometry(self) -> bool:
        if self._restored or self.settings is None:
            return self._restored
        self._restored = True
        g = self.settings.geometry(self.GEOMETRY_KEY)
        if isinstance(g, QByteArray) and not g.isEmpty():
            return self.restoreGeometry(g)
        return False

    def _save_geometry(self) -> None:
        if self.settings is not None:
            self.settings.save_geometry(self.GEOMETRY_KEY, self.saveGeometry())

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_geometry()
        super().closeEvent(event)


class JobPanel(QWidget):
    """Compact toolbar widget: overall progress + button that opens the job list."""

    show_result = Signal(int)
    retry_requested = Signal(int)

    def __init__(self, manager: JobManager, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.popup = JobListPopup(manager, self, settings)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        self.label = QLabel("No active jobs")
        layout.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(200)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        self.bar.hide()
        layout.addWidget(self.bar)
        self.rate = QLabel("")
        layout.addWidget(self.rate)
        self.btn = QToolButton()
        self.btn.setText("Jobs")
        self.btn.setToolTip("Show all offload and verify jobs")
        self.btn.clicked.connect(self.toggle_popup)
        layout.addWidget(self.btn)

        manager.job_added.connect(self._added)
        manager.job_updated.connect(self._updated)
        manager.job_finished.connect(self._updated)

    def toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            return
        if not self.popup.restore_geometry():
            # First open: hang the window under the toolbar button, right-aligned, and keep it on screen.
            pos = self.btn.mapToGlobal(QPoint(self.btn.width() - self.popup.width(), self.btn.height() + 4))
            screen = self.btn.screen()
            if screen is not None:
                avail = screen.availableGeometry()
                height = min(self.popup.height(), avail.height() - (pos.y() - avail.y()) - 24)
                self.popup.resize(self.popup.width(), max(height, self.popup.minimumHeight()))
                pos.setX(max(avail.left(), min(pos.x(), avail.right() - self.popup.width())))
            self.popup.move(pos)
        self.popup.show()
        self.popup.raise_()
        self.popup.activateWindow()

    def _added(self, job: Job) -> None:
        self.popup.add_job(job, self.show_result.emit, self.retry_requested.emit)
        self._summary()

    def _updated(self, job: Job) -> None:
        self.popup.update_job(job)
        self._summary()

    def _summary(self) -> None:
        active = [j for j in self.manager.jobs.values() if j.state in ("running", "queued")]
        if not active:
            finished = [j for j in self.manager.jobs.values() if j.state in ("done", "failed", "cancelled")]
            if finished:
                last = finished[-1]
                self.label.setText(f"{last.title}: {_STATE_TEXT.get(last.state, last.state)}")
            else:
                self.label.setText("No active jobs")
            self.bar.hide()
            self.rate.setText("")
            self.btn.setText(f"Jobs ({len(self.manager.jobs)})" if self.manager.jobs else "Jobs")
            return
        running = [j for j in active if j.state == "running"]
        paused = [j for j in active if j.paused]
        total = sum(j.progress.bytes_total for j in running)
        done = sum(j.progress.bytes_done for j in running)
        speed = sum(j.progress.speed_bps for j in running if not j.paused)
        self.bar.show()
        self.bar.setValue(int(done / total * 1000) if total else 0)
        if len(active) == 1:
            self.label.setText(active[0].title + (" (paused)" if paused else ""))
        elif len(paused) == len(active):
            self.label.setText(f"{len(active)} jobs paused")
        else:
            parts = [f"{len(running) - len([j for j in running if j.paused])} running"]
            if paused:
                parts.append(f"{len(paused)} paused")
            queued = len(active) - len(running)
            if queued:
                parts.append(f"{queued} queued")
            self.label.setText(", ".join(parts))
        self.rate.setText("" if len(paused) == len(active) else human_rate(speed))
        self.btn.setText(f"Jobs ({len(active)})")

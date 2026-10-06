"""Top-right job progress panel: compact summary in the toolbar + popup with every job."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
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

    def __init__(self, job: Job, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job_id = job.id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        top = QHBoxLayout()
        self.title = QLabel(f"<b>{job.title}</b>")
        top.addWidget(self.title, 1)
        self.state = QLabel("")
        top.addWidget(self.state)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.clicked.connect(lambda: self.cancel_clicked.emit(self.job_id))
        top.addWidget(self.cancel_btn)
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

    def update_job(self, job: Job) -> None:
        p = job.progress
        self.bar.setValue(int(p.fraction * 1000))
        if job.state == "running":
            phase = p.phase.capitalize()
            self.state.setText(phase)
            self.detail.setText(
                f"{p.files_done}/{p.files_total} files  |  {human_rate(p.speed_bps)}  |  ETA {human_eta(p.eta_seconds)}"
                + (f"\n{p.current_file}" if p.current_file else "")
            )
        elif job.state == "queued":
            self.state.setText("Queued")
            self.detail.setText(job.message)
        else:
            self.state.setText(_STATE_TEXT.get(job.state, job.state))
            self.detail.setText(job.message)
            if job.state == "done":
                self.bar.setValue(1000)
        finished = job.state in ("done", "failed", "cancelled")
        self.cancel_btn.setVisible(not finished)
        self.result_btn.setVisible(finished and job.result is not None)
        self.setStyleSheet(
            "QFrame { border-left: 3px solid %s; }"
            % {"done": "#2ea043", "failed": "#d9534f", "cancelled": "#999999"}.get(job.state, "palette(highlight)")
        )


class JobListPopup(QFrame):
    def __init__(self, manager: JobManager, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.manager = manager
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(460)
        self.setMaximumHeight(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Jobs</b>"), 1)
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

    def add_job(self, job: Job, on_show_result) -> JobRow:
        row = JobRow(job)
        row.cancel_clicked.connect(self.manager.cancel)
        row.show_result.connect(on_show_result)
        self.rows_layout.insertWidget(0, row)
        self.rows[job.id] = row
        self.empty.hide()
        return row

    def update_job(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row:
            row.update_job(job)

    def clear_finished(self) -> None:
        for jid, row in list(self.rows.items()):
            job = self.manager.jobs.get(jid)
            if job and job.state in ("done", "failed", "cancelled"):
                self.rows_layout.removeWidget(row)
                row.deleteLater()
                del self.rows[jid]
        self.empty.setVisible(not self.rows)


class JobPanel(QWidget):
    """Compact toolbar widget: overall progress + button that opens the job list."""

    show_result = Signal(int)

    def __init__(self, manager: JobManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.popup = JobListPopup(manager, self)
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
        self.popup.adjustSize()
        pos = self.btn.mapToGlobal(QPoint(self.btn.width() - self.popup.width(), self.btn.height() + 4))
        self.popup.move(pos)
        self.popup.show()

    def _added(self, job: Job) -> None:
        self.popup.add_job(job, self.show_result.emit)
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
        total = sum(j.progress.bytes_total for j in running)
        done = sum(j.progress.bytes_done for j in running)
        speed = sum(j.progress.speed_bps for j in running)
        self.bar.show()
        self.bar.setValue(int(done / total * 1000) if total else 0)
        if len(active) == 1:
            self.label.setText(active[0].title)
        else:
            self.label.setText(f"{len(running)} running, {len(active) - len(running)} queued")
        self.rate.setText(human_rate(speed))
        self.btn.setText(f"Jobs ({len(active)})")

"""Main window: toolbar + job panel, sidebar | (media browser / production view)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QWidget,
)

from lumdit import APP_NAME, __version__
from lumdit.core import devices
from lumdit.core.jobs import Job, JobManager
from lumdit.core.mhl import VerifyResult, find_manifests
from lumdit.core.offload import OffloadRequest, OffloadResult
from lumdit.core.production import OffloadRecord, Production, ProductionError
from lumdit.logging_setup import log_dir, log_path
from lumdit.settings import Settings, cache_dir
from lumdit.ui.cardlog_panel import CardLogPanel
from lumdit.ui.dialogs.event_picker import EventPickerDialog
from lumdit.ui.dialogs.new_production import NewProductionDialog
from lumdit.ui.dialogs.offload_dialog import OffloadDialog
from lumdit.ui.dialogs.result_dialogs import OffloadResultDialog, VerifyResultDialog
from lumdit.ui.dialogs.settings_dialog import SettingsDialog
from lumdit.ui.drop_zone import DropZone
from lumdit.ui.event_backup import EventBackupController
from lumdit.ui.job_panel import JobPanel
from lumdit.ui.media_browser import MediaBrowser
from lumdit.ui.production_view import ProductionView
from lumdit.ui.sidebar import Sidebar
from lumdit.ui.thumbnail_loader import ThumbnailLoader
from lumdit.ui.welcome import WelcomePage

log = logging.getLogger(__name__)


def _recent_label(path: str) -> str:
    """'Client - Production' for a recent production folder."""
    try:
        return Production.load(Path(path)).display_name
    except Exception:
        return Path(path).name


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings()
        self.production: Production | None = None
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 900)

        self.jobs = JobManager(self.settings.max_concurrent_jobs, self)
        self.jobs.job_finished.connect(self._job_finished)
        self.loader = ThumbnailLoader(cache_dir(), parent=self)
        self._verify_batches: dict[int, list[int]] = {}  # batch id -> job ids
        self._verify_results: dict[int, list[VerifyResult]] = {}

        # ---- widgets ----
        self.welcome = WelcomePage()
        self.welcome.create_requested.connect(self.new_production)
        self.welcome.open_requested.connect(self.open_production)
        self.welcome.open_recent.connect(self.open_production_path)
        self.welcome.event_backup_requested.connect(self.event_backup_start)

        self.sidebar = Sidebar()
        self.browser = MediaBrowser(self.loader, self.settings.thumbnail_size, self.settings.show_hidden_files)
        self.production_view = ProductionView()
        self.cardlog_panel = CardLogPanel()
        self.drop_zone = DropZone()
        self.event_backup = EventBackupController(self.settings, self.jobs, self.cardlog_panel, self)

        self.sidebar.folder_selected.connect(self.browser.set_folder)
        self.sidebar.offload_requested.connect(lambda p: self.start_offload([p]))
        self.sidebar.devices.volume_added.connect(self.event_backup.on_volume_added)
        self.browser.folder_activated.connect(self._browser_navigate)
        self.browser.offload_requested.connect(lambda p: self.start_offload([p]))
        self.production_view.folder_activated.connect(self._browser_navigate)
        self.production_view.verify_requested.connect(self.verify_folder)
        self.production_view.add_category_requested.connect(self.add_category)
        self.drop_zone.folders_dropped.connect(self._folders_dropped)
        self.cardlog_panel.start_event_requested.connect(self.event_backup_start)
        self.cardlog_panel.folders_dropped.connect(self._folders_dropped)
        self.event_backup.message.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.event_backup.manual_offload_requested.connect(self._manual_offload_from_log)
        self.jobs.job_updated.connect(self.event_backup.on_job_updated)
        self.jobs.job_finished.connect(self.event_backup.on_job_finished)

        center = QSplitter(Qt.Orientation.Vertical)
        center.addWidget(self.browser)
        center.addWidget(self.production_view)
        center.setStretchFactor(0, 3)
        center.setStretchFactor(1, 2)
        self.right_splitter = center  # name kept for saved-state compatibility

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.cardlog_panel)
        right.addWidget(self.drop_zone)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        self.cardlog_splitter = right

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.addWidget(self.sidebar)
        main_split.addWidget(center)
        main_split.addWidget(right)
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 3)
        main_split.setStretchFactor(2, 2)
        main_split.setSizes([280, 700, 420])
        self.main_splitter = main_split

        self.stack = QStackedWidget()
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(main_split)
        self.setCentralWidget(self.stack)

        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self.statusBar().showMessage("Ready")

        self._restore_state()
        self.welcome.set_recent(self.settings.recent_productions)
        self._show_browser(False)

    # ---- construction ---------------------------------------------------------
    def _build_actions(self) -> None:
        self.act_new = QAction("New Production...", self)
        self.act_new.setShortcut(QKeySequence.StandardKey.New)
        self.act_new.triggered.connect(self.new_production)
        self.act_open = QAction("Open Production...", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_production)
        self.act_event = QAction("Event Backup...", self)
        self.act_event.setShortcut("Ctrl+E")
        self.act_event.setToolTip("Pick an event from the crew app's card log and be walked through each card")
        self.act_event.triggered.connect(self.event_backup_start)
        self.act_close = QAction("Close Production", self)
        self.act_close.triggered.connect(self.close_production)
        self.act_offload = QAction("Offload Folder...", self)
        self.act_offload.setShortcut("Ctrl+Shift+O")
        self.act_offload.triggered.connect(self._offload_pick)
        self.act_verify = QAction("Verify Folder...", self)
        self.act_verify.triggered.connect(self._verify_pick)
        self.act_settings = QAction("Settings...", self)
        self.act_settings.setShortcut(QKeySequence.StandardKey.Preferences)
        self.act_settings.triggered.connect(self.show_settings)
        self.act_refresh = QAction("Refresh", self)
        self.act_refresh.setShortcut(QKeySequence.StandardKey.Refresh)
        self.act_refresh.triggered.connect(self._refresh_all)
        self.act_quit = QAction("Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)
        self.act_about = QAction("About", self)
        self.act_about.triggered.connect(self._about)
        self.act_open_log = QAction("Open Log File", self)
        self.act_open_log.setToolTip(str(log_path()))
        self.act_open_log.triggered.connect(self._open_log)
        self.act_open_log_folder = QAction("Show Log Folder", self)
        self.act_open_log_folder.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))))

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        tb.addAction(self.act_event)
        tb.addAction(self.act_new)
        tb.addAction(self.act_open)
        tb.addSeparator()
        tb.addAction(self.act_offload)
        tb.addAction(self.act_verify)
        tb.addSeparator()
        tb.addAction(self.act_settings)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.job_panel = JobPanel(self.jobs, self.settings)
        self.job_panel.show_result.connect(self._show_job_result)
        self.job_panel.retry_requested.connect(self._retry_job)
        tb.addWidget(self.job_panel)
        self.addToolBar(tb)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        file_menu.addAction(self.act_event)
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_open)
        self.recent_menu = QMenu("Open Recent", self)
        file_menu.addMenu(self.recent_menu)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        tools = mb.addMenu("&Tools")
        tools.addAction(self.act_offload)
        tools.addAction(self.act_verify)
        tools.addAction(self.act_refresh)
        tools.addSeparator()
        tools.addAction(self.act_settings)
        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self.act_open_log)
        help_menu.addAction(self.act_open_log_folder)
        help_menu.addSeparator()
        help_menu.addAction(self.act_about)

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        for p in self.settings.recent_productions:
            act = self.recent_menu.addAction(f"{_recent_label(p)}  -  {p}")
            act.triggered.connect(lambda _c=False, path=p: self.open_production_path(path))
        if self.recent_menu.isEmpty():
            self.recent_menu.addAction("(none)").setEnabled(False)

    # ---- production lifecycle -----------------------------------------------------
    def new_production(self) -> None:
        dlg = NewProductionDialog(self.settings, self)
        if dlg.exec() == NewProductionDialog.DialogCode.Accepted and dlg.production:
            self._set_production(dlg.production)
            self.statusBar().showMessage(f"Created production at {dlg.production.root}", 8000)

    def open_production(self) -> None:
        start = self.settings.default_destination_root
        chosen = QFileDialog.getExistingDirectory(self, "Open production folder", start)
        if chosen:
            self.open_production_path(chosen)

    def open_production_path(self, path: Path | str) -> None:
        try:
            prod = Production.load(Path(path))
        except (ProductionError, OSError, ValueError, KeyError) as exc:
            log.warning("Cannot open production %s: %s", path, exc)
            QMessageBox.warning(self, "Cannot open production", f"{path}\n\n{exc}")
            return
        prod.ensure_structure()
        self._set_production(prod)

    def close_production(self) -> None:
        if self.jobs.active_count:
            QMessageBox.information(self, "Jobs running", "Wait for running jobs to finish before closing the production.")
            return
        self.production = None
        self.event_backup.detach()
        self.production_view.set_production(None)
        self.welcome.set_recent(self.settings.recent_productions)
        self._show_browser(False)
        self.setWindowTitle(APP_NAME)

    def _set_production(self, prod: Production, from_event: bool = False) -> None:
        self.production = prod
        self.settings.add_recent_production(prod.root)
        self.production_view.set_production(prod)
        self.drop_zone.set_enabled_state(True)
        self.setWindowTitle(f"{prod.display_name} - {APP_NAME}")
        self._show_browser(True)
        if not from_event:
            # Productions created from an event re-link to their card log automatically.
            self.event_backup.resume_for_production(prod)
        # Land the user on the first removable card if one is plugged in.
        cards = devices.removable_volumes()
        if cards:
            self._browser_navigate(cards[0].path)

    def _show_browser(self, on: bool) -> None:
        self.stack.setCurrentIndex(1 if on else 0)
        for a in (self.act_close, self.act_offload, self.act_refresh):
            a.setEnabled(on)
        if not on:
            self.drop_zone.set_enabled_state(False, "Create or open a production first")

    def add_category(self) -> None:
        if not self.production:
            return
        name, ok = QInputDialog.getText(self, "Add Folder", "Folder name (e.g. BTS, Audio, Drone):")
        if ok and name.strip():
            self.production.add_category(name)
            self.production_view.set_production(self.production)
            self.cardlog_panel.set_production_categories(self.production.categories)

    # ---- event backup (card log) ------------------------------------------------------
    def event_backup_start(self) -> None:
        if self.jobs.active_count and self.production:
            QMessageBox.information(self, "Jobs running", "Wait for running jobs to finish before switching events.")
            return
        if not self.settings.mongo_uri:
            QMessageBox.information(
                self,
                "Card log not configured",
                "Add the MongoDB connection string under Settings > Card Log (or a MONGO_URI in lumdit/.env).",
            )
            self.show_settings()
            if not self.settings.mongo_uri:
                return
        dlg = EventPickerDialog(self.settings, self)
        if dlg.exec() != EventPickerDialog.DialogCode.Accepted or dlg.choice is None:
            return
        try:
            prod = self.event_backup.start(dlg.choice)
        except (ProductionError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start event backup", str(exc))
            return
        self._set_production(prod, from_event=True)
        self.statusBar().showMessage(f"Event backup: {dlg.choice.event.title} -> {prod.root}", 8000)

    def _folders_dropped(self, folders: list) -> None:
        if not self.production:
            QMessageBox.information(self, "No production", "Create or open a production first.")
            return
        if self.event_backup.handle_dropped_folders([Path(f) for f in folders]):
            return
        self.start_offload(folders)

    def _manual_offload_from_log(self, source: Path, prefill: dict | None) -> None:
        if not self.production:
            return
        dlg = OffloadDialog(self.production, Path(source), self.settings, self, prefill=prefill)
        if dlg.exec() == OffloadDialog.DialogCode.Accepted and dlg.request is not None:
            job = self.jobs.submit_offload(dlg.request)
            if dlg.request.log_entry_id:
                self.event_backup.track_manual_job(job, dlg.request.log_entry_id, dlg.request.log_slot)
            self.statusBar().showMessage(f"Offload started: {job.title}", 6000)

    # ---- navigation -------------------------------------------------------------------
    def _browser_navigate(self, path: Path) -> None:
        self.browser.set_folder(path)
        self.sidebar.select_path(path)

    def _refresh_all(self) -> None:
        self.browser.refresh()
        self.production_view.refresh()
        self.sidebar.devices.refresh()

    # ---- offload ------------------------------------------------------------------------
    def _offload_pick(self) -> None:
        start = str(self.browser.current) if self.browser.current else ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a card folder to offload", start)
        if chosen:
            self.start_offload([Path(chosen)])

    def start_offload(self, sources: list) -> None:
        if not self.production:
            QMessageBox.information(self, "No production", "Create or open a production first.")
            return
        for src in sources:
            src = Path(src)
            if self.production.root == src or self.production.root in src.parents:
                QMessageBox.warning(self, "Invalid source", f"{src.name} is inside the production itself.")
                continue
            dlg = OffloadDialog(self.production, src, self.settings, self)
            if dlg.exec() != OffloadDialog.DialogCode.Accepted or dlg.request is None:
                continue
            job = self.jobs.submit_offload(dlg.request)
            self.statusBar().showMessage(f"Offload started: {job.title} -> {dlg.request.destination.name}", 6000)

    def _job_finished(self, job: Job) -> None:
        if job.kind == "offload" and isinstance(job.result, OffloadResult):
            self._offload_finished(job, job.result)
        elif job.kind == "verify":
            self._verify_job_finished(job)

    def _offload_finished(self, job: Job, result: OffloadResult) -> None:
        req = result.request
        if self.production and result.status in ("verified", "issues"):
            try:
                self.production.record_offload(
                    OffloadRecord(
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                        category=req.category,
                        date=req.shoot_date.isoformat() if req.shoot_date else "",
                        camera=req.camera,
                        operator=req.operator,
                        card=req.card_number,
                        destination=str(req.destination),
                        source=str(req.source),
                        source_label=req.source_label,
                        fingerprint=result.fingerprint,
                        file_count=len(result.files),
                        total_bytes=result.total_bytes,
                        status=result.status,
                        log_entry_id=req.log_entry_id,
                        log_slot=req.log_slot,
                        include_roots=list(req.include_roots),
                    )
                )
            except OSError as exc:
                log.exception("Could not update production.json for %s", self.production.root)
                self.statusBar().showMessage(f"Could not update production.json: {exc}", 8000)
        self.production_view.refresh()
        self.production_view.reveal(req.destination)
        if result.status == "verified":
            self.statusBar().showMessage(f"Verified: {job.title} ({result.copied} files)", 10000)
            if self.settings.eject_after_offload:
                vol = devices.volume_for_path(req.source)
                if vol and vol.removable and not any(
                    j.state in ("running", "queued") and j.device_key == job.device_key for j in self.jobs.jobs.values()
                ):
                    QTimer.singleShot(1000, lambda v=vol: self.sidebar.devices.eject(v))
        else:
            self.statusBar().showMessage(f"{job.title}: {result.message}", 10000)
            if result.status in ("issues", "error"):
                self._show_offload_result(result)

    def _show_offload_result(self, result: OffloadResult) -> None:
        dlg = OffloadResultDialog(result, self)
        dlg.repair_requested.connect(self.resubmit_offload)
        dlg.show()

    def _show_verify_results(self, results: list[VerifyResult]) -> None:
        dlg = VerifyResultDialog(results, self)
        dlg.repair_requested.connect(self.repair_folder)
        dlg.show()

    def _show_job_result(self, job_id: int) -> None:
        job = self.jobs.jobs.get(job_id)
        if not job or job.result is None:
            return
        if isinstance(job.result, OffloadResult):
            self._show_offload_result(job.result)
        elif isinstance(job.result, VerifyResult):
            self._show_verify_results([job.result])

    def _retry_job(self, job_id: int) -> None:
        """'Retry' from the job list: re-run an offload that failed, was cancelled or had issues."""
        job = self.jobs.jobs.get(job_id)
        if not job or job.kind != "offload":
            return
        if isinstance(job.result, OffloadResult) and job.result.problems:
            # Let the result dialog ask what to do about differing files.
            self._show_offload_result(job.result)
            return
        self.resubmit_offload(job.payload, False)

    def resubmit_offload(self, request: OffloadRequest, repair_conflicts: bool) -> None:
        """Run *request* again. The engine skips verified files, copies missing/failed ones and,
        in repair mode, moves differing destination files to _CONFLICTS before recopying."""
        req = replace(request, repair_conflicts=repair_conflicts)
        if not Path(req.source).is_dir():
            QMessageBox.warning(
                self,
                "Card not available",
                f"The source is not mounted:\n{req.source}\n\nInsert the card and try again"
                + (f" ({req.source_label})." if req.source_label else "."),
            )
            return
        if self.jobs.active_count and any(
            j.state in ("running", "queued") and isinstance(j.payload, OffloadRequest) and j.payload.destination == req.destination
            for j in self.jobs.jobs.values()
        ):
            QMessageBox.information(self, "Already running", f"A job for {req.destination.name} is already in progress.")
            return
        job = self.jobs.submit_offload(req)
        if req.log_entry_id and self.event_backup.active:
            self.event_backup.track_manual_job(job, req.log_entry_id, req.log_slot)
        log.info("Resubmitted offload %s (repair_conflicts=%s) as job %s", req.title, repair_conflicts, job.id)
        verb = "Repair" if repair_conflicts else ("Resume" if request.repair_conflicts is False and self._was_cancelled(request) else "Retry")
        self.statusBar().showMessage(f"{verb} started: {job.title}", 6000)

    def _was_cancelled(self, request: OffloadRequest) -> bool:
        return any(
            j.state == "cancelled" and isinstance(j.payload, OffloadRequest) and j.payload.destination == request.destination
            for j in self.jobs.jobs.values()
        )

    def repair_folder(self, folder: Path) -> None:
        """Repair a card folder whose manifest verification failed, using its recorded offload."""
        if not self.production:
            QMessageBox.information(self, "No production", "Open the production this folder belongs to first.")
            return
        rec = self.production.offload_for_destination(folder)
        if rec is None:
            QMessageBox.information(
                self,
                "No offload record",
                f"LumDIT has no record of how {Path(folder).name} was offloaded, so it cannot repair it "
                "automatically. Offload the card again manually to the same folder; verified files are skipped.",
            )
            return
        shoot = date.fromisoformat(rec.date) if rec.date else None
        req = OffloadRequest(
            source=Path(rec.source),
            destination=Path(rec.destination),
            client_name=self.production.client,
            production_name=self.production.name,
            category=rec.category,
            shoot_date=shoot,
            camera=rec.camera,
            operator=rec.operator,
            card_number=rec.card,
            source_label=rec.source_label,
            log_entry_id=rec.log_entry_id,
            log_slot=rec.log_slot,
            include_roots=tuple(rec.include_roots),
        )
        self.resubmit_offload(req, True)

    # ---- verify ------------------------------------------------------------------------
    def _verify_pick(self) -> None:
        start = str(self.production.root) if self.production else ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder to verify against its MHL manifests", start)
        if chosen:
            self.verify_folder(Path(chosen))

    def verify_folder(self, folder: Path) -> None:
        manifests = find_manifests(Path(folder))
        if not manifests:
            QMessageBox.information(self, "Nothing to verify", f"No .mhl manifests found under\n{folder}")
            return
        # Only the newest manifest per card folder is authoritative.
        newest: dict[Path, Path] = {}
        for m in manifests:
            cur = newest.get(m.parent)
            if cur is None or m.stat().st_mtime > cur.stat().st_mtime:
                newest[m.parent] = m
        batch_id = max(self._verify_batches, default=0) + 1
        self._verify_batches[batch_id] = []
        self._verify_results[batch_id] = []
        for m in newest.values():
            job = self.jobs.submit_verify(m)
            self._verify_batches[batch_id].append(job.id)
        self.statusBar().showMessage(f"Verifying {len(newest)} card folder(s)...", 6000)

    def _verify_job_finished(self, job: Job) -> None:
        for batch_id, ids in list(self._verify_batches.items()):
            if job.id in ids:
                ids.remove(job.id)
                if isinstance(job.result, VerifyResult):
                    self._verify_results[batch_id].append(job.result)
                if not ids:
                    results = self._verify_results.pop(batch_id)
                    del self._verify_batches[batch_id]
                    if results:
                        self._show_verify_results(results)
                break

    # ---- misc ------------------------------------------------------------------------
    def show_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self.jobs.set_max_concurrent(self.settings.max_concurrent_jobs)
            self.browser.show_hidden = self.settings.show_hidden_files
            self.browser.size_slider.setValue(self.settings.thumbnail_size)
            self.browser.refresh()

    def _open_log(self) -> None:
        p = log_path()
        if not p.exists():
            QMessageBox.information(self, "Log file", f"No log file yet.\n\nIt will be written to:\n{p}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br>Verified media offload for DITs and photo/video teams.<br><br>"
            "Checksums: xxHash64 with read-back verification<br>Manifests: MHL 1.1 + .xxh64 sidecar<br><br>"
            f"<span style='color:gray'>Log: {log_path()}</span>",
        )

    # ---- state ------------------------------------------------------------------------
    def _restore_state(self) -> None:
        g = self.settings.geometry("geometry")
        if isinstance(g, QByteArray):
            self.restoreGeometry(g)
        for name, splitter in (
            ("main_splitter_v2", self.main_splitter),
            ("right_splitter", self.right_splitter),
            ("cardlog_splitter", self.cardlog_splitter),
        ):
            s = self.settings.geometry(name)
            if isinstance(s, QByteArray):
                splitter.restoreState(s)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.jobs.active_count:
            paused = self.jobs.paused_count
            res = QMessageBox.question(
                self,
                "Jobs running",
                f"{self.jobs.active_count} job(s) are still running"
                + (f" ({paused} paused)" if paused else "")
                + ". Quit and cancel them?\n\n"
                "Partially copied files will be removed; completed files are kept and can be resumed later.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.jobs.shutdown()
        self.settings.save_geometry("geometry", self.saveGeometry())
        self.settings.save_geometry("main_splitter_v2", self.main_splitter.saveState())
        self.settings.save_geometry("right_splitter", self.right_splitter.saveState())
        self.settings.save_geometry("cardlog_splitter", self.cardlog_splitter.saveState())
        self.event_backup.detach()
        super().closeEvent(event)

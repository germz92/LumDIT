"""Walk-through controller for backing up an event from its card log.

Owns: the linked event + production, which card is currently being asked for,
per-card statuses, the "card detected" / "offload as ...?" prompts, request
building from log entries, and the MongoDB write-back (with an offline retry
queue stored in production.json).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QWidget

from lumdit.core import devices
from lumdit.core.cardlayout import CardLayout, MediaRoot, detect_layout, looks_like_card_root
from lumdit.core.cardlog import CardLogClient, CardLogEntry, CardLogEvent
from lumdit.core.jobs import Job, JobManager
from lumdit.core.offload import OffloadRequest, OffloadResult
from lumdit.core.production import Production, find_or_create_for_event
from lumdit.settings import Settings
from lumdit.ui.async_util import run_async
from lumdit.ui.cardlog_panel import CardLogPanel
from lumdit.ui.dialogs.event_picker import EventChoice

ACTIVE_STATES = ("queued", "running")


class EventBackupController(QObject):
    production_ready = Signal(object)  # Production - open/switch to it
    message = Signal(str)  # status-bar text
    manual_offload_requested = Signal(object, object)  # source Path, prefill dict | None

    def __init__(self, settings: Settings, jobs: JobManager, panel: CardLogPanel, parent: QWidget) -> None:
        super().__init__(parent)
        self.settings = settings
        self.jobs = jobs
        self.panel = panel
        self._parent = parent
        self.log_event: CardLogEvent | None = None
        self.production: Production | None = None
        self.target: tuple[str, int] | None = None
        self.skipped: set[tuple[str, int]] = set()
        self.statuses: dict[tuple[str, int], str] = {}
        self._job_links: dict[int, tuple[str, int]] = {}
        self._syncing: set[tuple[str, int]] = set()
        self._refreshing = False

        self._timer = QTimer(self)
        self._timer.setInterval(max(15, settings.cardlog_refresh_seconds) * 1000)
        self._timer.timeout.connect(self.refresh)

        panel.refresh_requested.connect(self.refresh)
        panel.target_changed.connect(self.set_target)
        panel.choose_folder_requested.connect(self.choose_folder)
        panel.skip_requested.connect(self.skip)

    # ---- lifecycle ---------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.log_event is not None and self.production is not None

    def start(self, choice: EventChoice) -> Production:
        ev = choice.event
        prod = find_or_create_for_event(
            choice.destination_root,
            ev.id,
            ev.title,
            choice.client,
            choice.name,
            ev.start or date.today(),
            ev.end or ev.start or date.today(),
            ev.categories(),
            self.settings.card_folder_template,
        )
        self.attach(ev, prod)
        return prod

    def attach(self, event: CardLogEvent, production: Production) -> None:
        self.log_event = event
        self.production = production
        self.target = None
        self.skipped.clear()
        self.statuses = {}
        for w in production.pending_writebacks:
            self.statuses[(str(w.get("entry_id")), int(w.get("slot") or 1))] = "verified_unsynced"
        for job_id, link in self._job_links.items():
            job = self.jobs.jobs.get(job_id)
            if job and job.state in ACTIVE_STATES:
                self.statuses[link] = job.state
        self.panel.set_production_categories(production.categories)
        self.panel.set_event(event, self.statuses)
        self.panel.set_note("")
        self.advance("insert")
        self._timer.start()
        self._retry_writebacks()

    def detach(self) -> None:
        self._timer.stop()
        self.log_event = None
        self.production = None
        self.target = None
        self.statuses = {}
        self.panel.set_event(None)

    def resume_for_production(self, production: Production) -> None:
        """Re-link a production that was created from an event in an earlier session."""
        if not production.event_id:
            self.detach()
            return
        if self.log_event is not None and self.log_event.id == production.event_id and self.production is production:
            return
        self.production = production
        self.panel.set_note(f"Loading card log for {production.event_title or 'event'}...")

        def work():
            client = self._client()
            try:
                return client.get_event(production.event_id)
            finally:
                client.close()

        def done(ev: CardLogEvent) -> None:
            if self.production is production:
                self.attach(ev, production)

        def failed(msg: str) -> None:
            self.panel.set_note(f"Card log unavailable: {msg}")

        run_async(work, done, failed)

    def _client(self) -> CardLogClient:
        return CardLogClient(self.settings.mongo_uri, self.settings.mongo_db, self.settings.mongo_collection)

    # ---- refresh -----------------------------------------------------------------
    def refresh(self) -> None:
        if not self.active or self._refreshing:
            return
        self._refreshing = True
        event_id = self.log_event.id

        def work():
            client = self._client()
            try:
                return client.get_event(event_id)
            finally:
                client.close()

        def done(ev: CardLogEvent) -> None:
            self._refreshing = False
            if not self.active or self.log_event.id != ev.id:
                return
            self.log_event = ev
            # DB says backed up -> drop our local "unsynced" marker.
            for key in [k for k, s in self.statuses.items() if s == "verified_unsynced"]:
                e = ev.find_entry(key[0])
                if e is not None and e.backed_up(key[1]):
                    del self.statuses[key]
                    self.production.clear_writeback(*key)
            self.panel.set_event(ev, self.statuses)
            self.panel.set_note("")
            if self.target is None or not self._is_promptable(*self.target):
                self.advance("insert")
            else:
                self.panel.set_target(*self.target)
            self._retry_writebacks()

        def failed(msg: str) -> None:
            self._refreshing = False
            self.panel.set_note(f"Card log refresh failed: {msg}")

        run_async(work, done, failed)

    # ---- targeting -----------------------------------------------------------------
    def _is_promptable(self, entry_id: str, slot: int) -> bool:
        e = self.log_event.find_entry(entry_id) if self.log_event else None
        if e is None or not e.offloadable(slot) or e.backed_up(slot):
            return False
        return self.statuses.get((entry_id, slot)) not in (*ACTIVE_STATES, "verified_unsynced")

    def _next_pending(self) -> CardLogEntry | None:
        if not self.log_event:
            return None
        for e in self.log_event.pending_entries(1):
            if (e.id, 1) in self.skipped:
                continue
            if self._is_promptable(e.id, 1):
                return e
        return None

    def advance(self, prompt: str = "insert") -> None:
        nxt = self._next_pending()
        self.target = (nxt.id, 1) if nxt else None
        self.panel.set_target(nxt.id if nxt else None, 1, prompt)

    def set_target(self, entry_id: str, slot: int) -> None:
        if not self.log_event:
            return
        self.skipped.discard((entry_id, slot))
        self.target = (entry_id, slot)
        self.panel.set_target(entry_id, slot, "insert")

    def skip(self) -> None:
        if self.target:
            self.skipped.add(self.target)
        self.advance("insert")

    def _target_entry(self) -> tuple[CardLogEntry, int] | None:
        if not self.log_event or not self.target:
            return None
        e = self.log_event.find_entry(self.target[0])
        return (e, self.target[1]) if e else None

    # ---- sources: detected card / dropped folder / chosen folder -------------------
    def choose_folder(self) -> None:
        tgt = self._target_entry()
        if not tgt:
            return
        chosen = QFileDialog.getExistingDirectory(self._parent, f"Choose the folder for card {tgt[0].display_card(tgt[1])}")
        if chosen:
            self.offload_source(Path(chosen), *tgt)

    def on_volume_added(self, volume: devices.Volume) -> None:
        if not self.active:
            return
        tgt = self._target_entry()
        if not tgt:
            return
        self._prompt_for_source(volume.path, f"Card detected: {volume.display_name}", tgt)

    def handle_dropped_folders(self, folders: list[Path]) -> bool:
        """Return True if the drop was consumed by the walk-through."""
        if not self.active:
            return False
        tgt = self._target_entry()
        if not tgt:
            return False
        for folder in folders:
            self._prompt_for_source(Path(folder), f"Folder dropped: {folder}", tgt)
        return True

    def _prompt_for_source(self, source: Path, headline: str, tgt: tuple[CardLogEntry, int]) -> None:
        entry, slot = tgt
        category = self._category_for(entry)
        layout, roots = self.plan_roots(source, category)
        box = QMessageBox(self._parent)
        box.setWindowTitle("Offload card")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(headline)
        info = [
            f"Offload it as:\n\n{entry.describe(slot)}\n",
            f"Destination: {self._destination_for(entry, slot, category).relative_to(self.production.root).as_posix()}",
        ]
        left_behind: list = []
        if roots:
            info.append("Copying: " + "; ".join(r.summary() for r in roots))
            left_behind = layout.excluded_media(roots)
            if left_behind:
                info.append(
                    "Also on this card (NOT copied for " + category + "): " + "; ".join(r.summary() for r in left_behind)
                )
        elif layout is not None and layout.recognised:
            info.append("No " + category.lower() + " media found in the usual folders - the whole card will be copied.")
        box.setInformativeText("\n".join(info))
        offload = box.addButton("Offload", QMessageBox.ButtonRole.AcceptRole)
        everything = box.addButton("Offload entire card", QMessageBox.ButtonRole.ActionRole) if left_behind else None
        other = box.addButton("Different card...", QMessageBox.ButtonRole.ActionRole)
        manual = box.addButton("Manual offload...", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Ignore", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(offload)
        box.exec()
        clicked = box.clickedButton()
        if clicked == offload:
            self.offload_source(source, entry, slot, include_roots=tuple(r.rel for r in roots))
        elif everything is not None and clicked == everything:
            self.offload_source(source, entry, slot, include_roots=())
        elif clicked == other:
            picked = self._pick_entry()
            if picked:
                self.set_target(picked[0].id, picked[1])
                _, roots2 = self.plan_roots(source, self._category_for(picked[0]))
                self.offload_source(source, *picked, include_roots=tuple(r.rel for r in roots2))
        elif clicked == manual:
            self.manual_offload_requested.emit(source, self.prefill_for(entry, slot))

    @staticmethod
    def plan_roots(source: Path, category: str) -> tuple[CardLayout | None, list[MediaRoot]]:
        """Decide which top-level card folders to copy for *category*.

        Returns (layout, roots). An empty *roots* list means copy the whole source - either
        because the source is not a card root (user dropped a sub-folder) or the layout is
        not one we recognise (Blackmagic / RED / ARRI write clips at the root).
        """
        source = Path(source)
        if not looks_like_card_root(source):
            return None, []
        layout = detect_layout(source)
        return layout, layout.roots_for(category)

    def _pick_entry(self) -> tuple[CardLogEntry, int] | None:
        if not self.log_event:
            return None
        options: list[tuple[str, CardLogEntry, int]] = []
        for e in self.log_event.eligible_entries(1):
            if not e.backed_up(1):
                options.append((e.describe(1), e, 1))
        for e in self.log_event.eligible_entries(2):
            if not e.backed_up(2):
                options.append((e.describe(2) + "   [card 2]", e, 2))
        if not options:
            QMessageBox.information(self._parent, "No cards", "Every card in this log is already backed up.")
            return None
        label, ok = QInputDialog.getItem(
            self._parent, "Which card is this?", "Card log entry:", [o[0] for o in options], 0, False
        )
        if not ok:
            return None
        return next(((e, s) for lbl, e, s in options if lbl == label), None)

    # ---- request building -----------------------------------------------------------
    def _category_for(self, entry: CardLogEntry) -> str:
        cat = entry.category or self.panel.chosen_category() or "Photo"
        return cat

    def _destination_for(self, entry: CardLogEntry, slot: int, category: str) -> Path:
        day = entry.day or self.production.start_date
        return self.production.card_dir(
            category, day, entry.camera or "Camera", self.log_event.operator_name(entry), entry.card_value(slot) or "Card"
        )

    def prefill_for(self, entry: CardLogEntry, slot: int) -> dict:
        return {
            "camera": entry.camera,
            "operator": self.log_event.operator_name(entry) if self.log_event else entry.first_name,
            "category": self._category_for(entry),
            "date": entry.day,
            "card": entry.card_value(slot),
            "log_entry_id": entry.id,
            "log_slot": slot,
            "note": f"From card log: {entry.describe(slot)}",
        }

    def offload_source(
        self, source: Path, entry: CardLogEntry, slot: int, include_roots: tuple[str, ...] | None = None
    ) -> Job | None:
        if not self.active:
            return None
        if include_roots is None:
            # Called without a plan (e.g. "Choose folder..."): apply the same category rule.
            _, roots = self.plan_roots(source, self._category_for(entry))
            include_roots = tuple(r.rel for r in roots)
        category = self._category_for(entry)
        if category not in self.production.categories:
            self.production.add_category(category)
            self.panel.set_production_categories(self.production.categories)
        if self.production.root == source or self.production.root in source.parents:
            QMessageBox.warning(self._parent, "Invalid source", "That folder is inside the production itself.")
            return None
        dest = self._destination_for(entry, slot, category)
        if dest.exists() and any(dest.iterdir()):
            res = QMessageBox.question(
                self._parent,
                "Destination exists",
                f"{dest.name} already contains files.\n\nIdentical files will be skipped and different files flagged; "
                "nothing will be overwritten. Continue?",
            )
            if res != QMessageBox.StandardButton.Yes:
                return None
        vol = devices.volume_for_path(source)
        request = OffloadRequest(
            source=Path(source),
            destination=dest,
            client_name=self.production.client,
            production_name=self.production.name,
            category=category,
            shoot_date=entry.day or self.production.start_date,
            camera=entry.camera or "Camera",
            operator=self.log_event.operator_name(entry),
            card_number=entry.card_value(slot) or "Card",
            source_label=vol.label if vol else "",
            log_entry_id=entry.id,
            log_slot=slot,
            include_roots=tuple(include_roots),
        )
        job = self.jobs.submit_offload(request)
        self._job_links[job.id] = (entry.id, slot)
        self.statuses[(entry.id, slot)] = job.state if job.state in ACTIVE_STATES else "queued"
        self.panel.set_status(entry.id, slot, self.statuses[(entry.id, slot)])
        self.message.emit(f"Offload started: {request.title}")
        # Move on immediately so the DIT can insert the next card while this one copies.
        if self.target == (entry.id, slot):
            self.advance("next")
        return job

    # ---- job events ---------------------------------------------------------------------
    def track_manual_job(self, job: Job, entry_id: str, slot: int) -> None:
        """Link a job started from the manual dialog (pre-filled from a log entry) to its card."""
        self._job_links[job.id] = (entry_id, slot)
        self.statuses[(entry_id, slot)] = job.state if job.state in ACTIVE_STATES else "queued"
        self.panel.set_status(entry_id, slot, self.statuses[(entry_id, slot)])
        if self.target == (entry_id, slot):
            self.advance("next")

    def on_job_updated(self, job: Job) -> None:
        link = self._job_links.get(job.id)
        if link and job.state in ACTIVE_STATES and self.statuses.get(link) != job.state:
            self.statuses[link] = job.state
            self.panel.set_status(link[0], link[1], job.state)

    def on_job_finished(self, job: Job) -> None:
        link = self._job_links.pop(job.id, None)
        if not link:
            return
        entry_id, slot = link
        result = job.result if isinstance(job.result, OffloadResult) else None
        if result is not None and result.status == "verified":
            self.statuses[link] = "verified_unsynced"
            self.panel.set_status(entry_id, slot, "verified_unsynced")
            self._mark_backed_up(entry_id, slot)
        else:
            status = {"issues": "issues", "cancelled": "cancelled"}.get(result.status if result else "", "failed")
            self.statuses[link] = status
            self.panel.set_status(entry_id, slot, status)
            if self.target is None:
                self.advance("insert")

    # ---- write-back --------------------------------------------------------------------------
    def _mark_backed_up(self, entry_id: str, slot: int) -> None:
        if not self.active or (entry_id, slot) in self._syncing:
            return
        self._syncing.add((entry_id, slot))
        event_id = self.log_event.id
        production = self.production

        def work():
            client = self._client()
            try:
                return client.mark_backed_up(event_id, entry_id, slot)
            finally:
                client.close()

        def done(matched: bool) -> None:
            self._syncing.discard((entry_id, slot))
            if self.log_event and self.log_event.id == event_id:
                e = self.log_event.find_entry(entry_id)
                if e is not None:
                    e.set_backed_up(slot)
                self.statuses.pop((entry_id, slot), None)
                self.panel.set_status(entry_id, slot, "backed_up")
                if not matched:
                    self.panel.set_note("Card log entry not found in the database - it may have been deleted in the crew app.")
            production.clear_writeback(entry_id, slot)
            self.message.emit(f"Card log updated: card marked as backed up")
            self._update_pending_note()

        def failed(msg: str) -> None:
            self._syncing.discard((entry_id, slot))
            production.queue_writeback(entry_id, slot)
            self.statuses[(entry_id, slot)] = "verified_unsynced"
            self.panel.set_status(entry_id, slot, "verified_unsynced")
            self._update_pending_note(msg)

        run_async(work, done, failed)

    def _retry_writebacks(self) -> None:
        if not self.active:
            return
        for w in list(self.production.pending_writebacks):
            self._mark_backed_up(str(w.get("entry_id")), int(w.get("slot") or 1))

    def _update_pending_note(self, error: str = "") -> None:
        if not self.production:
            return
        n = len(self.production.pending_writebacks)
        if n:
            self.panel.set_note(
                f"{n} verified card(s) not yet marked in the crew app" + (f" - {error}" if error else "") + ". Will retry on refresh."
            )
        else:
            self.panel.set_note("")

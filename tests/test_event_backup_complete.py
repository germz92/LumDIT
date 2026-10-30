"""Event-backup completion prompt (no Mongo)."""

from datetime import date
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from lumdit.core.cardlog import parse_event
from lumdit.core.jobs import JobManager
from lumdit.core.production import create_production
from lumdit.settings import Settings
from lumdit.ui.cardlog_panel import CardLogPanel
from lumdit.ui.event_backup import EventBackupController
from tests.test_cardlog import EVENT_DOC


def test_announce_complete_once(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    ev = parse_event(EVENT_DOC)
    for e in ev.eligible_entries(1):
        e.set_backed_up(1)
    prod = create_production(tmp_path, "Acme", "Launch", date(2026, 9, 21), date(2026, 9, 23), ["Photo"])
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *a, **k: shown.append(str(a[2] if len(a) > 2 else k.get("text", "")))),
    )
    parent = QWidget()
    panel = CardLogPanel()
    jobs = JobManager()
    ctrl = EventBackupController(Settings(), jobs, panel, parent)
    ctrl.log_event = ev
    ctrl.production = prod

    ctrl._maybe_announce_complete()
    assert len(shown) == 1
    assert "fully backed up" in shown[0]
    assert str(prod.root.resolve()) in shown[0]
    assert f"{ev.progress(1)[0]} of {ev.progress(1)[1]} card 1s" in shown[0]

    ctrl._maybe_announce_complete()
    assert len(shown) == 1  # not shown again

    # Incomplete event: no prompt.
    ev2 = parse_event(EVENT_DOC)
    ctrl.log_event = ev2
    ctrl._completion_announced = False
    ctrl._maybe_announce_complete()
    assert len(shown) == 1
    jobs.shutdown()
    parent.deleteLater()
    panel.deleteLater()
    app.processEvents()

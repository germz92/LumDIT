"""Event-backup insert prompt: one card must not offload as two log entries."""

from datetime import date
from pathlib import Path

from PySide6.QtWidgets import QApplication, QWidget

from lumdit.core.devices import Volume, normalize_mountpoint, parse_diskutil_info
from lumdit.core.jobs import Job, JobManager
from lumdit.core.production import create_production
from lumdit.settings import Settings
from lumdit.ui.cardlog_panel import CardLogPanel
from lumdit.ui.event_backup import EventBackupController
from tests.test_cardlog import EVENT_DOC
from lumdit.core.cardlog import parse_event


def _vol(mount: str, *, label: str = "SONY", identity: str = "", total: int = 10**9) -> Volume:
    return Volume(
        mountpoint=mount, label=label, fstype="exfat", total=total, free=total // 2, removable=True, identity=identity
    )


def _controller(tmp_path: Path) -> tuple[EventBackupController, object, object]:
    QApplication.instance() or QApplication([])
    ev = parse_event(EVENT_DOC)
    prod = create_production(tmp_path, "Acme", "Launch", date(2026, 9, 21), date(2026, 9, 23), ["Photo", "Video"])
    parent = QWidget()
    panel = CardLogPanel()
    jobs = JobManager()
    ctrl = EventBackupController(Settings(), jobs, panel, parent)
    ctrl.log_event = ev
    ctrl.production = prod
    pending = ev.pending_entries(1)[0]
    ctrl.target = (pending.id, 1)
    return ctrl, pending, ev


def test_normalize_drive_letter_forms():
    assert normalize_mountpoint("E:\\") == normalize_mountpoint("E:") == normalize_mountpoint("e:/")


def test_second_insert_of_same_card_does_not_prompt(tmp_path: Path, monkeypatch) -> None:
    ctrl, pending, _ev = _controller(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(ctrl, "_run_source_prompt", lambda source, headline, tgt: shown.append(tgt[0].id))
    card = tmp_path / "CARD"
    card.mkdir()
    vol = _vol(str(card))

    ctrl.on_volume_added(vol)
    ctrl.on_volume_added(vol)
    ctrl.on_volume_added(_vol(str(card)))
    assert shown == [pending.id]
    assert ctrl.target == (pending.id, 1)

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()


def test_windows_remount_does_not_prompt_next_card(tmp_path: Path, monkeypatch) -> None:
    ctrl, pending, ev = _controller(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(ctrl, "_run_source_prompt", lambda source, headline, tgt: shown.append(tgt[0].id))
    card = tmp_path / "CARD"
    card.mkdir()
    vol = _vol(str(card))

    ctrl.on_volume_added(vol)
    assert shown == [pending.id]
    # Pretend the first prompt offloaded and the walk-through moved on.
    nxt = ev.pending_entries(1)[1]
    ctrl.target = (nxt.id, 1)
    ctrl.on_volume_removed(vol)
    ctrl.on_volume_added(vol)  # remount of the same card, still in the reader
    assert shown == [pending.id]
    assert ctrl.target == (nxt.id, 1)
    assert ctrl._source_key(card) in ctrl._claimed_keys

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()


def test_offload_source_refuses_duplicate_entry_and_source(tmp_path: Path) -> None:
    ctrl, pending, ev = _controller(tmp_path)
    card = tmp_path / "CARD"
    card.mkdir()
    before = ctrl.target
    ctrl.statuses[(pending.id, 1)] = "running"
    assert ctrl.offload_source(card, pending, 1) is None
    assert ctrl.target == before

    ctrl.statuses.pop((pending.id, 1))
    other = ev.pending_entries(1)[1]
    job = Job(id=99, kind="offload", title="t", device_key=ctrl._source_key(card), payload=None)
    job.state = "running"
    ctrl.jobs.jobs[99] = job
    ctrl.target = (other.id, 1)
    assert ctrl.offload_source(card, other, 1) is None
    assert ctrl.target == (other.id, 1)

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()


def test_mac_untitled_renamed_to_label_does_not_prompt_again(tmp_path: Path, monkeypatch) -> None:
    """macOS remounts /Volumes/Untitled -> /Volumes/EOS_DIGITAL; same disk4s1."""
    ctrl, pending, ev = _controller(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(ctrl, "_run_source_prompt", lambda source, headline, tgt: shown.append(tgt[0].id))
    untitled = tmp_path / "Untitled"
    labeled = tmp_path / "EOS_DIGITAL"
    untitled.mkdir()
    labeled.mkdir()
    first = _vol(str(untitled), label="Untitled", identity="disk4s1")
    second = _vol(str(labeled), label="EOS_DIGITAL", identity="disk4s1")

    ctrl.on_volume_added(first)
    nxt = ev.pending_entries(1)[1]
    ctrl.target = (nxt.id, 1)
    ctrl.on_volume_removed(first)
    ctrl.on_volume_added(second)
    assert shown == [pending.id]
    assert ctrl.target == (nxt.id, 1)

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()


def test_different_card_same_size_still_prompts(tmp_path: Path, monkeypatch) -> None:
    ctrl, pending, ev = _controller(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(ctrl, "_run_source_prompt", lambda source, headline, tgt: shown.append(tgt[0].id))
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    first = _vol(str(a), identity="disk4s1", total=64_000_000_000)
    other = _vol(str(b), identity="disk5s1", total=64_000_000_000)

    ctrl.on_volume_added(first)
    nxt = ev.pending_entries(1)[1]
    ctrl.target = (nxt.id, 1)
    ctrl._release_claim(ctrl._source_key(a))
    for k in list(ctrl._claimed_keys):
        ctrl._release_claim(k)
    ctrl.on_volume_added(other)
    assert shown == [pending.id, nxt.id]

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()


def test_parse_diskutil_info_identity_and_removable() -> None:
    text = """
   Device Identifier:        disk4s1
   Device Node:              /dev/disk4s1
   Volume Name:              EOS_DIGITAL
   Volume UUID:              12345678-ABCD-EF00-1234-567890ABCDEF
   File System Personality:  ExFAT
   Removable Media:          Removable
   Ejectable:                Yes
"""
    removable, ident = parse_diskutil_info(text)
    assert removable
    assert ident == "disk4s1"
    fixed, ident2 = parse_diskutil_info("   Removable Media:          Non-Removable\n   Ejectable:                No\n")
    assert not fixed
    assert ident2 == ""


def test_release_claim_allows_next_physical_card(tmp_path: Path, monkeypatch) -> None:
    ctrl, pending, ev = _controller(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(ctrl, "_run_source_prompt", lambda source, headline, tgt: shown.append(tgt[0].id))
    card = tmp_path / "CARD"
    card.mkdir()
    vol = _vol(str(card))
    ctrl.on_volume_added(vol)
    nxt = ev.pending_entries(1)[1]
    ctrl.target = (nxt.id, 1)
    for k in list(ctrl._claimed_keys):
        ctrl._release_claim(k)
    ctrl.on_volume_added(vol)
    assert shown == [pending.id, nxt.id]

    ctrl.jobs.shutdown()
    ctrl._parent.deleteLater()
    ctrl.panel.deleteLater()

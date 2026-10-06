import os
import threading
from datetime import date
from pathlib import Path

import pytest

from lumdit.core.hashing import xxh64_file
from lumdit.core.mhl import find_manifests, read_mhl, verify_manifest
from lumdit.core.offload import (
    REPORT_NAME,
    OffloadEngine,
    OffloadRequest,
    is_junk,
    scan_source,
)


def make_card(root: Path) -> Path:
    card = root / "CARD"
    (card / "DCIM" / "100MSDCF").mkdir(parents=True)
    (card / "PRIVATE" / "M4ROOT" / "CLIP").mkdir(parents=True)
    (card / "DCIM" / "100MSDCF" / "DSC00001.ARW").write_bytes(os.urandom(300_000))
    (card / "DCIM" / "100MSDCF" / "DSC00001.JPG").write_bytes(os.urandom(50_000))
    (card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4").write_bytes(os.urandom(1_200_000))
    (card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001M01.XML").write_text("<xml/>")
    # Junk that must be skipped.
    (card / ".DS_Store").write_bytes(b"junk")
    (card / "DCIM" / "._DSC00001.ARW").write_bytes(b"junk")
    (card / "System Volume Information").mkdir()
    (card / "System Volume Information" / "IndexerVolumeGuid").write_bytes(b"junk")
    return card


def test_is_junk():
    assert is_junk(".DS_Store")
    assert is_junk("._foo.mov")
    assert is_junk("$RECYCLE.BIN")
    assert not is_junk("DSC00001.ARW")


def test_scan_filters_junk_and_fingerprint_is_stable(tmp_path):
    card = make_card(tmp_path)
    scan = scan_source(card, "SD01")
    assert sorted(f.relative for f in scan.files) == [
        "DCIM/100MSDCF/DSC00001.ARW",
        "DCIM/100MSDCF/DSC00001.JPG",
        "PRIVATE/M4ROOT/CLIP/C0001.MP4",
        "PRIVATE/M4ROOT/CLIP/C0001M01.XML",
    ]
    assert scan.skipped_junk == 3
    assert scan.fingerprint == scan_source(card, "SD01").fingerprint
    assert scan.fingerprint != scan_source(card, "SD02").fingerprint


def test_offload_copies_verifies_and_writes_manifests(tmp_path):
    card = make_card(tmp_path)
    dest = tmp_path / "Acme" / "Prod" / "Video" / "10.05.2026" / "FX3 - Gerry (#12)"
    req = OffloadRequest(
        source=card, destination=dest, client_name="Acme", production_name="Prod", category="Video",
        shoot_date=date(2026, 10, 5), camera="FX3", operator="Gerry", card_number=12,
        source_label="SD01", chunk_size=64 * 1024,
    )
    assert req.title == "FX3 - Gerry (#12)"
    updates = []
    result = OffloadEngine(req, progress=lambda p: updates.append(p.fraction)).run()

    assert result.status == "verified", result.message
    assert result.copied == 4
    assert result.problems == []
    assert updates and updates[-1] == 1.0
    for rel in ("DCIM/100MSDCF/DSC00001.ARW", "PRIVATE/M4ROOT/CLIP/C0001.MP4"):
        src, dst = card / rel, dest / rel
        assert xxh64_file(src) == xxh64_file(dst)
        assert abs(src.stat().st_mtime - dst.stat().st_mtime) < 2
    assert not (dest / ".DS_Store").exists()
    assert not list(dest.rglob("*.part"))

    # Manifest, sidecar and report.
    assert result.mhl_path and result.mhl_path.is_file()
    entries = read_mhl(result.mhl_path)
    assert len(entries) == 4
    assert {e.relative_path for e in entries} == {f.relative for f in result.files}
    assert result.sidecar_path and result.sidecar_path.read_text().count("\n") == 4
    report = (dest / REPORT_NAME).read_text()
    assert report.startswith("LumDIT")
    assert "Client:        Acme" in report
    assert "Card number:   #12" in report
    assert find_manifests(dest) == [result.mhl_path]

    verify = verify_manifest(result.mhl_path)
    assert verify.passed and verify.ok == 4


def _set_zero_filetime(path: Path) -> bool:
    """Give *path* the bogus 1601-01-01 timestamp Sony writes on SONYCARD.IND. Returns False if unsupported."""
    if os.name == "nt":
        import ctypes

        k = ctypes.windll.kernel32
        h = k.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x80, None)
        if h == -1:
            return False

        class FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

        ok = k.SetFileTime(h, None, None, ctypes.byref(FT(1, 0)))
        k.CloseHandle(h)
        return bool(ok)
    try:
        os.utime(path, (0, -11644473600))
        return True
    except OSError:
        return False


def test_offload_survives_garbage_source_timestamps(tmp_path):
    """Regression: SONYCARD.IND with a zero FILETIME killed the whole job with [Errno 22]."""
    card = make_card(tmp_path)
    ind = card / "PRIVATE" / "SONY" / "SONYCARD.IND"
    ind.parent.mkdir(parents=True)
    ind.write_bytes(b"\x00" * 64)
    if not _set_zero_filetime(ind):
        pytest.skip("cannot create a pre-1970 timestamp on this filesystem")
    assert ind.stat().st_mtime < 0

    dest = tmp_path / "out" / "A7IV - Jen (#32)"
    req = OffloadRequest(source=card, destination=dest, camera="A7IV", operator="Jen", card_number=32, chunk_size=64 * 1024)
    result = OffloadEngine(req).run()

    assert result.status == "verified", result.message
    assert result.copied == 5
    assert result.mhl_path and result.mhl_path.is_file()
    assert (dest / REPORT_NAME).is_file()
    bad = next(e for e in read_mhl(result.mhl_path) if e.relative_path.endswith("SONYCARD.IND"))
    assert bad.last_modified == "1601-01-01T00:00:00Z"
    assert verify_manifest(result.mhl_path).passed


def test_iso_from_timestamp_edge_cases():
    from lumdit.core.mhl import iso_from_timestamp

    assert iso_from_timestamp(0) == "1970-01-01T00:00:00Z"
    assert iso_from_timestamp(-11644473600) == "1601-01-01T00:00:00Z"
    assert iso_from_timestamp(315532800) == "1980-01-01T00:00:00Z"
    assert iso_from_timestamp(1e18) == "1970-01-01T00:00:00Z"  # out of range -> clamped, never raises


def test_offload_resume_skips_identical_and_flags_conflicts(tmp_path):
    card = make_card(tmp_path)
    dest = tmp_path / "dest"
    req = OffloadRequest(source=card, destination=dest, chunk_size=64 * 1024)
    first = OffloadEngine(req).run()
    assert first.status == "verified"

    # Tamper with one destination file, then re-run.
    (dest / "DCIM" / "100MSDCF" / "DSC00001.JPG").write_bytes(os.urandom(50_000))
    second = OffloadEngine(req).run()
    assert second.status == "issues"
    assert second.skipped == 3
    conflicts = [f for f in second.problems if f.status == "conflict"]
    assert [c.relative for c in conflicts] == ["DCIM/100MSDCF/DSC00001.JPG"]
    # Original (tampered) destination file is left untouched - never overwrite.
    assert (dest / "DCIM" / "100MSDCF" / "DSC00001.JPG").stat().st_size == 50_000

    # Verification against the first manifest catches the tampering.
    v = verify_manifest(first.mhl_path)
    assert not v.passed
    assert v.issues[0].relative_path == "DCIM/100MSDCF/DSC00001.JPG"
    assert v.issues[0].kind == "hash"


def test_verify_detects_missing_files(tmp_path):
    card = make_card(tmp_path)
    dest = tmp_path / "dest"
    result = OffloadEngine(OffloadRequest(source=card, destination=dest)).run()
    (dest / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4").unlink()
    v = verify_manifest(result.mhl_path)
    assert [i.kind for i in v.issues] == ["missing"]


def test_refuses_destination_inside_source(tmp_path):
    card = make_card(tmp_path)
    result = OffloadEngine(OffloadRequest(source=card, destination=card / "backup")).run()
    assert result.status == "error"
    assert "inside" in result.message


def test_empty_source_is_an_error(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    result = OffloadEngine(OffloadRequest(source=src, destination=tmp_path / "d")).run()
    assert result.status == "error"


def test_cancel_leaves_no_partial_files(tmp_path):
    card = make_card(tmp_path)
    dest = tmp_path / "dest"
    cancel = threading.Event()

    def on_progress(p):
        if p.bytes_done > 100_000:
            cancel.set()

    result = OffloadEngine(
        OffloadRequest(source=card, destination=dest, chunk_size=16 * 1024),
        progress=on_progress,
        cancel_event=cancel,
    ).run()
    assert result.status == "cancelled"
    assert not list(dest.rglob("*.part"))
    assert not list(dest.rglob("*.mhl"))

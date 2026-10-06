from datetime import datetime, timezone

from lumdit.core.mhl import HashEntry, read_mhl, write_mhl, write_xxh64_sidecar


def test_mhl_round_trip(tmp_path):
    entries = [
        HashEntry("DCIM/100/A.ARW", 10, "0123456789abcdef", "2026-10-05T10:00:00Z", "2026-10-05T10:00:01Z"),
        HashEntry("CLIP/C0001.MP4", 20, "fedcba9876543210", "2026-10-05T10:00:00Z", "2026-10-05T10:00:02Z"),
    ]
    start = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    path = write_mhl(tmp_path / "card.mhl", entries, start=start, comment="test")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<?xml")
    assert '<hashlist version="1.1">' in text
    assert "<xxhash64>0123456789abcdef</xxhash64>" in text
    assert "<startdate>2026-10-05T10:00:00Z</startdate>" in text

    back = read_mhl(path)
    assert back == entries
    assert not (tmp_path / "card.mhl.tmp").exists()


def test_sidecar_format(tmp_path):
    entries = [HashEntry("a/b.mov", 1, "aa", "", ""), HashEntry("c.jpg", 2, "bb", "", "")]
    p = write_xxh64_sidecar(tmp_path / "card.xxh64", entries)
    assert p.read_text() == "aa  a/b.mov\nbb  c.jpg\n"

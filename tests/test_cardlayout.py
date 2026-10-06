from __future__ import annotations

import os
from pathlib import Path

from lumdit.core.cardlayout import detect_layout, looks_like_card_root
from lumdit.core.offload import scan_source


def _touch(p: Path, size: int = 10) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))


def sony_card(root: Path) -> Path:
    _touch(root / "DCIM" / "100MSDCF" / "DSC00001.ARW", 300)
    _touch(root / "DCIM" / "100MSDCF" / "DSC00001.JPG", 100)
    _touch(root / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4", 500)
    _touch(root / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001M01.XML", 20)
    _touch(root / "PRIVATE" / "M4ROOT" / "THMBNL" / "C0001T01.JPG", 30)
    _touch(root / "PRIVATE" / "M4ROOT" / "MEDIAPRO.XML", 40)
    _touch(root / "PRIVATE" / "SONY" / "firmware.bin", 10)
    _touch(root / "AVF_INFO" / "AVIN0001.INP", 10)
    (root / "MISC").mkdir()
    return root


def test_sony_card_photo_and_video_roots(tmp_path):
    card = sony_card(tmp_path / "SONY")
    assert looks_like_card_root(card)
    layout = detect_layout(card)
    assert layout.recognised
    by_rel = {r.rel: r for r in layout.roots}
    assert by_rel["DCIM"].photo_files == 2
    assert by_rel["PRIVATE/M4ROOT"].video_files == 1
    assert by_rel["PRIVATE/M4ROOT"].files == 4  # XML sidecars + thumbnail + MEDIAPRO come along

    photo = layout.roots_for("Photo")
    assert [r.rel for r in photo] == ["DCIM"]
    video = layout.roots_for("Video")
    assert [r.rel for r in video] == ["PRIVATE/M4ROOT"]
    assert [r.rel for r in layout.roots_for("Headshot Booth")] == ["DCIM"]
    assert [r.rel for r in layout.excluded_media(photo)] == ["PRIVATE/M4ROOT"]
    # Housekeeping folders never appear as "other" items.
    assert layout.other_top_level == []


def test_scan_source_respects_include_roots_and_keeps_structure(tmp_path):
    card = sony_card(tmp_path / "SONY")
    full = scan_source(card)
    photo = scan_source(card, include_roots=("DCIM",))
    video = scan_source(card, include_roots=("PRIVATE/M4ROOT",))
    assert {f.relative for f in photo.files} == {"DCIM/100MSDCF/DSC00001.ARW", "DCIM/100MSDCF/DSC00001.JPG"}
    assert all(f.relative.startswith("PRIVATE/M4ROOT/") for f in video.files)
    assert len(video.files) == 4
    assert len(full.files) == len(photo.files) + len(video.files) + 2  # + SONY firmware + AVF_INFO
    assert photo.fingerprint != video.fingerprint != full.fingerprint


def test_include_roots_case_insensitive(tmp_path):
    card = tmp_path / "CARD"
    _touch(card / "private" / "m4root" / "clip" / "C0001.MP4", 50)
    scan = scan_source(card, include_roots=("PRIVATE/M4ROOT",))
    assert [f.relative for f in scan.files] == ["private/m4root/clip/C0001.MP4"]


def test_canon_and_lumix_video_lives_in_dcim(tmp_path):
    card = tmp_path / "EOS"
    _touch(card / "DCIM" / "100CANON" / "IMG_0001.CR3", 200)
    _touch(card / "DCIM" / "100CANON" / "MVI_0002.MP4", 400)
    (card / "MISC").mkdir()
    layout = detect_layout(card)
    # No dedicated video root, so Video falls back to DCIM (which contains clips).
    assert [r.rel for r in layout.roots_for("Video")] == ["DCIM"]
    assert [r.rel for r in layout.roots_for("Photo")] == ["DCIM"]
    assert layout.excluded_media(layout.roots_for("Video")) == []


def test_canon_cinema_contents_and_crm(tmp_path):
    card = tmp_path / "C70"
    _touch(card / "CONTENTS" / "CLIPS001" / "A001C001_240101AB.MXF", 800)
    _touch(card / "CRM" / "REEL_001" / "A001C002.CRM", 900)
    _touch(card / "DCIM" / "100CANON" / "IMG_0001.JPG", 100)
    layout = detect_layout(card)
    assert sorted(r.rel for r in layout.roots_for("Video")) == ["CONTENTS", "CRM"]
    assert [r.rel for r in layout.roots_for("Photo")] == ["DCIM"]


def test_unknown_layout_copies_everything(tmp_path):
    # Blackmagic: clips straight at the card root.
    card = tmp_path / "BMPCC"
    _touch(card / "A001_01011200_C001.braw", 1000)
    _touch(card / "A001_01011200_C002.mov", 1000)
    assert not looks_like_card_root(card)
    layout = detect_layout(card)
    assert not layout.recognised
    assert layout.roots_for("Video") == []
    assert sorted(layout.other_top_level) == ["A001_01011200_C001.braw", "A001_01011200_C002.mov"]
    scan = scan_source(card, include_roots=())
    assert len(scan.files) == 2


def test_video_category_with_only_stills_falls_back_to_whole_card(tmp_path):
    card = tmp_path / "PHOTOSONLY"
    _touch(card / "DCIM" / "100MSDCF" / "DSC00001.ARW", 300)
    layout = detect_layout(card)
    # DCIM has no clips, so there is nothing sensible to restrict to.
    assert layout.roots_for("Video") == []
    assert [r.rel for r in layout.roots_for("Photo")] == ["DCIM"]


def test_subfolder_source_is_not_a_card_root(tmp_path):
    card = sony_card(tmp_path / "SONY")
    assert not looks_like_card_root(card / "DCIM")
    assert not looks_like_card_root(card / "PRIVATE" / "M4ROOT" / "CLIP")

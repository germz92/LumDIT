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


def zoom_h6_card(root: Path) -> Path:
    _touch(root / "FOLDER01" / "ZOOM0001" / "ZOOM0001_LR.WAV", 200)
    _touch(root / "FOLDER01" / "ZOOM0001" / "ZOOM0001_Tr1.WAV", 180)
    _touch(root / "FOLDER01" / "ZOOM0001" / "ZOOM0001.hprj", 20)
    return root


def mixpre_card(root: Path) -> Path:
    _touch(root / "05-15-20" / "MIXPRE-001.WAV", 400)
    _touch(root / "05-15-20" / "05-15-20_Report.csv", 30)
    _touch(root / "TRASH" / "MIXPRE-000.WAV", 100)
    _touch(root / "UNDO" / "MIXPRE-002.WAV", 100)
    return root


def tascam_card(root: Path) -> Path:
    _touch(root / "MUSIC" / "T0001.WAV", 250)
    _touch(root / "MUSIC" / "FOLDER" / "T0002.WAV", 250)
    _touch(root / "UTILITY" / "info.dat", 10)
    _touch(root / "dr-70.sys", 8)
    return root


def dji_mic_volume(root: Path) -> Path:
    _touch(root / "TX01_MIC001_20250527_202904_orig.wav", 300)
    _touch(root / "TX01_MIC002_20250527_210000_orig.wav", 300)
    return root


def test_zoom_h6_audio_copies_folder01_only(tmp_path):
    card = zoom_h6_card(tmp_path / "ZOOM")
    assert looks_like_card_root(card)
    layout = detect_layout(card)
    assert layout.recognised
    assert layout.fingerprint == "Zoom H-series"
    audio = layout.roots_for("Audio")
    assert [r.rel for r in audio] == ["FOLDER01"]
    assert audio[0].audio_files == 2
    assert "2 takes" in audio[0].summary()
    assert layout.exclude_for("Audio") == ()
    assert layout.roots_for("Photo") == []
    assert layout.roots_for("Video") == []
    scan = scan_source(card, include_roots=tuple(r.rel for r in audio))
    assert {f.relative for f in scan.files} == {
        "FOLDER01/ZOOM0001/ZOOM0001_LR.WAV",
        "FOLDER01/ZOOM0001/ZOOM0001_Tr1.WAV",
        "FOLDER01/ZOOM0001/ZOOM0001.hprj",
    }


def test_mixpre_skips_trash_and_undo(tmp_path):
    card = mixpre_card(tmp_path / "MIXPRE")
    assert looks_like_card_root(card)
    layout = detect_layout(card)
    assert layout.fingerprint == "MixPre"
    assert layout.roots_for("Audio") == []
    assert layout.exclude_for("Audio") == ("TRASH", "UNDO", "UTILITY")
    assert "MixPre" in layout.volume_summary()
    scan = scan_source(card, exclude_names=layout.exclude_for("Audio"))
    assert {f.relative for f in scan.files} == {
        "05-15-20/MIXPRE-001.WAV",
        "05-15-20/05-15-20_Report.csv",
    }
    full = scan_source(card)
    assert any(f.relative.startswith("TRASH/") for f in full.files)


def test_tascam_audio_copies_music_only(tmp_path):
    card = tascam_card(tmp_path / "TASCAM")
    layout = detect_layout(card)
    assert layout.fingerprint == "Tascam"
    audio = layout.roots_for("Audio")
    assert [r.rel for r in audio] == ["MUSIC"]
    assert audio[0].audio_files == 2
    assert layout.exclude_for("Audio") == ()
    scan = scan_source(card, include_roots=("MUSIC",))
    assert all(f.relative.startswith("MUSIC/") for f in scan.files)
    assert not any("UTILITY" in f.relative for f in scan.files)


def test_dji_mic_copies_whole_volume(tmp_path):
    card = dji_mic_volume(tmp_path / "DJIMIC")
    assert looks_like_card_root(card)
    layout = detect_layout(card)
    assert layout.fingerprint == "DJI Mic"
    assert layout.roots_for("Audio") == []
    assert layout.audio_files == 2
    assert "2 takes" in layout.volume_summary()
    scan = scan_source(card, exclude_names=layout.exclude_for("Audio"))
    assert {f.relative for f in scan.files} == {
        "TX01_MIC001_20250527_202904_orig.wav",
        "TX01_MIC002_20250527_210000_orig.wav",
    }


def test_sony_card_audio_does_not_take_audio_path(tmp_path):
    card = sony_card(tmp_path / "SONY")
    layout = detect_layout(card)
    assert layout.fingerprint is None
    assert layout.roots_for("Audio") == []
    assert layout.exclude_for("Audio") == ()
    assert [r.rel for r in layout.roots_for("Photo")] == ["DCIM"]
    assert [r.rel for r in layout.roots_for("Video")] == ["PRIVATE/M4ROOT"]


def test_other_category_copies_whole_card(tmp_path):
    card = mixpre_card(tmp_path / "MISC")
    layout = detect_layout(card)
    assert layout.roots_for("Other") == []
    assert layout.exclude_for("Other") == ()

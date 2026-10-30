"""Recognise camera-card and audio-recorder folder layouts.

Camera manufacturers put media in a handful of well-known places on a card. The
rest of the card (camera settings, DPOF print lists, firmware, OS metadata) is not
footage. When LumDIT offloads a whole card for a *known* category it copies only
the media roots relevant to that category, preserving the card-relative structure
underneath them so NLEs and manufacturer tools still find their sidecars.

Known media roots (relative to the card root)
---------------------------------------------
DCIM                 DCF stills - and video on Canon EOS R, Nikon, Fujifilm, Panasonic Lumix,
                     GoPro, DJI, phones.
PRIVATE/M4ROOT       Sony XAVC S / HS (CLIP/*.MP4 + XML, THMBNL, SUB, GENERAL, MEDIAPRO.XML).
                     Copy the whole M4ROOT: MEDIAPRO.XML and the XML sidecars are what Catalyst,
                     Resolve and Media Composer use, and spanned clips need them.
PRIVATE/XDROOT       Sony XAVC-I / XAVC-L MXF (FX6, FX9, FS7...).
PRIVATE/AVCHD        AVCHD (Sony, Panasonic, Canon camcorders) - needs the whole BDMV tree.
MP_ROOT              Sony legacy MP4.
CONTENTS             Canon Cinema EOS XF-AVC MXF (CLIPS001/...).
XFVC, CRM, XMLTAG    Canon EOS R5 / R5 C / R1 XF-AVC S, Cinema RAW Light, news metadata.
stills               Blackmagic DNG stills.
MUSIC                Tascam DR / Portacapture recordings.
FOLDER01–FOLDER10    Zoom H5 / H6 / H4n project folders.

Audio recorders without a single fixed root (Zoom H8 / F-series, Sound Devices
MixPre, DJI Mic USB volumes) are recognised by a cheap top-level fingerprint.
Audio offloads then copy the whole volume minus recorder housekeeping (TRASH,
UNDO, UTILITY) so project files (.hprj, SESSION.XML, MixPre CSV reports) stay
with the takes.

Cameras that write clips straight to the card root (Blackmagic BRAW/ProRes, RED *.RDC,
ARRI, Atomos) have no recognisable root, so the whole card is copied unchanged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PHOTO, VIDEO, BOTH, AUDIO = "photo", "video", "both", "audio"
CAMERA_KINDS = {PHOTO, VIDEO, BOTH}

# (relative path, kind, description)
KNOWN_ROOTS: tuple[tuple[str, str, str], ...] = (
    ("DCIM", BOTH, "DCF stills (and in-DCIM video)"),
    ("PRIVATE/M4ROOT", VIDEO, "Sony XAVC S/HS"),
    ("PRIVATE/XDROOT", VIDEO, "Sony XAVC-I/L (MXF)"),
    ("PRIVATE/AVCHD", VIDEO, "AVCHD"),
    ("MP_ROOT", VIDEO, "Sony MP4"),
    ("CONTENTS", VIDEO, "Canon XF-AVC (MXF)"),
    ("XFVC", VIDEO, "Canon XF-AVC S / XF-HEVC S"),
    ("CRM", VIDEO, "Canon Cinema RAW Light"),
    ("XMLTAG", VIDEO, "Canon news metadata"),
    ("stills", PHOTO, "Blackmagic stills"),
    ("MUSIC", AUDIO, "Tascam recordings"),
    *(
        (f"FOLDER{i:02d}", AUDIO, "Zoom H-series")
        for i in range(1, 11)
    ),
)

# Top-level items that are camera housekeeping, never footage. Used only for the
# "what else is on this card" summary; they are never selected.
HOUSEKEEPING = {
    "MISC",
    "CAMSET",
    "AD_LUMIX",
    "AVF_INFO",
    "EOSMISC",
    "MODELINF.DAT",
    "PRIVATE/SONY",
    "PRIVATE/M4ROOT/STATUS.BIN",
}

# Recorder housekeeping left on the card for Audio whole-volume copies.
AUDIO_HOUSEKEEPING = frozenset({"UTILITY", "TRASH", "UNDO"})

VIDEO_EXT = {
    ".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".braw", ".r3d", ".crm", ".nev", ".ari", ".arx", ".mkv",
}
PHOTO_EXT = {
    ".jpg", ".jpeg", ".heif", ".heic", ".hif", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".rw2", ".orf",
    ".dng", ".pef", ".srw", ".tif", ".tiff", ".png",
}
AUDIO_EXT = {
    ".wav", ".bwf", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac",
}

# Which media-root kinds a production category wants.
_CATEGORY_KINDS = {
    "photo": {PHOTO, BOTH},
    "headshot booth": {PHOTO, BOTH},
    "headshot": {PHOTO, BOTH},
    "video": {VIDEO, BOTH},
    "audio": {AUDIO},
}

_ZOOM_FOLDER_RE = re.compile(r"^FOLDER\d{2}$", re.IGNORECASE)
_ZOOM_PROJECT_RE = re.compile(r"^ZOOM\d{4}$", re.IGNORECASE)
_ZOOM_H8_RE = re.compile(r"^[FMP]\d{6}_\d{3}$", re.IGNORECASE)
_ZOOM_F_TAKE_RE = re.compile(r".+-T\d{3}.*\.(?:wav|WAV)$")
_ZOOM_F_SCENE_RE = re.compile(r"^(?:Scene\d+|\d{6})$", re.IGNORECASE)
_MIXPRE_WAV_RE = re.compile(r"^MIXPRE-\d{3}\.WAV$", re.IGNORECASE)
_MIXPRE_REPORT_RE = re.compile(r".+_Report\.csv$", re.IGNORECASE)
_DJI_FOLDER_RE = re.compile(r"^TX_MIC\d+", re.IGNORECASE)
_DJI_FILE_RE = re.compile(r"^TX\d{2}_MIC\d+_.+_(?:orig|edit)\.wav$", re.IGNORECASE)
_TASCAM_SYS_RE = re.compile(r"^dr-.*\.sys$", re.IGNORECASE)


@dataclass
class MediaRoot:
    rel: str
    kind: str
    description: str
    path: Path
    files: int = 0
    bytes: int = 0
    video_files: int = 0
    photo_files: int = 0
    audio_files: int = 0

    @property
    def has_media(self) -> bool:
        return self.files > 0

    def summary(self) -> str:
        parts = []
        if self.audio_files:
            parts.append(f"{self.audio_files} take{'s' if self.audio_files != 1 else ''}")
        if self.video_files:
            parts.append(f"{self.video_files} clip{'s' if self.video_files != 1 else ''}")
        # Thumbnails inside a video root (Sony THMBNL) are not photos worth reporting.
        if self.photo_files and self.kind != VIDEO:
            parts.append(f"{self.photo_files} photo{'s' if self.photo_files != 1 else ''}")
        if not parts and self.files:
            parts.append(f"{self.files} file{'s' if self.files != 1 else ''}")
        return f"{self.rel}: {', '.join(parts) or 'empty'} ({_human(self.bytes)})"


@dataclass
class CardLayout:
    root: Path
    roots: list[MediaRoot] = field(default_factory=list)
    other_top_level: list[str] = field(default_factory=list)  # unrecognised, non-housekeeping items
    fingerprint: str | None = None  # "Zoom H-series", "MixPre", "DJI Mic", ...
    audio_files: int = 0
    audio_bytes: int = 0

    @property
    def recognised(self) -> bool:
        return any(r.has_media for r in self.roots) or bool(self.fingerprint)

    @property
    def systems(self) -> list[str]:
        seen: list[str] = []
        for r in self.roots:
            if r.has_media and r.description not in seen:
                seen.append(r.description)
        if self.fingerprint and self.fingerprint not in seen:
            seen.append(self.fingerprint)
        return seen

    def volume_summary(self) -> str:
        """Whole-volume description for audio recorders without a fixed media root."""
        name = self.fingerprint or "Audio recorder"
        if self.audio_files:
            takes = f"{self.audio_files} take{'s' if self.audio_files != 1 else ''}"
        else:
            takes = "audio"
        return f"{name}: {takes} ({_human(self.audio_bytes)}) — whole volume"

    def roots_for(self, category: str) -> list[MediaRoot]:
        """Media roots to copy for *category*; empty list means 'copy the whole card'."""
        kinds = _CATEGORY_KINDS.get((category or "").strip().lower())
        populated = [r for r in self.roots if r.has_media]
        if kinds is None or not populated:
            return []
        if kinds == {VIDEO, BOTH}:
            # Prefer dedicated video roots (Sony M4ROOT etc.); fall back to DCIM when the
            # camera records video there (Canon EOS R, Nikon, Fuji, Lumix, GoPro...).
            dedicated = [r for r in populated if r.kind == VIDEO]
            if dedicated:
                return dedicated
            return [r for r in populated if r.kind == BOTH and r.video_files]
        if kinds == {AUDIO}:
            # Tascam MUSIC / Zoom H FOLDER01-10. MixPre, Zoom F/H8 and DJI Mic have no
            # fixed root — empty list means whole volume (minus AUDIO_HOUSEKEEPING).
            return [r for r in populated if r.kind == AUDIO]
        chosen = [r for r in populated if r.kind in kinds]
        return chosen

    def exclude_for(self, category: str) -> tuple[str, ...]:
        """Top-level names to skip. Only used for whole-volume Audio copies."""
        if (category or "").strip().lower() != "audio":
            return ()
        if self.roots_for(category):
            return ()
        if self.fingerprint or any(r.kind == AUDIO and r.has_media for r in self.roots):
            return tuple(sorted(AUDIO_HOUSEKEEPING))
        return ()

    def excluded_media(self, chosen: list[MediaRoot]) -> list[MediaRoot]:
        """Populated media roots that *chosen* leaves behind."""
        picked = {r.rel for r in chosen}
        return [r for r in self.roots if r.has_media and r.rel not in picked]


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _count(path: Path, skip_dirs: frozenset[str] | set[str] | None = None) -> tuple[int, int, int, int, int]:
    files = size = video = photo = audio = 0
    skip = {s.lower() for s in (skip_dirs or ())}
    for dirpath, dirnames, filenames in os.walk(path):
        if skip and Path(dirpath) == path:
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.startswith("._"):
                continue
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            files += 1
            size += st.st_size
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXT:
                video += 1
            elif ext in PHOTO_EXT:
                photo += 1
            elif ext in AUDIO_EXT:
                audio += 1
    return files, size, video, photo, audio


def _find_case_insensitive(parent: Path, rel: str) -> Path | None:  # noqa: shared with offload.scan_source
    """Resolve *rel* under *parent* ignoring case (FAT/exFAT cards may present either)."""
    cur = parent
    for part in rel.split("/"):
        try:
            entries = {e.name.lower(): e for e in os.scandir(cur) if e.is_dir()}
        except OSError:
            return None
        hit = entries.get(part.lower())
        if hit is None:
            return None
        cur = Path(hit.path)
    return cur


def _has_camera_root(card_root: Path) -> bool:
    return any(
        _find_case_insensitive(card_root, rel) is not None
        for rel, kind, _ in KNOWN_ROOTS
        if kind in CAMERA_KINDS
    )


def _peek_names(dirs: list[os.DirEntry]) -> list[str]:
    names: list[str] = []
    for entry in dirs:
        try:
            names.extend(e.name for e in os.scandir(entry.path))
        except OSError:
            continue
    return names


def _audio_fingerprint(card_root: Path) -> str | None:
    """Cheap top-level (plus one peek) signature for audio recorders. None if this looks like a camera."""
    if _has_camera_root(card_root):
        return None
    try:
        entries = list(os.scandir(card_root))
    except OSError:
        return None
    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    names_lower = {e.name.lower() for e in entries}
    file_names = [e.name for e in files]

    if any(_DJI_FOLDER_RE.match(e.name) for e in dirs) or any(_DJI_FILE_RE.match(n) for n in file_names):
        return "DJI Mic"
    if "music" in names_lower:
        return "Tascam"
    if any(_ZOOM_FOLDER_RE.match(e.name) for e in dirs):
        return "Zoom H-series"
    if "soundpad" in names_lower or any(_ZOOM_H8_RE.match(e.name) for e in dirs):
        return "Zoom H8"
    peeked = _peek_names(dirs)
    if any(n.lower().endswith(".h8prj") for n in peeked):
        return "Zoom H8"
    if "trash" in names_lower and "undo" in names_lower:
        return "MixPre"
    if any(_MIXPRE_WAV_RE.match(n) for n in file_names) or any(_MIXPRE_REPORT_RE.match(n) for n in file_names):
        return "MixPre"
    if any(_MIXPRE_WAV_RE.match(n) for n in peeked) or any(_MIXPRE_REPORT_RE.match(n) for n in peeked):
        return "MixPre"
    if any(_ZOOM_F_TAKE_RE.match(n) for n in file_names) or any(_ZOOM_F_TAKE_RE.match(n) for n in peeked):
        return "Zoom F-series"
    if any(_ZOOM_F_SCENE_RE.match(e.name) for e in dirs) and any(
        Path(n).suffix.lower() in AUDIO_EXT for n in peeked
    ):
        return "Zoom F-series"
    if any(_ZOOM_PROJECT_RE.match(e.name) for e in dirs) and any(
        n.lower().endswith(".hprj") or Path(n).suffix.lower() in AUDIO_EXT for n in peeked
    ):
        return "Zoom H-series"
    audio_at_root = sum(1 for n in file_names if Path(n).suffix.lower() in AUDIO_EXT)
    if audio_at_root >= 2:
        return "Audio recorder"
    return None


def detect_layout(card_root: Path) -> CardLayout:
    """Inspect the top of *card_root* and classify what is there. Cheap: only known roots are walked."""
    card_root = Path(card_root)
    layout = CardLayout(root=card_root)
    for rel, kind, desc in KNOWN_ROOTS:
        p = _find_case_insensitive(card_root, rel)
        if p is None:
            continue
        files, size, video, photo, audio = _count(p)
        layout.roots.append(
            MediaRoot(
                rel=rel,
                kind=kind,
                description=desc,
                path=p,
                files=files,
                bytes=size,
                video_files=video,
                photo_files=photo,
                audio_files=audio,
            )
        )
    known_top = {r.split("/")[0].lower() for r, _, _ in KNOWN_ROOTS}
    housekeeping_top = {h.split("/")[0].lower() for h in HOUSEKEEPING} | {n.lower() for n in AUDIO_HOUSEKEEPING}
    try:
        for e in os.scandir(card_root):
            n = e.name
            if n.startswith(".") or n.startswith("$") or n in ("System Volume Information", "Thumbs.db", "desktop.ini"):
                continue
            if n.lower() in known_top or n.lower() in housekeeping_top:
                continue
            layout.other_top_level.append(n + ("/" if e.is_dir() else ""))
    except OSError:
        pass
    layout.other_top_level.sort()
    if not any(r.has_media and r.kind in CAMERA_KINDS for r in layout.roots):
        layout.fingerprint = _audio_fingerprint(card_root)
        if layout.fingerprint:
            _files, size, _video, _photo, audio = _count(card_root, skip_dirs=AUDIO_HOUSEKEEPING)
            layout.audio_files = audio
            layout.audio_bytes = size
    return layout


def looks_like_card_root(path: Path) -> bool:
    """True if *path* is the top of a camera card or a recognised audio recorder volume."""
    path = Path(path)
    if any(_find_case_insensitive(path, rel) is not None for rel, _, _ in KNOWN_ROOTS):
        return True
    return _audio_fingerprint(path) is not None

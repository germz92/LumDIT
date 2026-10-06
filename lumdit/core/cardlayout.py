"""Recognise camera-card folder layouts and decide which top-level folders to offload.

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

Cameras that write clips straight to the card root (Blackmagic BRAW/ProRes, RED *.RDC,
ARRI, Atomos) have no recognisable root, so the whole card is copied unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PHOTO, VIDEO, BOTH = "photo", "video", "both"

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

VIDEO_EXT = {
    ".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".braw", ".r3d", ".crm", ".nev", ".ari", ".arx", ".mkv",
}
PHOTO_EXT = {
    ".jpg", ".jpeg", ".heif", ".heic", ".hif", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".rw2", ".orf",
    ".dng", ".pef", ".srw", ".tif", ".tiff", ".png",
}

# Which media-root kinds a production category wants.
_CATEGORY_KINDS = {
    "photo": {PHOTO, BOTH},
    "headshot booth": {PHOTO, BOTH},
    "headshot": {PHOTO, BOTH},
    "video": {VIDEO, BOTH},
}


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

    @property
    def has_media(self) -> bool:
        return self.files > 0

    def summary(self) -> str:
        parts = []
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

    @property
    def recognised(self) -> bool:
        return any(r.has_media for r in self.roots)

    @property
    def systems(self) -> list[str]:
        return [r.description for r in self.roots if r.has_media]

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
        chosen = [r for r in populated if r.kind in kinds]
        return chosen

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


def _count(path: Path) -> tuple[int, int, int, int]:
    files = size = video = photo = 0
    for dirpath, dirnames, filenames in os.walk(path):
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
    return files, size, video, photo


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


def detect_layout(card_root: Path) -> CardLayout:
    """Inspect the top of *card_root* and classify what is there. Cheap: only known roots are walked."""
    card_root = Path(card_root)
    layout = CardLayout(root=card_root)
    for rel, kind, desc in KNOWN_ROOTS:
        p = _find_case_insensitive(card_root, rel)
        if p is None:
            continue
        files, size, video, photo = _count(p)
        layout.roots.append(
            MediaRoot(rel=rel, kind=kind, description=desc, path=p, files=files, bytes=size, video_files=video, photo_files=photo)
        )
    known_top = {r.split("/")[0].lower() for r, _, _ in KNOWN_ROOTS}
    housekeeping_top = {h.split("/")[0].lower() for h in HOUSEKEEPING}
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
    return layout


def looks_like_card_root(path: Path) -> bool:
    """True if *path* is the top of a camera card (has at least one known media root)."""
    return any(_find_case_insensitive(Path(path), rel) is not None for rel, _, _ in KNOWN_ROOTS)

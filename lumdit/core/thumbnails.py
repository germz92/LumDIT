"""Thumbnail generation for photos, RAW files, HEIF and video, with a disk cache.

All functions here are thread-safe and Qt-free except for the small
``ThumbnailLoader`` wrapper at the bottom which turns results into QImages.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import xxhash
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".jfif"}
HEIF_EXTS = {".heic", ".heif", ".hif"}
RAW_EXTS = {
    ".arw", ".srf", ".sr2",  # Sony
    ".cr2", ".cr3", ".crw",  # Canon
    ".nef", ".nrw",  # Nikon
    ".raf",  # Fujifilm
    ".orf",  # Olympus
    ".rw2",  # Panasonic
    ".dng",  # Adobe / Leica / drones
    ".pef",  # Pentax
    ".3fr", ".fff",  # Hasselblad
    ".iiq",  # Phase One
    ".x3f",  # Sigma
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts", ".mpg", ".mpeg",
    ".braw", ".r3d", ".crm", ".webm", ".wmv", ".3gp", ".insv", ".360",
}
AUDIO_EXTS = {".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".bwf"}
SIDECAR_EXTS = {".xml", ".xmp", ".thm", ".lrf", ".srt", ".bim", ".cpi", ".mpl", ".bdm", ".txt", ".json"}

MEDIA_EXTS = IMAGE_EXTS | HEIF_EXTS | RAW_EXTS | VIDEO_EXTS | AUDIO_EXTS


def media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS or ext in HEIF_EXTS:
        return "image"
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "other"


class ThumbnailCache:
    def __init__(self, cache_dir: Path) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def key(self, path: Path, size: int) -> str:
        try:
            st = path.stat()
            sig = f"{path}|{st.st_size}|{int(st.st_mtime)}|{size}"
        except OSError:
            sig = f"{path}|{size}"
        return xxhash.xxh64(sig.encode("utf-8", "surrogateescape")).hexdigest()

    def path_for(self, path: Path, size: int) -> Path:
        return self.dir / f"{self.key(path, size)}.jpg"

    def get(self, path: Path, size: int) -> Path | None:
        p = self.path_for(path, size)
        return p if p.is_file() else None

    def put(self, path: Path, size: int, image: Image.Image) -> Path:
        p = self.path_for(path, size)
        tmp = p.with_suffix(".tmp")
        image.convert("RGB").save(tmp, "JPEG", quality=85)
        os.replace(tmp, p)
        return p

    def clear(self) -> int:
        n = 0
        for f in self.dir.glob("*.jpg"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        return n


def _fit(img: Image.Image, size: int) -> Image.Image:
    img = ImageOps.exif_transpose(img) or img
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return img


def _image_thumb(path: Path, size: int) -> Image.Image:
    with Image.open(path) as im:
        im.draft("RGB", (size * 2, size * 2))  # fast JPEG downscale on load
        return _fit(im.copy(), size)


def _raw_thumb(path: Path, size: int) -> Image.Image:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as im:
                    return _fit(im.copy(), size)
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return _fit(Image.fromarray(thumb.data), size)
        except rawpy.LibRawNoThumbnailError:
            pass
        rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
        return _fit(Image.fromarray(rgb), size)


def _video_thumb(path: Path, size: int) -> Image.Image:
    import av

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            raise ValueError("no video stream")
        stream.thread_type = "AUTO"
        # Seek ~1 s in so we skip black leaders / slates where possible.
        try:
            if stream.duration and stream.time_base and float(stream.duration * stream.time_base) > 3:
                container.seek(int(1 / stream.time_base), stream=stream, backward=True)
        except Exception:
            pass
        for frame in container.decode(stream):
            img = frame.to_image()
            return _fit(img, size)
    raise ValueError("no frames decoded")


def make_thumbnail(path: Path, size: int = 192) -> Image.Image | None:
    kind = media_kind(path)
    try:
        if kind == "image":
            return _image_thumb(path, size)
        if kind == "raw":
            return _raw_thumb(path, size)
        if kind == "video":
            return _video_thumb(path, size)
    except Exception:
        return None
    return None


def thumbnail_file(path: Path, size: int, cache: ThumbnailCache) -> Path | None:
    cached = cache.get(path, size)
    if cached:
        return cached
    img = make_thumbnail(path, size)
    if img is None:
        return None
    return cache.put(path, size, img)

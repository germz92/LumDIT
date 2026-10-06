"""Verified offload engine.

Per file:
  1. stream copy source -> ``<dest>.part`` while hashing the bytes read from source
  2. fsync, then re-read the written file from the destination and hash it
  3. compare; on mismatch delete the .part and retry once, then report an error
  4. atomically rename ``.part`` -> final name and restore the modification time

Per job: write an MHL manifest, a ``.xxh64`` sidecar and a human-readable
offload report into the destination (card) folder.

Safety rules: never overwrite an existing destination file (identical files
are skipped and counted as already verified, different ones are flagged as
conflicts), refuse when the destination is inside the source, refuse when
free space is insufficient, never write to the source.
"""

from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from lumdit import APP_NAME, __version__
from lumdit.core import devices
from lumdit.core.hashing import (
    DEFAULT_CHUNK_SIZE,
    Cancelled,
    copy_and_hash,
    xxh64_bytes,
    xxh64_file,
)
from lumdit.core.mhl import HashEntry, iso_from_timestamp, write_mhl, write_xxh64_sidecar
from lumdit.core.cardlayout import _find_case_insensitive
from lumdit.core.naming import format_card_number

JUNK_NAMES = {
    ".DS_Store",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    ".DocumentRevisions-V100",
    ".VolumeIcon.icns",
    "Thumbs.db",
    "desktop.ini",
    "System Volume Information",
    "$RECYCLE.BIN",
    "$Recycle.Bin",
    "RECYCLER",
    "lost+found",
}
JUNK_PREFIXES = ("._",)
REPORT_NAME = "OFFLOAD_REPORT.txt"


class OffloadError(Exception):
    pass


@dataclass
class OffloadRequest:
    source: Path
    destination: Path  # the card folder; created if missing
    client_name: str = ""
    production_name: str = ""
    category: str = ""
    shoot_date: date | None = None
    camera: str = ""
    operator: str = ""
    card_number: int | str = 0  # number printed on the card, or a label such as "Internal"
    source_label: str = ""
    chunk_size: int = DEFAULT_CHUNK_SIZE
    # Link to the crew app's card log entry (empty for manual offloads).
    log_entry_id: str = ""
    log_slot: int = 0
    # Card-relative top-level folders to copy (e.g. ("DCIM",) or ("PRIVATE/M4ROOT",)).
    # Empty = copy the whole source. Structure under each root is preserved.
    include_roots: tuple[str, ...] = ()

    @property
    def card_label(self) -> str:
        return format_card_number(self.card_number)

    @property
    def title(self) -> str:
        if self.camera or self.operator:
            return f"{self.camera} - {self.operator} ({self.card_label})".strip(" -")
        return self.source.name


@dataclass
class FileEntry:
    source: Path
    relative: str  # forward slashes
    size: int
    mtime: float


@dataclass
class ScanResult:
    files: list[FileEntry]
    total_bytes: int
    skipped_junk: int
    fingerprint: str  # hash of the sorted (relative path, size) list + volume label


@dataclass
class FileOutcome:
    relative: str
    status: str  # "copied" | "skipped-identical" | "conflict" | "error"
    xxh64: str = ""
    detail: str = ""


@dataclass
class OffloadProgress:
    phase: str = "scanning"  # scanning | copying | verifying | finalising | done | error | cancelled
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    current_file: str = ""
    speed_bps: float = 0.0
    eta_seconds: float | None = None

    @property
    def fraction(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.bytes_total)


@dataclass
class OffloadResult:
    request: OffloadRequest
    status: str  # "verified" | "issues" | "cancelled" | "error"
    started: datetime
    finished: datetime
    files: list[FileOutcome] = field(default_factory=list)
    total_bytes: int = 0
    copied_bytes: int = 0
    fingerprint: str = ""
    mhl_path: Path | None = None
    sidecar_path: Path | None = None
    report_path: Path | None = None
    message: str = ""

    @property
    def copied(self) -> int:
        return sum(1 for f in self.files if f.status == "copied")

    @property
    def skipped(self) -> int:
        return sum(1 for f in self.files if f.status == "skipped-identical")

    @property
    def problems(self) -> list[FileOutcome]:
        return [f for f in self.files if f.status in ("conflict", "error")]

    @property
    def duration(self) -> float:
        return max(0.0, (self.finished - self.started).total_seconds())


ProgressCallback = Callable[[OffloadProgress], None]


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIXES)


def scan_source(source: Path, volume_label: str = "", include_roots: tuple[str, ...] | list[str] = ()) -> ScanResult:
    """Walk *source* collecting media files (junk filtered) and a cheap fingerprint.

    When *include_roots* is given only those card-relative folders are walked; paths in
    the result stay relative to *source* so the card structure is preserved.
    """
    files: list[FileEntry] = []
    skipped = 0
    total = 0
    source = Path(source)
    if include_roots:
        # Resolve against the on-disk casing so relative paths match the card exactly.
        starts = [p for p in (_find_case_insensitive(source, r) for r in include_roots) if p is not None]
    else:
        starts = [source]
    for start in starts:
        if not start.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            # Prune junk directories in place so we never descend into them.
            keep = [d for d in dirnames if not is_junk(d)]
            skipped += len(dirnames) - len(keep)
            dirnames[:] = sorted(keep)
            for name in sorted(filenames):
                if is_junk(name) or name.endswith(".part"):
                    skipped += 1
                    continue
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    skipped += 1
                    continue
                rel = full.relative_to(source).as_posix()
                files.append(FileEntry(source=full, relative=rel, size=st.st_size, mtime=st.st_mtime))
                total += st.st_size
    files.sort(key=lambda f: f.relative)
    fp_src = "\n".join(f"{f.relative}|{f.size}" for f in files)
    fingerprint = xxh64_bytes(f"{volume_label}\n{fp_src}".encode("utf-8"))
    return ScanResult(files=files, total_bytes=total, skipped_junk=skipped, fingerprint=fingerprint)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


class _Speedometer:
    """Rolling-window throughput estimate."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self.samples: list[tuple[float, int]] = []

    def add(self, nbytes: int) -> None:
        now = time.monotonic()
        self.samples.append((now, nbytes))
        cutoff = now - self.window
        while len(self.samples) > 1 and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1][0] - self.samples[0][0]
        if span <= 0:
            return 0.0
        return sum(n for _, n in self.samples[1:]) / span


class OffloadEngine:
    def __init__(
        self,
        request: OffloadRequest,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.request = request
        self._progress_cb = progress
        self._cancel = cancel_event or threading.Event()
        self.progress = OffloadProgress()
        self._speed = _Speedometer()
        self._last_emit = 0.0

    # ---- helpers ------------------------------------------------------------
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        self.progress.speed_bps = self._speed.bps()
        remaining = self.progress.bytes_total - self.progress.bytes_done
        self.progress.eta_seconds = (
            remaining / self.progress.speed_bps if self.progress.speed_bps > 0 else None
        )
        if self._progress_cb is not None:
            self._progress_cb(self.progress)

    def _advance(self, n: int) -> None:
        self.progress.bytes_done += n
        self._speed.add(n)
        self._emit()

    # ---- preflight ----------------------------------------------------------
    def preflight(self, scan: ScanResult) -> None:
        src, dst = self.request.source, self.request.destination
        if not src.is_dir():
            raise OffloadError(f"Source folder does not exist: {src}")
        if _is_within(dst, src):
            raise OffloadError("Destination is inside the source folder")
        if _is_within(src, dst):
            raise OffloadError("Source is inside the destination folder")
        if not scan.files:
            raise OffloadError("Source folder contains no files to copy")
        free = devices.free_space(dst)
        # Keep a 2 % / 256 MiB cushion for manifests and filesystem overhead.
        needed = scan.total_bytes + max(256 * 1024 * 1024, int(scan.total_bytes * 0.02))
        if free < needed:
            raise OffloadError(
                f"Not enough free space: need {needed / 1e9:.1f} GB, have {free / 1e9:.1f} GB"
            )

    # ---- main ---------------------------------------------------------------
    def run(self) -> OffloadResult:
        req = self.request
        started = datetime.now(timezone.utc)
        self.progress.phase = "scanning"
        self._emit(force=True)
        outcomes: list[FileOutcome] = []
        entries: list[HashEntry] = []
        copied_bytes = 0
        fingerprint = ""
        status = "error"
        message = ""
        mhl_path = sidecar_path = report_path = None

        try:
            scan = scan_source(req.source, req.source_label, req.include_roots)
            fingerprint = scan.fingerprint
            self.preflight(scan)
            self.progress.bytes_total = scan.total_bytes * 2  # copy pass + read-back pass
            self.progress.files_total = len(scan.files)
            req.destination.mkdir(parents=True, exist_ok=True)

            for entry in scan.files:
                if self.cancelled():
                    raise Cancelled()
                self.progress.current_file = entry.relative
                outcome = self._process_file(entry)
                outcomes.append(outcome)
                if outcome.status == "copied":
                    copied_bytes += entry.size
                if outcome.xxh64:
                    entries.append(
                        HashEntry(
                            relative_path=entry.relative,
                            size=entry.size,
                            xxh64=outcome.xxh64,
                            last_modified=iso_from_timestamp(entry.mtime),
                            hash_date=iso_from_timestamp(time.time()),
                        )
                    )
                self.progress.files_done += 1
                self._emit(force=True)

            self.progress.phase = "finalising"
            self._emit(force=True)
            finished = datetime.now(timezone.utc)
            problems = [o for o in outcomes if o.status in ("conflict", "error")]
            status = "issues" if problems else "verified"
            stamp = finished.astimezone().strftime("%Y%m%d_%H%M%S")
            base = req.destination.name or "offload"
            if entries:
                mhl_path = write_mhl(
                    _unique_path(req.destination / f"{base}_{stamp}.mhl"),
                    entries,
                    start=started,
                    finish=finished,
                    comment=f"{req.client_name} / {req.production_name} / {req.category} / {req.camera} / {req.operator} / card {req.card_label}",
                )
                sidecar_path = write_xxh64_sidecar(req.destination / f"{base}.xxh64", entries)
            result = OffloadResult(
                request=req,
                status=status,
                started=started,
                finished=finished,
                files=outcomes,
                total_bytes=scan.total_bytes,
                copied_bytes=copied_bytes,
                fingerprint=fingerprint,
                mhl_path=mhl_path,
                sidecar_path=sidecar_path,
                message=f"{len(problems)} file(s) need attention" if problems else "All files verified",
            )
            result.report_path = write_report(result, scan)
            self.progress.phase = "done"
            self.progress.bytes_done = self.progress.bytes_total
            self.progress.current_file = ""
            self._emit(force=True)
            return result

        except Cancelled:
            status, message = "cancelled", "Cancelled by user"
            self.progress.phase = "cancelled"
        except (OffloadError, OSError) as exc:
            status, message = "error", str(exc)
            self.progress.phase = "error"

        self._emit(force=True)
        return OffloadResult(
            request=req,
            status=status,
            started=started,
            finished=datetime.now(timezone.utc),
            files=outcomes,
            copied_bytes=copied_bytes,
            fingerprint=fingerprint,
            message=message,
        )

    # ---- per-file -----------------------------------------------------------
    def _process_file(self, entry: FileEntry) -> FileOutcome:
        dest = self.request.destination / Path(*entry.relative.split("/"))
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            # Never overwrite. Decide whether it is the same file (resume) or a clash.
            self.progress.phase = "verifying"
            try:
                if dest.stat().st_size == entry.size:
                    src_hash = xxh64_file(entry.source, self.request.chunk_size, self._advance, self.cancelled)
                    dst_hash = xxh64_file(dest, self.request.chunk_size, self._advance, self.cancelled)
                    if src_hash == dst_hash:
                        return FileOutcome(entry.relative, "skipped-identical", src_hash)
                else:
                    self._advance(entry.size * 2)
            except Cancelled:
                raise
            except OSError as exc:
                return FileOutcome(entry.relative, "error", detail=str(exc))
            return FileOutcome(
                entry.relative,
                "conflict",
                detail="A different file with this name already exists at the destination",
            )

        last_error = ""
        for attempt in range(2):
            try:
                self.progress.phase = "copying"
                base_done = self.progress.bytes_done
                src_hash = copy_and_hash(entry.source, part, self.request.chunk_size, self._advance, self.cancelled)
                self.progress.phase = "verifying"
                dst_hash = xxh64_file(part, self.request.chunk_size, self._advance, self.cancelled)
                if src_hash != dst_hash:
                    last_error = f"Checksum mismatch after write (attempt {attempt + 1})"
                    _unlink(part)
                    # Roll progress back so the retry doesn't over-count.
                    self.progress.bytes_done = base_done
                    continue
                os.utime(part, (time.time(), entry.mtime))
                os.replace(part, dest)
                try:
                    shutil.copystat(entry.source, dest, follow_symlinks=False)
                except OSError:
                    pass
                return FileOutcome(entry.relative, "copied", src_hash)
            except Cancelled:
                _unlink(part)
                raise
            except OSError as exc:
                last_error = str(exc)
                _unlink(part)
                break
        return FileOutcome(entry.relative, "error", detail=last_error or "Unknown error")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_report(result: OffloadResult, scan: ScanResult) -> Path:
    req = result.request
    path = req.destination / REPORT_NAME
    lines = [
        f"{APP_NAME} {__version__} - Offload Report",
        "=" * 60,
        f"Status:        {result.status.upper()} - {result.message}",
        f"Client:        {req.client_name}",
        f"Production:    {req.production_name}",
        f"Category:      {req.category}",
        f"Shoot date:    {req.shoot_date.isoformat() if req.shoot_date else ''}",
        f"Camera:        {req.camera}",
        f"Operator:      {req.operator}",
        f"Card number:   {req.card_label}",
        f"Source:        {req.source}",
        f"Source volume: {req.source_label}",
        f"Copied roots:  {', '.join(req.include_roots) if req.include_roots else 'entire source'}",
        f"Destination:   {req.destination}",
        f"Started:       {result.started.astimezone().isoformat(timespec='seconds')}",
        f"Finished:      {result.finished.astimezone().isoformat(timespec='seconds')}",
        f"Duration:      {result.duration:.1f} s",
        f"Files:         {len(result.files)} ({result.copied} copied, {result.skipped} already present, {len(result.problems)} problems)",
        f"Bytes:         {result.total_bytes:,} ({result.total_bytes / 1e9:.2f} GB)",
        f"Junk skipped:  {scan.skipped_junk}",
        f"Fingerprint:   {result.fingerprint}",
        f"Checksum:      xxHash64, read-back verified",
        f"Manifest:      {result.mhl_path.name if result.mhl_path else '-'}",
        f"Sidecar:       {result.sidecar_path.name if result.sidecar_path else '-'}",
        f"Host:          {platform.node()} ({platform.system()} {platform.release()})",
        "",
        "Files",
        "-" * 60,
    ]
    for f in result.files:
        flag = {"copied": "OK ", "skipped-identical": "DUP", "conflict": "!! ", "error": "ERR"}[f.status]
        extra = f"  [{f.detail}]" if f.detail else ""
        lines.append(f"{flag} {f.xxh64 or '-':16} {f.relative}{extra}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

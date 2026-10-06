"""Media Hash List (MHL 1.x) writer / reader / verifier plus a flat .xxh64 sidecar.

The MHL 1.1 XML format is what Silverstack, ShotPut Pro, Hedge, YoYotta and
most post houses understand. Paths inside the manifest are relative to the
folder the .mhl lives in, using forward slashes.
"""

from __future__ import annotations

import getpass
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree as ET

from lumdit import APP_NAME, __version__
from lumdit.core.hashing import Cancelled, xxh64_file

HASH_TAG = "xxhash64"


@dataclass
class HashEntry:
    relative_path: str  # forward-slash path relative to the manifest folder
    size: int
    xxh64: str
    last_modified: str  # ISO 8601
    hash_date: str  # ISO 8601


@dataclass
class VerifyIssue:
    relative_path: str
    kind: str  # "missing" | "size" | "hash" | "error"
    detail: str = ""


@dataclass
class VerifyResult:
    manifest: Path
    checked: int = 0
    ok: int = 0
    issues: list[VerifyIssue] | None = None

    def __post_init__(self) -> None:
        if self.issues is None:
            self.issues = []

    @property
    def passed(self) -> bool:
        return not self.issues


def _iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso_from_timestamp(ts: float) -> str:
    """ISO-8601 UTC for a POSIX timestamp, tolerant of garbage camera timestamps.

    Cards regularly carry files with a zero Windows FILETIME (``st_mtime`` = -11644473600,
    i.e. 1601-01-01) - Sony's ``PRIVATE/SONY/SONYCARD.IND`` is one. ``datetime.fromtimestamp``
    raises ``OSError [Errno 22]`` for those on Windows, so convert arithmetically instead and
    clamp anything outside the datetime range.
    """
    try:
        return _iso(_EPOCH + timedelta(seconds=ts))
    except (OverflowError, ValueError, TypeError):
        return _iso(_EPOCH)


def write_mhl(
    mhl_path: Path,
    entries: Iterable[HashEntry],
    start: datetime,
    finish: datetime | None = None,
    comment: str = "",
) -> Path:
    root = ET.Element("hashlist", version="1.1")
    creator = ET.SubElement(root, "creatorinfo")
    ET.SubElement(creator, "name").text = APP_NAME
    try:
        ET.SubElement(creator, "username").text = getpass.getuser()
    except Exception:  # pragma: no cover - environment specific
        ET.SubElement(creator, "username").text = "unknown"
    ET.SubElement(creator, "hostname").text = socket.gethostname()
    ET.SubElement(creator, "tool").text = f"{APP_NAME} {__version__}"
    ET.SubElement(creator, "startdate").text = _iso(start)
    ET.SubElement(creator, "finishdate").text = _iso(finish)
    if comment:
        ET.SubElement(creator, "log").text = comment

    for e in entries:
        h = ET.SubElement(root, "hash")
        ET.SubElement(h, "file").text = e.relative_path
        ET.SubElement(h, "size").text = str(e.size)
        ET.SubElement(h, "lastmodificationdate").text = e.last_modified
        ET.SubElement(h, HASH_TAG).text = e.xxh64
        ET.SubElement(h, "hashdate").text = e.hash_date

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tmp = mhl_path.with_suffix(mhl_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(mhl_path)
    return mhl_path


def read_mhl(mhl_path: Path) -> list[HashEntry]:
    tree = ET.parse(mhl_path)
    root = tree.getroot()
    entries: list[HashEntry] = []
    for h in root.findall("hash"):
        rel = (h.findtext("file") or "").strip()
        digest = (h.findtext(HASH_TAG) or "").strip().lower()
        if not rel or not digest:
            # Manifest written by another tool with a different algorithm.
            continue
        entries.append(
            HashEntry(
                relative_path=rel,
                size=int(h.findtext("size") or 0),
                xxh64=digest,
                last_modified=h.findtext("lastmodificationdate") or "",
                hash_date=h.findtext("hashdate") or "",
            )
        )
    return entries


def write_xxh64_sidecar(path: Path, entries: Iterable[HashEntry]) -> Path:
    """Write a ``xxh64sum``-compatible text file: ``<hash>  <relative path>``."""
    lines = [f"{e.xxh64}  {e.relative_path}" for e in entries]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)
    return path


def find_manifests(folder: Path, recursive: bool = True) -> list[Path]:
    pattern = "**/*.mhl" if recursive else "*.mhl"
    return sorted(p for p in folder.glob(pattern) if p.is_file())


def verify_manifest(
    mhl_path: Path,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> VerifyResult:
    """Re-hash every file listed in the manifest and compare.

    *progress* receives (bytes_processed_delta, current_relative_path).
    """
    base = mhl_path.parent
    result = VerifyResult(manifest=mhl_path)
    for entry in read_mhl(mhl_path):
        if cancelled is not None and cancelled():
            raise Cancelled()
        result.checked += 1
        target = base / Path(*entry.relative_path.split("/"))
        if not target.is_file():
            result.issues.append(VerifyIssue(entry.relative_path, "missing"))
            continue
        try:
            size = target.stat().st_size
            if size != entry.size:
                result.issues.append(
                    VerifyIssue(entry.relative_path, "size", f"expected {entry.size}, found {size}")
                )
                # Still count bytes so progress stays accurate.
                if progress is not None:
                    progress(size, entry.relative_path)
                continue
            digest = xxh64_file(
                target,
                progress=(lambda n, rel=entry.relative_path: progress(n, rel)) if progress else None,
                cancelled=cancelled,
            )
        except Cancelled:
            raise
        except OSError as exc:
            result.issues.append(VerifyIssue(entry.relative_path, "error", str(exc)))
            continue
        if digest.lower() != entry.xxh64.lower():
            result.issues.append(
                VerifyIssue(entry.relative_path, "hash", f"expected {entry.xxh64}, found {digest}")
            )
        else:
            result.ok += 1
    return result


def manifest_total_bytes(mhl_path: Path) -> int:
    return sum(e.size for e in read_mhl(mhl_path))

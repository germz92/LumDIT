"""Removable volume detection and eject for Windows and macOS."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


@dataclass(frozen=True)
class Volume:
    mountpoint: str
    label: str
    fstype: str
    total: int
    free: int
    removable: bool
    # Stable across remounts: Mac ``disk4s1`` / Volume UUID, Windows volume serial.
    # Empty when the OS has not published one yet (first-mount race).
    identity: str = ""

    @property
    def display_name(self) -> str:
        if IS_WINDOWS:
            drive = self.mountpoint.rstrip("\\/")
            return f"{self.label or 'Removable Disk'} ({drive})"
        return self.label or Path(self.mountpoint).name or self.mountpoint

    @property
    def path(self) -> Path:
        return Path(self.mountpoint)


def _windows_volume_info(mountpoint: str) -> tuple[str, str]:
    """Return (label, identity) from the volume serial. Identity survives remounts."""
    if not IS_WINDOWS:
        return "", ""
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint(0)
    root = mountpoint if mountpoint.endswith("\\") else mountpoint + "\\"
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf), ctypes.byref(serial), None, None, fs, ctypes.sizeof(fs)
    )
    if not ok:
        return "", ""
    ident = f"win:{serial.value:08x}" if serial.value else ""
    return buf.value, ident


def _windows_label(mountpoint: str) -> str:
    return _windows_volume_info(mountpoint)[0]


def _windows_is_removable(mountpoint: str) -> bool:
    if not IS_WINDOWS:
        return False
    root = mountpoint if mountpoint.endswith("\\") else mountpoint + "\\"
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))  # type: ignore[attr-defined]
    return drive_type == 2  # DRIVE_REMOVABLE


def parse_diskutil_info(text: str) -> tuple[bool, str]:
    """Parse ``diskutil info`` text into (removable, identity).

    Identity is the Device Identifier (``disk4s1``) or Volume UUID so a remount
    from ``/Volumes/Untitled`` to ``/Volumes/EOS_DIGITAL`` stays the same card.
    """
    ident = uuid = ""
    removable_line = ""
    ejectable = False
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "device identifier":
            ident = val
        elif key == "volume uuid":
            uuid = val
        elif key == "removable media":
            removable_line = val.lower()
        elif key == "ejectable":
            ejectable = val.lower().startswith("yes")
    # "Removable" but not "Non-Removable" / "No".
    removable = removable_line.startswith("yes") or removable_line == "removable"
    if not removable_line:
        removable = ejectable or "removable" in text.lower()
    return removable or ejectable, ident or uuid


# ``diskutil info`` talks to the device, so each mount is asked once and the answer is
# kept until it unmounts. Asking on every poll kept card readers busy, and a slow or
# empty answer made a mounted card look removed and then newly inserted.
_MAC_LOOKUP_ATTEMPTS = 3
_MAC_RETRY_SECONDS = 1.5
_MAC_CARD_FSTYPES = frozenset({"msdos", "exfat"})

_MacKey = tuple[str, str]  # (mountpoint, device node)
_mac_lock = threading.Lock()
_mac_info: dict[_MacKey, tuple[bool, str]] = {}
_mac_failures: dict[_MacKey, tuple[int, float]] = {}  # attempts, earliest retry time
_mac_pending: set[_MacKey] = set()


def _mac_query(mountpoint: str) -> tuple[bool, str] | None:
    """Ask diskutil about a mount. None when it gave no usable answer."""
    try:
        r = subprocess.run(["diskutil", "info", mountpoint], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return parse_diskutil_info(r.stdout)


def _device_identity(device: str) -> str:
    """``/dev/disk4s1`` -> ``disk4s1``, matching diskutil's Device Identifier."""
    return device[len("/dev/"):] if device.startswith("/dev/") else ""


def _mac_resolve(key: _MacKey, fstype: str) -> tuple[bool, str]:
    mountpoint, device = key
    answer = _mac_query(mountpoint)
    with _mac_lock:
        _mac_pending.discard(key)
        if answer is None:
            attempts = _mac_failures.get(key, (0, 0.0))[0] + 1
            if attempts < _MAC_LOOKUP_ATTEMPTS:
                _mac_failures[key] = (attempts, time.monotonic() + _MAC_RETRY_SECONDS)
                return False, _device_identity(device)
            # diskutil cannot describe this mount (network share, disk image): stop
            # asking and go by the filesystem, since cards are FAT32 or exFAT.
            answer = (fstype.lower() in _MAC_CARD_FSTYPES, "")
        info = (answer[0], answer[1] or _device_identity(device))
        _mac_info[key] = info
        _mac_failures.pop(key, None)
        return info


def _mac_disk_info(mountpoint: str, device: str = "", fstype: str = "", wait: bool = True) -> tuple[bool, str]:
    """Return (removable, identity) for a /Volumes mount.

    With ``wait=False`` an unknown mount is looked up on a background thread and
    reported as not removable until the answer arrives (see ``lookups_pending``).
    """
    if not mountpoint.startswith("/Volumes/"):
        return False, ""
    key = (mountpoint, device)
    with _mac_lock:
        known = _mac_info.get(key)
        if known is not None:
            return known
        if not wait:
            retry_at = _mac_failures.get(key, (0, 0.0))[1]
            if key not in _mac_pending and time.monotonic() >= retry_at:
                _mac_pending.add(key)
                threading.Thread(
                    target=_mac_resolve, args=(key, fstype), name="lumdit-diskutil", daemon=True
                ).start()
            return False, _device_identity(device)
    return _mac_resolve(key, fstype)


def _mac_forget_unmounted(mounted: set[_MacKey]) -> None:
    with _mac_lock:
        for cache in (_mac_info, _mac_failures):
            for key in [k for k in cache if k not in mounted]:
                del cache[key]


def lookups_pending() -> bool:
    """True while a background volume lookup is running; poll again shortly."""
    with _mac_lock:
        return bool(_mac_pending)


def normalize_mountpoint(mountpoint: str) -> str:
    """Stable volume identity: ``E:`` and ``E:\\`` are the same reader slot."""
    raw = str(mountpoint).strip()
    if IS_WINDOWS:
        drive = raw.replace("/", "\\").rstrip("\\")
        if len(drive) == 2 and drive[1] == ":":
            return os.path.normcase(drive + "\\")
        mp = os.path.normcase(os.path.abspath(raw))
        return mp.rstrip("\\/") + "\\"
    mp = os.path.normcase(os.path.abspath(raw))
    return mp.rstrip("/") or "/"


_usage_lock = threading.Lock()
_last_usage: dict[str, tuple[int, int]] = {}  # mountpoint -> (total, free)


def _usage(mountpoint: str) -> tuple[int, int] | None:
    """(total, free), falling back to the last reading while the volume stays mounted.

    A busy card can fail one size check; that must not look like the card was removed.
    """
    try:
        usage = psutil.disk_usage(mountpoint)
    except (PermissionError, OSError):
        with _usage_lock:
            return _last_usage.get(mountpoint)
    with _usage_lock:
        _last_usage[mountpoint] = (usage.total, usage.free)
    return usage.total, usage.free


def list_volumes(include_fixed: bool = True, wait: bool = True) -> list[Volume]:
    """Mounted volumes. ``wait=False`` never blocks on a macOS disk lookup."""
    volumes: list[Volume] = []
    mounted: set[str] = set()
    mac_mounted: set[_MacKey] = set()
    for part in psutil.disk_partitions(all=False):
        mp = normalize_mountpoint(part.mountpoint)
        if IS_MAC and mp != "/" and not mp.startswith("/Volumes/"):
            continue
        if "cdrom" in part.opts or part.fstype == "":
            continue
        mounted.add(mp)
        mac_mounted.add((mp, part.device))
        usage = _usage(mp)
        if usage is None:
            continue
        total, free = usage
        identity = ""
        if IS_WINDOWS:
            removable = _windows_is_removable(mp) or "removable" in part.opts
            label, identity = _windows_volume_info(mp)
        elif IS_MAC:
            removable, identity = _mac_disk_info(mp, part.device, part.fstype, wait=wait)
            label = Path(mp).name if mp != "/" else "Macintosh HD"
        else:
            removable = "/media/" in mp or "/run/media/" in mp
            label = Path(mp).name
        if not include_fixed and not removable:
            continue
        volumes.append(
            Volume(
                mountpoint=mp,
                label=label,
                fstype=part.fstype,
                total=total,
                free=free,
                removable=removable,
                identity=identity,
            )
        )
    with _usage_lock:
        for mp in [m for m in _last_usage if m not in mounted]:
            del _last_usage[mp]
    _mac_forget_unmounted(mac_mounted)
    return volumes


def removable_volumes(wait: bool = True) -> list[Volume]:
    return [v for v in list_volumes(wait=wait) if v.removable]


def volume_for_path(path: Path | str) -> Volume | None:
    """Longest-prefix match of *path* against mounted volumes."""
    p = os.path.normcase(os.path.abspath(str(path)))
    best: Volume | None = None
    for v in list_volumes():
        mp = normalize_mountpoint(v.mountpoint)
        mp_cmp = mp.rstrip("\\/") + os.sep
        if p == mp.rstrip("\\/") or p.startswith(mp_cmp) or (mp == os.sep and p.startswith(os.sep)):
            if best is None or len(mp) > len(best.mountpoint):
                best = v
    return best


def device_key(path: Path | str) -> str:
    """Stable key identifying the physical volume a path lives on (for job serialisation)."""
    v = volume_for_path(path)
    if v is None:
        return normalize_mountpoint(str(path))
    if v.identity:
        return "id:" + v.identity
    return normalize_mountpoint(v.mountpoint)


def free_space(path: Path | str) -> int:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return psutil.disk_usage(str(p)).free


def eject(volume: Volume) -> tuple[bool, str]:
    """Safely eject/unmount a removable volume. Returns (ok, message)."""
    try:
        if IS_MAC:
            r = subprocess.run(
                ["diskutil", "eject", volume.mountpoint], capture_output=True, text=True, timeout=30
            )
            return r.returncode == 0, (r.stdout or r.stderr).strip()
        if IS_WINDOWS:
            drive = volume.mountpoint.rstrip("\\/")
            script = (
                "$sh = New-Object -ComObject Shell.Application; "
                f"$d = $sh.Namespace(17).ParseName('{drive}\\'); "
                "if ($d) { $d.InvokeVerb('Eject'); 'ok' } else { 'not found' }"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            ok = r.returncode == 0 and "ok" in r.stdout
            return ok, (r.stdout or r.stderr).strip()
        r = subprocess.run(["udisksctl", "unmount", "-b", volume.mountpoint], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as exc:  # pragma: no cover - platform dependent
        return False, str(exc)

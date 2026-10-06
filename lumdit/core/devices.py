"""Removable volume detection and eject for Windows and macOS."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
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

    @property
    def display_name(self) -> str:
        if IS_WINDOWS:
            drive = self.mountpoint.rstrip("\\/")
            return f"{self.label or 'Removable Disk'} ({drive})"
        return self.label or Path(self.mountpoint).name or self.mountpoint

    @property
    def path(self) -> Path:
        return Path(self.mountpoint)


def _windows_label(mountpoint: str) -> str:
    if not IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    root = mountpoint if mountpoint.endswith("\\") else mountpoint + "\\"
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf), None, None, None, fs, ctypes.sizeof(fs)
    )
    return buf.value if ok else ""


def _windows_is_removable(mountpoint: str) -> bool:
    if not IS_WINDOWS:
        return False
    root = mountpoint if mountpoint.endswith("\\") else mountpoint + "\\"
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))  # type: ignore[attr-defined]
    return drive_type == 2  # DRIVE_REMOVABLE


def _mac_is_removable(mountpoint: str) -> bool:
    if not mountpoint.startswith("/Volumes/"):
        return False
    try:
        out = subprocess.run(
            ["diskutil", "info", mountpoint], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return True
    lower = out.lower()
    if "removable media:" in lower:
        return "removable" in lower.split("removable media:")[1].split("\n")[0]
    return "ejectable: yes" in lower or "removable" in lower


def list_volumes(include_fixed: bool = True) -> list[Volume]:
    volumes: list[Volume] = []
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if IS_MAC and mp != "/" and not mp.startswith("/Volumes/"):
            continue
        if "cdrom" in part.opts or part.fstype == "":
            continue
        try:
            usage = psutil.disk_usage(mp)
        except (PermissionError, OSError):
            continue
        if IS_WINDOWS:
            removable = _windows_is_removable(mp) or "removable" in part.opts
            label = _windows_label(mp)
        elif IS_MAC:
            removable = _mac_is_removable(mp)
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
                total=usage.total,
                free=usage.free,
                removable=removable,
            )
        )
    return volumes


def removable_volumes() -> list[Volume]:
    return [v for v in list_volumes() if v.removable]


def volume_for_path(path: Path | str) -> Volume | None:
    """Longest-prefix match of *path* against mounted volumes."""
    p = os.path.normcase(os.path.abspath(str(path)))
    best: Volume | None = None
    for v in list_volumes():
        mp = os.path.normcase(os.path.abspath(v.mountpoint))
        mp_cmp = mp.rstrip("\\/") + os.sep
        if p == mp.rstrip("\\/") or p.startswith(mp_cmp) or (mp == os.sep and p.startswith(os.sep)):
            if best is None or len(mp) > len(best.mountpoint):
                best = v
    return best


def device_key(path: Path | str) -> str:
    """Stable key identifying the physical volume a path lives on (for job serialisation)."""
    v = volume_for_path(path)
    return os.path.normcase(v.mountpoint) if v else os.path.normcase(os.path.abspath(str(path)))


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

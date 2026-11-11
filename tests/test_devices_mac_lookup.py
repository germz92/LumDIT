"""macOS volume polling: diskutil is asked once per mount, never on every poll."""

import threading
import time
from types import SimpleNamespace

import pytest

from lumdit.core import devices

CARD = "/Volumes/EOS_DIGITAL"


class FakeMac:
    def __init__(self) -> None:
        self.parts: list[SimpleNamespace] = []
        self.answers: dict[str, tuple[bool, str] | None] = {}
        self.queries: list[str] = []
        self.usage_error = False

    def mount(self, mountpoint: str, device: str = "/dev/disk4s1", fstype: str = "exfat") -> None:
        self.parts.append(SimpleNamespace(mountpoint=mountpoint, device=device, fstype=fstype, opts="rw"))

    def unmount(self, mountpoint: str) -> None:
        self.parts = [p for p in self.parts if p.mountpoint != mountpoint]

    def query(self, mountpoint: str) -> tuple[bool, str] | None:
        self.queries.append(mountpoint)
        return self.answers.get(mountpoint)

    def usage(self, _mountpoint: str) -> SimpleNamespace:
        if self.usage_error:
            raise OSError("Input/output error")
        return SimpleNamespace(total=64 * 10**9, free=10 * 10**9)


@pytest.fixture
def mac(monkeypatch) -> FakeMac:
    fake = FakeMac()
    monkeypatch.setattr(devices, "IS_MAC", True)
    monkeypatch.setattr(devices, "IS_WINDOWS", False)
    monkeypatch.setattr(devices, "normalize_mountpoint", lambda m: str(m))
    monkeypatch.setattr(devices.psutil, "disk_partitions", lambda all=False: list(fake.parts))
    monkeypatch.setattr(devices.psutil, "disk_usage", fake.usage)
    monkeypatch.setattr(devices, "_mac_query", fake.query)
    caches = (devices._mac_info, devices._mac_failures, devices._mac_pending, devices._last_usage)
    for cache in caches:
        cache.clear()
    yield fake
    for cache in caches:
        cache.clear()


def _wait_for_lookups() -> None:
    deadline = time.monotonic() + 5
    while devices.lookups_pending():
        assert time.monotonic() < deadline, "background lookup never finished"
        time.sleep(0.01)


def test_diskutil_asked_once_per_mount(mac: FakeMac) -> None:
    mac.mount("/", device="/dev/disk3s1", fstype="apfs")
    mac.mount(CARD)
    mac.answers[CARD] = (True, "disk4s1")
    for _ in range(5):
        cards = devices.removable_volumes()
        assert [(v.mountpoint, v.identity) for v in cards] == [(CARD, "disk4s1")]
    assert mac.queries == [CARD]


def test_known_card_survives_diskutil_going_quiet(mac: FakeMac) -> None:
    mac.mount(CARD)
    mac.answers[CARD] = (True, "disk4s1")
    assert len(devices.removable_volumes()) == 1
    mac.answers[CARD] = None  # busy card: diskutil now times out or prints nothing
    for wait in (True, False, False):
        assert [v.mountpoint for v in devices.removable_volumes(wait=wait)] == [CARD]
    assert not devices.lookups_pending()


def test_failed_size_check_keeps_card(mac: FakeMac) -> None:
    mac.mount(CARD)
    mac.answers[CARD] = (True, "disk4s1")
    assert len(devices.removable_volumes()) == 1
    mac.usage_error = True
    cards = devices.removable_volumes(wait=False)
    assert [(v.mountpoint, v.total) for v in cards] == [(CARD, 64 * 10**9)]


def test_remount_is_looked_up_again(mac: FakeMac) -> None:
    mac.mount(CARD)
    mac.answers[CARD] = (True, "disk4s1")
    devices.removable_volumes()
    mac.unmount(CARD)
    assert devices.removable_volumes() == []
    mac.mount(CARD)
    assert len(devices.removable_volumes()) == 1
    assert mac.queries == [CARD, CARD]


def test_unanswered_mount_retries_then_goes_by_filesystem(mac: FakeMac) -> None:
    share = "/Volumes/Projects"
    mac.mount(CARD)
    mac.mount(share, device="//nas/Projects", fstype="smbfs")
    assert devices.removable_volumes() == []  # unknown yet: not announced as a card
    devices.removable_volumes()
    cards = devices.removable_volumes()
    assert [(v.mountpoint, v.identity) for v in cards] == [(CARD, "disk4s1")]
    asked = len(mac.queries)
    assert asked == 2 * devices._MAC_LOOKUP_ATTEMPTS
    devices.removable_volumes()
    assert len(mac.queries) == asked  # settled; no further diskutil calls


def test_poll_does_not_wait_for_diskutil(mac: FakeMac, monkeypatch) -> None:
    release = threading.Event()
    asked_on: list[str] = []

    def slow_query(mountpoint: str) -> tuple[bool, str] | None:
        asked_on.append(threading.current_thread().name)
        release.wait(5)
        return (True, "disk4s1")

    monkeypatch.setattr(devices, "_mac_query", slow_query)
    mac.mount(CARD)
    try:
        assert devices.removable_volumes(wait=False) == []
        assert devices.lookups_pending()
        assert devices.removable_volumes(wait=False) == []  # no second lookup while one runs
    finally:
        release.set()
    _wait_for_lookups()
    assert [v.identity for v in devices.removable_volumes(wait=False)] == ["disk4s1"]
    assert asked_on == ["lumdit-diskutil"]

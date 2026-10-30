"""Cards & Devices backup-status icons (clock / check)."""

from pathlib import Path

from PySide6.QtWidgets import QApplication

from lumdit.core.devices import Volume
from lumdit.ui.sidebar import DeviceList, _STATUS_TIP, _backup_icon


def test_backup_icons_are_distinct() -> None:
    QApplication.instance() or QApplication([])
    clock = _backup_icon("pending")
    check = _backup_icon("done")
    assert not clock.isNull() and not check.isNull()
    assert clock.cacheKey() != check.cacheKey()


def test_device_list_clock_then_check(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    vol = Volume(mountpoint="E:\\CARD", label="SONY", fstype="exfat", total=10**9, free=5 * 10**8, removable=True)
    monkeypatch.setattr("lumdit.core.devices.removable_volumes", lambda: [vol])
    monkeypatch.setattr("lumdit.core.devices.volume_for_path", lambda p: vol)
    lst = DeviceList()
    lst._timer.stop()
    lst.refresh()
    assert lst.count() == 1
    item = lst.item(0)
    assert item.icon().isNull()
    assert _STATUS_TIP["pending"] not in item.toolTip()

    lst.set_backup_status(Path(vol.mountpoint), "pending")
    assert _STATUS_TIP["pending"] in lst.item(0).toolTip()
    assert not lst.item(0).icon().isNull()

    lst.set_backup_status(Path(vol.mountpoint), "done")
    assert _STATUS_TIP["done"] in lst.item(0).toolTip()

    lst.set_backup_status(Path(vol.mountpoint), None)
    assert lst.item(0).icon().isNull()
    lst.deleteLater()

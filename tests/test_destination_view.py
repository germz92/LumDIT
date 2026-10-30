from datetime import date
from pathlib import Path

from PySide6.QtWidgets import QApplication

from lumdit.core.production import create_production
from lumdit.core.offload import is_destination_hidden
from lumdit.ui.production_view import ProductionView


def test_destination_hidden_names() -> None:
    assert is_destination_hidden("card_20261005.mhl")
    assert is_destination_hidden("card.xxh64")
    assert is_destination_hidden("clip.mov.part")
    assert is_destination_hidden("OFFLOAD_REPORT.txt")
    assert is_destination_hidden("production.json")
    assert is_destination_hidden(".DS_Store")
    assert not is_destination_hidden("DSC00001.ARW")
    assert not is_destination_hidden("C0001.MP4")
    assert not is_destination_hidden("_CONFLICTS")


def test_icon_view_hides_sidecars_until_checked(tmp_path: Path) -> None:
    QApplication.instance() or QApplication([])
    prod = create_production(tmp_path, "Acme", "Launch", date(2026, 10, 5), date(2026, 10, 5), ["Photo"])
    card = prod.root / "Photo" / "10.05.2026" / "A7IV - G (#1)"
    card.mkdir(parents=True)
    (card / "DSC00001.ARW").write_bytes(b"raw")
    (card / "Launch_20261005.mhl").write_text("<mhl/>")
    (card / "Launch.xxh64").write_text("x")
    (card / "OFFLOAD_REPORT.txt").write_text("ok")

    view = ProductionView()
    view.set_production(prod)
    assert not view.hidden_box.isChecked()
    assert view.stack.currentWidget() is view.icons
    view._set_icon_folder(card)
    names = [e.path.name for e in view.icon_model.entries]
    assert names == ["DSC00001.ARW"]

    view.hidden_box.setChecked(True)
    names = [e.path.name for e in view.icon_model.entries]
    assert "DSC00001.ARW" in names
    assert "Launch_20261005.mhl" in names
    assert "OFFLOAD_REPORT.txt" in names
    view.deleteLater()

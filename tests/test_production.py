from datetime import date

import pytest

from lumdit.core.production import (
    CATEGORY_SUBFOLDERS,
    PRESET_CATEGORIES,
    OffloadRecord,
    Production,
    ProductionError,
    create_production,
)


def _record(camera="FX3", operator="Gerry", card=1, category="Video", fingerprint="abc", status="verified"):
    return OffloadRecord(
        timestamp="t", category=category, date="2026-03-01", camera=camera, operator=operator,
        card=card, destination="d", source="s", source_label="SD", fingerprint=fingerprint,
        file_count=3, total_bytes=10, status=status,
    )


def test_create_production_structure_under_client(tmp_path):
    prod = create_production(
        tmp_path, "Acme Corp", "Launch", date(2026, 10, 5), date(2026, 10, 7), PRESET_CATEGORIES
    )
    root = tmp_path / "Acme Corp" / "Launch"
    assert prod.root == root
    assert prod.client == "Acme Corp"
    assert prod.display_name == "Acme Corp - Launch"
    assert (root / "production.json").is_file()
    for cat in PRESET_CATEGORIES:
        for day in ("10.05.2026", "10.06.2026", "10.07.2026"):
            assert (root / cat / day).is_dir()
        for sub in CATEGORY_SUBFOLDERS:
            assert (root / cat / sub).is_dir()
    # No stray folders.
    assert sorted(p.name for p in (root / "Photo").iterdir()) == sorted(
        ["10.05.2026", "10.06.2026", "10.07.2026", *CATEGORY_SUBFOLDERS]
    )


def test_two_productions_share_a_client_folder(tmp_path):
    a = create_production(tmp_path, "Acme", "Spring", date(2026, 1, 1), date(2026, 1, 1), ["Photo"])
    b = create_production(tmp_path, "Acme", "Fall", date(2026, 9, 1), date(2026, 9, 1), ["Photo"])
    assert a.root.parent == b.root.parent == tmp_path / "Acme"


def test_custom_category_and_sanitised_names(tmp_path):
    prod = create_production(
        tmp_path, "Cli:ent", "Bad:Name?", date(2026, 1, 1), date(2026, 1, 1), ["Photo", "BTS/Stills"]
    )
    assert prod.root.parent.name == "Cli_ent"
    assert prod.root.name == "Bad_Name_"
    assert (prod.root / "BTS_Stills" / "01.01.2026").is_dir()


def test_invalid_inputs(tmp_path):
    with pytest.raises(ProductionError):
        create_production(tmp_path, "", "X", date(2026, 1, 1), date(2026, 1, 1), ["Photo"])
    with pytest.raises(ProductionError):
        create_production(tmp_path, "C", "", date(2026, 1, 1), date(2026, 1, 1), ["Photo"])
    with pytest.raises(ProductionError):
        create_production(tmp_path, "C", "X", date(2026, 1, 2), date(2026, 1, 1), ["Photo"])
    with pytest.raises(ProductionError):
        create_production(tmp_path, "C", "X", date(2026, 1, 1), date(2026, 1, 1), [])
    create_production(tmp_path, "C", "Dup", date(2026, 1, 1), date(2026, 1, 1), ["Photo"])
    with pytest.raises(ProductionError):
        create_production(tmp_path, "C", "Dup", date(2026, 1, 1), date(2026, 1, 1), ["Photo"])


def test_save_load_round_trip(tmp_path):
    prod = create_production(tmp_path, "Acme", "P", date(2026, 3, 1), date(2026, 3, 2), ["Video"])
    prod.record_offload(_record(card=12))
    assert prod.defaults_for("Video") == {"camera": "FX3", "operator": "Gerry", "card": 12}
    # A different category falls back to the most recent values.
    assert prod.defaults_for("Photo")["camera"] == "FX3"

    loaded = Production.load(prod.root)
    assert loaded.client == "Acme"
    assert loaded.name == "P"
    assert loaded.start_date == date(2026, 3, 1)
    assert loaded.categories == ["Video"]
    assert loaded.offloads[0].fingerprint == "abc"
    assert loaded.find_duplicate("abc") is not None
    assert loaded.find_duplicate("zzz") is None


def test_load_legacy_production_without_client(tmp_path):
    prod = create_production(tmp_path, "Acme", "P", date(2026, 3, 1), date(2026, 3, 1), ["Video"])
    text = prod.file_path.read_text(encoding="utf-8").replace('"client": "Acme",', "")
    prod.file_path.write_text(text, encoding="utf-8")
    loaded = Production.load(prod.root)
    assert loaded.client == ""
    assert loaded.display_name == "P"


def test_card_numbers_are_physical_labels_not_auto_incremented(tmp_path):
    prod = create_production(tmp_path, "Acme", "P", date(2026, 3, 1), date(2026, 3, 2), ["Video"])
    assert prod.last_card_number("FX3", "Gerry") is None
    assert prod.card_history("FX3", "Gerry") == []

    prod.record_offload(_record(card=12, fingerprint="a"))
    prod.record_offload(_record(card=3, fingerprint="b"))
    prod.record_offload(_record(card=12, fingerprint="c"))  # same card reused later
    prod.record_offload(_record(camera="A7IV", card=5, fingerprint="d"))

    assert prod.last_card_number("fx3", "gerry") == 12
    assert prod.card_history("FX3", "Gerry") == [12, 3]
    assert len(prod.offloads_for_card("FX3", "Gerry", 12)) == 2
    assert prod.offloads_for_card("FX3", "Gerry", 99) == []
    assert prod.last_card_number("A7IV", "Gerry") == 5


def test_card_dir_path(tmp_path):
    prod = create_production(tmp_path, "Acme", "P", date(2026, 3, 1), date(2026, 3, 1), ["Photo"])
    p = prod.card_dir("Photo", date(2026, 3, 1), "A7IV", "Germaine", 12)
    assert p == prod.root / "Photo" / "03.01.2026" / "A7IV - Germaine (#12)"

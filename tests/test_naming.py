from datetime import date

from lumdit.core.naming import (
    format_card_folder,
    format_date_folder,
    parse_date_folder,
    sanitize_name,
    validate_template,
)


def test_sanitize_strips_illegal_characters():
    assert sanitize_name('My: Prod/Name?*') == "My_ Prod_Name__"


def test_sanitize_trailing_dots_and_spaces():
    assert sanitize_name("Name.  ") == "Name"


def test_sanitize_reserved_windows_names():
    assert sanitize_name("CON") == "_CON"
    assert sanitize_name("con.txt") == "_con.txt"


def test_sanitize_empty():
    assert sanitize_name("   ") == "Untitled"


def test_card_folder_default_template():
    assert format_card_folder("FX3", "Germaine", 12) == "FX3 - Germaine (#12)"
    assert format_card_folder(" A7IV ", "Sam", 7) == "A7IV - Sam (#7)"
    # Physical card numbers are not zero padded.
    assert format_card_folder("FX3", "Sam", 101) == "FX3 - Sam (#101)"


def test_card_folder_text_labels():
    assert format_card_folder("Pocket4", "Chris", "Internal") == "Pocket4 - Chris (Internal)"
    assert format_card_folder("A7IV", "Sam", "Sandisk 256") == "A7IV - Sam (Sandisk 256)"
    # Numeric strings are normalised to #n.
    assert format_card_folder("A7IV", "Sam", "32") == "A7IV - Sam (#32)"


def test_card_folder_custom_template_and_fallback():
    assert format_card_folder("FX3", "Gerry", 3, "{operator}_{camera}_{card}") == "Gerry_FX3_#3"
    # Legacy template from the first release maps to the new one.
    assert format_card_folder("FX3", "Gerry", 3, "{camera} - {operator} (#{card})") == "FX3 - Gerry (#3)"
    # Broken template falls back to the default rather than crashing.
    assert format_card_folder("FX3", "Gerry", 3, "{nope}") == "FX3 - Gerry (#3)"


def test_date_folder_round_trip():
    d = date(2026, 10, 5)
    name = format_date_folder(d)
    assert name == "10.05.2026"
    assert parse_date_folder(name) == d
    assert parse_date_folder("Project Files") is None
    assert parse_date_folder("13.45.2026") is None


def test_validate_template():
    assert validate_template("{camera} - {operator}{card}")
    assert not validate_template("{bogus}")

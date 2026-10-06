"""Card-log parsing and mapping tests (no database needed)."""

import os
from datetime import date, datetime, timezone

import pytest
from bson import ObjectId

from lumdit.core.cardlog import (
    CardLogClient,
    backed_up_update,
    normalise_category,
    parse_card_value,
    parse_event,
)
from lumdit.core.production import Production, find_or_create_for_event

EID = ObjectId()
E1, E2, E3, E4, E5, E6 = (ObjectId() for _ in range(6))


def _entry(_id, camera, card1, card2, user, category, **flags):
    d = {
        "_id": _id,
        "camera": camera,
        "card1": card1,
        "card2": card2,
        "user": user,
        "notes": "",
        "createdAt": datetime(2026, 9, 22, 1, 51, tzinfo=timezone.utc),
    }
    if category is not None:
        d["category"] = category
    d.update(flags)
    return d


EVENT_DOC = {
    "_id": EID,
    "title": "ProofPoint Protect",
    "archived": False,
    "general": {"client": "Jane Doe", "company": "Proofpoint", "start": "2026-09-21", "end": "2026-09-23"},
    "cardLog": [
        {
            "date": "2026-09-22",
            "entries": [
                _entry(E4, "A7IV-F", "45", "18", "Carlos Aguon", "Photo", card1BackedUp=False, card2BackedUp=False),
            ],
        },
        {
            "date": "2026-09-21",
            "entries": [
                _entry(E1, "A7SIII", "32", "30", "Catrina Manchor", "Video", card1BackedUp=True, card2BackedUp=False),
                _entry(E2, "Pocket4", "Internal", "", "Catrina Manchor", "Video"),  # flags missing
                _entry(E3, "A71-200", "90", "146", "Jennifer Ross", "Headshot", card1BackedUp=False),
                _entry(E5, "A7RV", "#7", "8", "Jennifer Smith", None),  # no category
                _entry(E6, "Pocket 4p", "", "", "Chris Angeles", "Other"),  # empty card
            ],
        },
    ],
}


def test_parse_card_value():
    assert parse_card_value("32") == 32
    assert parse_card_value(" #7 ") == 7
    assert parse_card_value("Internal") == "Internal"
    assert parse_card_value("Sandisk   256") == "Sandisk 256"
    assert parse_card_value("") is None
    assert parse_card_value(None) is None


def test_normalise_category():
    assert normalise_category("Photo") == "Photo"
    assert normalise_category("video") == "Video"
    assert normalise_category("Headshot") == "Headshot Booth"
    assert normalise_category("Other") is None
    assert normalise_category(None) is None


def test_client_name_falls_back_to_client_when_company_blank():
    doc = {**EVENT_DOC, "general": {**EVENT_DOC["general"], "company": ""}}
    assert parse_event(doc).client_name == "Jane Doe"


def test_parse_event_shape_and_ordering():
    ev = parse_event(EVENT_DOC)
    assert ev.id == str(EID)
    assert ev.title == "ProofPoint Protect"
    # Client folder comes from the company name; `client` is the contact person.
    assert ev.client_name == "Proofpoint"
    assert ev.client == "Jane Doe"
    assert ev.start == date(2026, 9, 21) and ev.end == date(2026, 9, 23)
    # Days sorted chronologically even though the doc had them reversed.
    assert [d.day for d in ev.days] == [date(2026, 9, 21), date(2026, 9, 22)]
    assert len(ev.all_entries()) == 6
    e2 = ev.find_entry(str(E2))
    assert e2.card1_backed_up is False and e2.card2_backed_up is False  # missing flags -> False
    assert e2.is_internal(1) and not e2.offloadable(1)
    assert ev.find_entry(str(E3)).category == "Headshot Booth"
    assert ev.find_entry(str(E5)).category is None
    assert ev.find_entry(str(E5)).card_value(1) == 7
    assert ev.categories() == ["Video", "Headshot Booth", "Photo"]


def test_pending_and_progress():
    ev = parse_event(EVENT_DOC)
    # Eligible card-1 entries: E1 (done), E3, E5, E4. Internal (E2) and empty (E6) excluded.
    assert [e.id for e in ev.eligible_entries(1)] == [str(E1), str(E3), str(E5), str(E4)]
    assert [e.id for e in ev.pending_entries(1)] == [str(E3), str(E5), str(E4)]
    assert ev.progress(1) == (1, 4)
    # Card 2: E1, E3, E5, E4 have cards; none backed up.
    assert ev.progress(2) == (0, 4)
    ev.find_entry(str(E3)).set_backed_up(1)
    assert ev.progress(1) == (2, 4)


def test_operator_first_name_collision_rule():
    ev = parse_event(EVENT_DOC)
    assert ev.operator_name(ev.find_entry(str(E1))) == "Catrina"  # same person twice -> plain first name
    assert ev.operator_name(ev.find_entry(str(E3))) == "Jennifer R."  # two Jennifers
    assert ev.operator_name(ev.find_entry(str(E5))) == "Jennifer S."
    assert ev.operator_name(ev.find_entry(str(E4))) == "Carlos"


def test_describe_and_display():
    ev = parse_event(EVENT_DOC)
    e1 = ev.find_entry(str(E1))
    assert e1.display_card(1) == "#32"
    assert ev.find_entry(str(E2)).display_card(1) == "Internal"
    assert ev.find_entry(str(E6)).display_card(1) == "-"
    assert e1.describe(1).startswith("card #32")
    assert "09.21.2026" in e1.describe(1)


def test_backed_up_update_shape():
    update, filters = backed_up_update(E1, 1)
    assert update["$set"]["cardLog.$[].entries.$[e].card1BackedUp"] is True
    assert "cardLog.$[].entries.$[e].updatedAt" in update["$set"]
    assert filters == [{"e._id": E1}]
    update2, _ = backed_up_update(E1, 2, False)
    assert update2["$set"]["cardLog.$[].entries.$[e].card2BackedUp"] is False


def test_find_or_create_for_event_maps_paths(tmp_path):
    ev = parse_event(EVENT_DOC)
    prod = find_or_create_for_event(
        tmp_path, ev.id, ev.title, ev.client_name, ev.production_name, ev.start, ev.end, ev.categories()
    )
    assert prod.root == tmp_path / "Proofpoint" / "ProofPoint Protect"
    assert prod.event_id == ev.id
    assert prod.categories == ["Photo", "Video", "Headshot Booth"]
    assert (prod.root / "Video" / "09.22.2026").is_dir()

    e3 = ev.find_entry(str(E3))
    dest = prod.card_dir(e3.category, e3.day, e3.camera, ev.operator_name(e3), e3.card_value(1))
    assert dest == prod.root / "Headshot Booth" / "09.21.2026" / "A71-200 - Jennifer R. (#90)"

    # Second call reopens the same production and extends it instead of failing.
    again = find_or_create_for_event(
        tmp_path, ev.id, ev.title, ev.client_name, ev.production_name, date(2026, 9, 20), ev.end, ["Drone"]
    )
    assert again.root == prod.root
    assert again.start_date == date(2026, 9, 20)
    assert "Drone" in again.categories
    assert (again.root / "Photo" / "09.20.2026").is_dir()


def test_writeback_queue_round_trip(tmp_path):
    ev = parse_event(EVENT_DOC)
    prod = find_or_create_for_event(tmp_path, ev.id, ev.title, "C", "P", ev.start, ev.end, [])
    prod.queue_writeback(str(E3), 1)
    prod.queue_writeback(str(E3), 1)  # idempotent
    assert len(Production.load(prod.root).pending_writebacks) == 1
    prod.clear_writeback(str(E3), 1)
    assert Production.load(prod.root).pending_writebacks == []


def test_client_requires_uri():
    from lumdit.core.cardlog import CardLogError

    with pytest.raises(CardLogError):
        CardLogClient("")


@pytest.mark.skipif(not os.environ.get("MONGO_URI"), reason="MONGO_URI not set")
def test_live_connection_lists_events():
    client = CardLogClient(os.environ["MONGO_URI"])
    assert "Connected" in client.test()
    events = client.list_events()
    assert events and all(e.id for e in events)
    client.close()

"""Card logs from the crew app (MongoDB).

Shape of an event document in ``<db>.tables``::

    {
      "_id": ObjectId, "title": "ProofPoint Protect", "archived": false,
      "general": {"client": "Jane Doe", "company": "Proofpoint", "start": "2026-09-21", "end": "2026-09-23"},
      "cardLog": [
        {"date": "2026-09-21",
         "entries": [{"_id": ObjectId, "camera": "A7SIII", "card1": "32", "card2": "30",
                      "card1BackedUp": true, "card2BackedUp": false, "user": "Catrina Manchor",
                      "category": "Video", "notes": ""}]}
      ]
    }

Everything in this module except ``CardLogClient`` is pure Python so it can be
unit-tested without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from lumdit.core.naming import sanitize_name

DEFAULT_DB = "test"
DEFAULT_COLLECTION = "tables"
CATEGORY_HEADSHOT = "Headshot Booth"
_CATEGORY_MAP = {
    "photo": "Photo",
    "photos": "Photo",
    "stills": "Photo",
    "video": "Video",
    "videos": "Video",
    "headshot": CATEGORY_HEADSHOT,
    "headshots": CATEGORY_HEADSHOT,
    "headshot booth": CATEGORY_HEADSHOT,
    "booth": CATEGORY_HEADSHOT,
}
_INTERNAL_RE = re.compile(r"^\s*internal", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^\s*#?\s*(\d{1,5})\s*$")


def normalise_category(raw: str | None) -> str | None:
    """Map the crew app's category text onto LumDIT folder names (None if unknown)."""
    if not raw:
        return None
    return _CATEGORY_MAP.get(raw.strip().lower())


def parse_card_value(raw: str | None) -> int | str | None:
    """``"32"`` -> 32, ``"#7"`` -> 7, ``"Internal"`` -> "Internal", ``""`` -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = _NUMERIC_RE.match(text)
    if m:
        return int(m.group(1))
    return re.sub(r"\s+", " ", text)


def is_internal_label(raw: str | None) -> bool:
    return bool(raw) and bool(_INTERNAL_RE.match(str(raw)))


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _oid_str(value: Any) -> str:
    return str(value) if value is not None else ""


@dataclass
class CardLogEntry:
    id: str
    day: date | None
    camera: str
    card1: str
    card2: str
    card1_backed_up: bool
    card2_backed_up: bool
    user: str
    raw_category: str
    notes: str = ""
    created_at: datetime | None = None

    # ---- derived ------------------------------------------------------------
    @property
    def category(self) -> str | None:
        return normalise_category(self.raw_category)

    def card_label(self, slot: int) -> str:
        return (self.card1 if slot == 1 else self.card2).strip()

    def card_value(self, slot: int) -> int | str | None:
        return parse_card_value(self.card_label(slot))

    def is_internal(self, slot: int) -> bool:
        return is_internal_label(self.card_label(slot))

    def has_card(self, slot: int) -> bool:
        return self.card_value(slot) is not None

    def backed_up(self, slot: int) -> bool:
        return self.card1_backed_up if slot == 1 else self.card2_backed_up

    def set_backed_up(self, slot: int, value: bool = True) -> None:
        if slot == 1:
            self.card1_backed_up = value
        else:
            self.card2_backed_up = value

    def offloadable(self, slot: int) -> bool:
        """True if this slot is a physical card that can be inserted into a reader."""
        return self.has_card(slot) and not self.is_internal(slot)

    @property
    def first_name(self) -> str:
        parts = self.user.strip().split()
        return parts[0] if parts else ""

    @property
    def last_initial(self) -> str:
        parts = self.user.strip().split()
        return parts[-1][0].upper() if len(parts) > 1 else ""

    def display_card(self, slot: int) -> str:
        v = self.card_value(slot)
        if v is None:
            return "-"
        return f"#{v}" if isinstance(v, int) else str(v)

    def describe(self, slot: int = 1) -> str:
        bits = [self.camera or "camera?", self.user or "operator?", self.category or (self.raw_category or "category?")]
        if self.day:
            bits.append(self.day.strftime("%m.%d.%Y"))
        return f"card {self.display_card(slot)}  -  " + "  |  ".join(bits)


@dataclass
class CardLogDay:
    day: date | None
    entries: list[CardLogEntry] = field(default_factory=list)


@dataclass
class CardLogEvent:
    id: str
    title: str
    client: str = ""
    company: str = ""
    start: date | None = None
    end: date | None = None
    archived: bool = False
    days: list[CardLogDay] = field(default_factory=list)

    # ---- naming -------------------------------------------------------------
    @property
    def client_name(self) -> str:
        """Top-level client folder: the event's company name (falls back to `client` if blank)."""
        return (self.company or self.client or "").strip()

    @property
    def production_name(self) -> str:
        return sanitize_name(self.title) if self.title.strip() else "Untitled Event"

    def operator_name(self, entry: CardLogEntry) -> str:
        """First name, or ``First L.`` when two crew share a first name on this event."""
        first = entry.first_name
        if not first:
            return "Operator"
        others = {
            e.user.strip().lower()
            for e in self.all_entries()
            if e.first_name.lower() == first.lower() and e.user.strip()
        }
        if len(others) > 1 and entry.last_initial:
            return f"{first} {entry.last_initial}."
        return first

    # ---- entries ------------------------------------------------------------
    def all_entries(self) -> list[CardLogEntry]:
        return [e for d in self.days for e in d.entries]

    def find_entry(self, entry_id: str) -> CardLogEntry | None:
        return next((e for e in self.all_entries() if e.id == entry_id), None)

    def eligible_entries(self, slot: int = 1) -> list[CardLogEntry]:
        """Entries with a physical card in *slot* (ordered by day, then log order)."""
        return [e for e in self.all_entries() if e.offloadable(slot)]

    def pending_entries(self, slot: int = 1) -> list[CardLogEntry]:
        return [e for e in self.eligible_entries(slot) if not e.backed_up(slot)]

    def progress(self, slot: int = 1) -> tuple[int, int]:
        eligible = self.eligible_entries(slot)
        return sum(1 for e in eligible if e.backed_up(slot)), len(eligible)

    def categories(self) -> list[str]:
        out: list[str] = []
        for e in self.all_entries():
            c = e.category
            if c and c not in out:
                out.append(c)
        return out

    def dates(self) -> list[date]:
        return sorted({d.day for d in self.days if d.day})


def parse_event(doc: dict[str, Any]) -> CardLogEvent:
    general = doc.get("general") or {}
    days: list[CardLogDay] = []
    for day_doc in doc.get("cardLog") or []:
        if not isinstance(day_doc, dict):
            continue
        day = _parse_date(day_doc.get("date"))
        entries: list[CardLogEntry] = []
        for e in day_doc.get("entries") or []:
            if not isinstance(e, dict):
                continue
            created = e.get("createdAt")
            entries.append(
                CardLogEntry(
                    id=_oid_str(e.get("_id")),
                    day=day,
                    camera=str(e.get("camera") or "").strip(),
                    card1=str(e.get("card1") or ""),
                    card2=str(e.get("card2") or ""),
                    card1_backed_up=bool(e.get("card1BackedUp") or False),
                    card2_backed_up=bool(e.get("card2BackedUp") or False),
                    user=str(e.get("user") or "").strip(),
                    raw_category=str(e.get("category") or ""),
                    notes=str(e.get("notes") or ""),
                    created_at=created if isinstance(created, datetime) else None,
                )
            )
        entries.sort(key=lambda x: (x.created_at or datetime.min.replace(tzinfo=None)).replace(tzinfo=None))
        days.append(CardLogDay(day=day, entries=entries))
    days.sort(key=lambda d: d.day or date.min)
    return CardLogEvent(
        id=_oid_str(doc.get("_id")),
        title=str(doc.get("title") or "").strip(),
        client=str(general.get("client") or "").strip(),
        company=str(general.get("company") or "").strip(),
        start=_parse_date(general.get("start")),
        end=_parse_date(general.get("end")),
        archived=bool(doc.get("archived") or False),
        days=days,
    )


def backed_up_update(entry_id: Any, slot: int, value: bool = True) -> tuple[dict, list[dict]]:
    """Return (update_doc, array_filters) that flip ``card{slot}BackedUp`` on one entry."""
    field_name = "card1BackedUp" if slot == 1 else "card2BackedUp"
    update = {
        "$set": {
            f"cardLog.$[].entries.$[e].{field_name}": value,
            "cardLog.$[].entries.$[e].updatedAt": datetime.now(timezone.utc),
        }
    }
    return update, [{"e._id": entry_id}]


EVENT_PROJECTION = {
    "title": 1,
    "archived": 1,
    "general.client": 1,
    "general.company": 1,
    "general.start": 1,
    "general.end": 1,
    "cardLog": 1,
    "updatedAt": 1,
}


class CardLogError(Exception):
    pass


class CardLogClient:
    """Thin pymongo wrapper. All methods block; call them from a worker thread."""

    def __init__(self, uri: str, db_name: str = DEFAULT_DB, collection: str = DEFAULT_COLLECTION, timeout_ms: int = 8000) -> None:
        if not uri:
            raise CardLogError("MongoDB connection string is not configured (Settings > Card Log)")
        self.uri = uri
        self.db_name = db_name or DEFAULT_DB
        self.collection_name = collection or DEFAULT_COLLECTION
        self.timeout_ms = timeout_ms
        self._client = None

    # ---- connection -----------------------------------------------------------
    def _coll(self):
        if self._client is None:
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover
                raise CardLogError("pymongo is not installed") from exc
            self._client = MongoClient(
                self.uri,
                serverSelectionTimeoutMS=self.timeout_ms,
                connectTimeoutMS=self.timeout_ms,
                socketTimeoutMS=self.timeout_ms * 4,
                appname="LumDIT",
            )
        return self._client[self.db_name][self.collection_name]

    def test(self) -> str:
        try:
            coll = self._coll()
            self._client.admin.command("ping")
            n = coll.count_documents({"cardLog.0": {"$exists": True}})
            return f"Connected to {self.db_name}.{self.collection_name}: {n} event(s) with card logs"
        except CardLogError:
            raise
        except Exception as exc:
            raise CardLogError(f"Cannot reach MongoDB: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- reads -----------------------------------------------------------------
    def list_events(self, include_archived: bool = False, only_with_cardlog: bool = True) -> list[CardLogEvent]:
        query: dict[str, Any] = {}
        if not include_archived:
            query["archived"] = {"$ne": True}
        if only_with_cardlog:
            query["cardLog.0"] = {"$exists": True}
        try:
            cursor = self._coll().find(query, EVENT_PROJECTION).sort("general.start", -1)
            return [parse_event(d) for d in cursor]
        except Exception as exc:
            raise CardLogError(f"Could not load events: {exc}") from exc

    def get_event(self, event_id: str) -> CardLogEvent:
        from bson import ObjectId

        try:
            doc = self._coll().find_one({"_id": ObjectId(event_id)}, EVENT_PROJECTION)
        except Exception as exc:
            raise CardLogError(f"Could not load event: {exc}") from exc
        if doc is None:
            raise CardLogError("Event no longer exists in the card log database")
        return parse_event(doc)

    # ---- writes ----------------------------------------------------------------
    def mark_backed_up(self, event_id: str, entry_id: str, slot: int, value: bool = True) -> bool:
        from bson import ObjectId

        update, array_filters = backed_up_update(ObjectId(entry_id), slot, value)
        try:
            res = self._coll().update_one({"_id": ObjectId(event_id)}, update, array_filters=array_filters)
        except Exception as exc:
            raise CardLogError(f"Could not update card log: {exc}") from exc
        return res.matched_count > 0

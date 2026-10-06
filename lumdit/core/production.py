"""Production model, folder-structure generator and production.json persistence.

Hierarchy generated for each selected category::

    <root>/<Client Name>/<Production Name>/
        production.json
        <Category>/
            MM.DD.YYYY/            (one per day in start..end)
                <Camera - Operator (#Card)>/   (created at offload time)
            Project Files/
            Deliverables/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from lumdit import __version__
from lumdit.core.naming import (
    DEFAULT_CARD_FOLDER_TEMPLATE,
    format_card_folder,
    format_date_folder,
    parse_date_folder,
    sanitize_name,
)

PRESET_CATEGORIES: tuple[str, ...] = ("Photo", "Video", "Headshot Booth")
CATEGORY_SUBFOLDERS: tuple[str, ...] = ("Project Files", "Deliverables")
PRODUCTION_FILE = "production.json"
SCHEMA_VERSION = 1


class ProductionError(Exception):
    pass


@dataclass
class OffloadRecord:
    """History entry written after each offload (used for the duplicate-card guard)."""

    timestamp: str
    category: str
    date: str
    camera: str
    operator: str
    card: int | str
    destination: str
    source: str
    source_label: str
    fingerprint: str
    file_count: int
    total_bytes: int
    status: str
    # Link back to the crew app's card log (empty when offloaded manually).
    log_entry_id: str = ""
    log_slot: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OffloadRecord":
        kwargs = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        kwargs.setdefault("log_entry_id", "")
        kwargs.setdefault("log_slot", 0)
        return cls(**kwargs)  # type: ignore[arg-type]


@dataclass
class Production:
    name: str
    start_date: date
    end_date: date
    categories: list[str]
    root: Path  # the production folder itself: <destination_root>/<client>/<name>
    client: str = ""
    card_folder_template: str = DEFAULT_CARD_FOLDER_TEMPLATE
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    app_version: str = __version__
    # category -> {"camera": str, "operator": str, "card": int | str}
    last_card: dict[str, dict[str, Any]] = field(default_factory=dict)
    offloads: list[OffloadRecord] = field(default_factory=list)
    # Crew-app event this production was created from (empty for manual productions).
    event_id: str = ""
    event_title: str = ""
    # Card-log updates that could not reach MongoDB yet: [{"entry_id", "slot", "timestamp"}]
    pending_writebacks: list[dict[str, Any]] = field(default_factory=list)

    # ---- paths -----------------------------------------------------------------
    @property
    def file_path(self) -> Path:
        return self.root / PRODUCTION_FILE

    @property
    def display_name(self) -> str:
        return f"{self.client} - {self.name}" if self.client else self.name

    def date_range(self) -> list[date]:
        if self.end_date < self.start_date:
            raise ProductionError("End date is before start date")
        days = (self.end_date - self.start_date).days
        return [self.start_date + timedelta(days=i) for i in range(days + 1)]

    def category_dir(self, category: str) -> Path:
        return self.root / sanitize_name(category)

    def date_dir(self, category: str, day: date) -> Path:
        return self.category_dir(category) / format_date_folder(day)

    def card_dir(self, category: str, day: date, camera: str, operator: str, card: int | str) -> Path:
        return self.date_dir(category, day) / format_card_folder(
            camera, operator, card, self.card_folder_template
        )

    def existing_dates(self, category: str) -> list[date]:
        cat = self.category_dir(category)
        if not cat.is_dir():
            return []
        found = [parse_date_folder(p.name) for p in cat.iterdir() if p.is_dir()]
        return sorted(d for d in found if d is not None)

    # ---- structure ----------------------------------------------------------------
    def ensure_structure(self) -> list[Path]:
        """Create every folder in the hierarchy (idempotent). Returns created paths."""
        created: list[Path] = []
        self.root.mkdir(parents=True, exist_ok=True)
        for category in self.categories:
            for day in self.date_range():
                created.append(_mkdir(self.date_dir(category, day)))
            for sub in CATEGORY_SUBFOLDERS:
                created.append(_mkdir(self.category_dir(category) / sub))
        return created

    def add_category(self, category: str) -> None:
        category = sanitize_name(category)
        if category not in self.categories:
            self.categories.append(category)
        self.ensure_structure()
        self.save()

    # ---- card bookkeeping -----------------------------------------------------------
    def defaults_for(self, category: str) -> dict[str, Any]:
        """Pre-fill values for the offload dialog."""
        d = dict(self.last_card.get(category) or {})
        if not d:
            # Fall back to the most recent values from any category.
            for rec in reversed(self.offloads):
                d = {"camera": rec.camera, "operator": rec.operator, "card": rec.card}
                break
        card = d.get("card", 0)
        if not isinstance(card, int):
            card = int(card) if str(card).isdigit() else 0
        return {"camera": d.get("camera", ""), "operator": d.get("operator", ""), "card": card}

    def _matching_offloads(self, camera: str, operator: str) -> list[OffloadRecord]:
        return [
            r
            for r in self.offloads
            if r.camera.strip().lower() == camera.strip().lower()
            and r.operator.strip().lower() == operator.strip().lower()
        ]

    def last_card_number(self, camera: str, operator: str) -> int | str | None:
        """Card label used most recently for this camera/operator pair, if any.

        Card numbers are the labels printed on the physical cards, so they are
        chosen by the user rather than auto-incremented; this is only a hint.
        """
        matches = self._matching_offloads(camera, operator)
        return matches[-1].card if matches else None

    def card_history(self, camera: str, operator: str) -> list[int | str]:
        """Distinct card labels already offloaded for this camera/operator (newest first)."""
        seen: list[int | str] = []
        for r in reversed(self._matching_offloads(camera, operator)):
            if r.card not in seen:
                seen.append(r.card)
        return seen

    def offloads_for_card(self, camera: str, operator: str, card: int | str) -> list[OffloadRecord]:
        """Previous offloads of this physical card for this camera/operator."""
        return [r for r in self._matching_offloads(camera, operator) if str(r.card) == str(card)]

    def offloads_for_log_entry(self, entry_id: str, slot: int) -> list[OffloadRecord]:
        return [r for r in self.offloads if r.log_entry_id == entry_id and r.log_slot == slot]

    # ---- card-log write-back queue -------------------------------------------------
    def queue_writeback(self, entry_id: str, slot: int) -> None:
        if not any(w.get("entry_id") == entry_id and w.get("slot") == slot for w in self.pending_writebacks):
            self.pending_writebacks.append(
                {"entry_id": entry_id, "slot": slot, "timestamp": datetime.now().isoformat(timespec="seconds")}
            )
        self.save()

    def clear_writeback(self, entry_id: str, slot: int) -> None:
        before = len(self.pending_writebacks)
        self.pending_writebacks = [
            w for w in self.pending_writebacks if not (w.get("entry_id") == entry_id and w.get("slot") == slot)
        ]
        if len(self.pending_writebacks) != before:
            self.save()

    def record_offload(self, record: OffloadRecord) -> None:
        self.offloads.append(record)
        self.last_card[record.category] = {
            "camera": record.camera,
            "operator": record.operator,
            "card": record.card,
        }
        self.save()

    def find_duplicate(self, fingerprint: str) -> OffloadRecord | None:
        for rec in self.offloads:
            if rec.fingerprint == fingerprint and rec.status == "verified":
                return rec
        return None

    # ---- persistence ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "client": self.client,
            "name": self.name,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "categories": list(self.categories),
            "card_folder_template": self.card_folder_template,
            "created": self.created,
            "app_version": self.app_version,
            "last_card": self.last_card,
            "offloads": [asdict(o) for o in self.offloads],
            "event_id": self.event_id,
            "event_title": self.event_title,
            "pending_writebacks": list(self.pending_writebacks),
        }

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.file_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.file_path)

    @classmethod
    def load(cls, path: Path | str) -> "Production":
        path = Path(path)
        if path.is_dir():
            path = path / PRODUCTION_FILE
        if not path.is_file():
            raise ProductionError(f"No {PRODUCTION_FILE} found at {path.parent}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            start_date=date.fromisoformat(data["start_date"]),
            end_date=date.fromisoformat(data["end_date"]),
            categories=list(data.get("categories", [])),
            root=path.parent,
            client=data.get("client", ""),
            card_folder_template=data.get("card_folder_template", DEFAULT_CARD_FOLDER_TEMPLATE),
            created=data.get("created", ""),
            app_version=data.get("app_version", ""),
            last_card=dict(data.get("last_card", {})),
            offloads=[OffloadRecord.from_dict(o) for o in data.get("offloads", [])],
            event_id=str(data.get("event_id") or ""),
            event_title=str(data.get("event_title") or ""),
            pending_writebacks=list(data.get("pending_writebacks") or []),
        )


def create_production(
    destination_root: Path | str,
    client: str,
    name: str,
    start_date: date,
    end_date: date,
    categories: Iterable[str],
    card_folder_template: str = DEFAULT_CARD_FOLDER_TEMPLATE,
) -> Production:
    """Create ``<destination_root>/<client>/<name>`` with the folder hierarchy and production.json."""
    if not client.strip():
        raise ProductionError("Client name is required")
    if not name.strip():
        raise ProductionError("Production name is required")
    clean_client = sanitize_name(client)
    clean_name = sanitize_name(name)
    if end_date < start_date:
        raise ProductionError("End date must be on or after the start date")
    cats = []
    for c in categories:
        c = sanitize_name(c)
        if c and c not in cats:
            cats.append(c)
    if not cats:
        raise ProductionError("Select at least one folder (Photo, Video, ...)")

    root = Path(destination_root) / clean_client / clean_name
    if (root / PRODUCTION_FILE).exists():
        raise ProductionError(f"A production already exists at {root}")

    prod = Production(
        name=clean_name,
        start_date=start_date,
        end_date=end_date,
        categories=cats,
        root=root,
        client=clean_client,
        card_folder_template=card_folder_template,
    )
    prod.ensure_structure()
    prod.save()
    return prod


def find_or_create_for_event(
    destination_root: Path | str,
    event_id: str,
    event_title: str,
    client: str,
    name: str,
    start_date: date,
    end_date: date,
    categories: Iterable[str],
    card_folder_template: str = DEFAULT_CARD_FOLDER_TEMPLATE,
) -> Production:
    """Open the production for a crew-app event, creating it on first use.

    If ``<root>/<client>/<name>/production.json`` already exists it is loaded and
    linked to the event (and any missing categories/dates are added); otherwise
    a new production is created with the three presets plus whatever categories
    the card log uses.
    """
    cats = list(PRESET_CATEGORIES)
    for c in categories:
        c = sanitize_name(c)
        if c and c not in cats:
            cats.append(c)
    root = Path(destination_root) / sanitize_name(client) / sanitize_name(name)
    if (root / PRODUCTION_FILE).is_file():
        prod = Production.load(root)
        changed = False
        if prod.event_id != event_id:
            prod.event_id, prod.event_title, changed = event_id, event_title, True
        for c in cats:
            if c not in prod.categories:
                prod.categories.append(c)
                changed = True
        if start_date < prod.start_date:
            prod.start_date, changed = start_date, True
        if end_date > prod.end_date:
            prod.end_date, changed = end_date, True
        prod.ensure_structure()
        if changed:
            prod.save()
        return prod
    prod = create_production(destination_root, client, name, start_date, end_date, cats, card_folder_template)
    prod.event_id, prod.event_title = event_id, event_title
    prod.save()
    return prod


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

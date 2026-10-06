"""Typed wrapper around QSettings for app preferences."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from lumdit import APP_NAME, ORG_NAME
from lumdit.core.naming import DEFAULT_CARD_FOLDER_TEMPLATE

MAX_RECENT = 10


def _load_dotenv() -> None:
    """Load a developer ``.env`` (lumdit/.env, project root, or next to the frozen exe)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    candidates = [Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).parent / ".env")
    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)


_load_dotenv()


class Settings:
    def __init__(self) -> None:
        self._s = QSettings(ORG_NAME, APP_NAME)

    # ---- generic ----------------------------------------------------------
    def value(self, key: str, default=None, type_=None):
        if type_ is not None:
            return self._s.value(key, default, type=type_)
        return self._s.value(key, default)

    def set(self, key: str, value) -> None:
        self._s.setValue(key, value)
        self._s.sync()

    # ---- typed ------------------------------------------------------------
    @property
    def card_folder_template(self) -> str:
        return self.value("naming/card_folder_template", DEFAULT_CARD_FOLDER_TEMPLATE, str)

    @card_folder_template.setter
    def card_folder_template(self, v: str) -> None:
        self.set("naming/card_folder_template", v)

    @property
    def max_concurrent_jobs(self) -> int:
        return int(self.value("jobs/max_concurrent", 2, int))

    @max_concurrent_jobs.setter
    def max_concurrent_jobs(self, v: int) -> None:
        self.set("jobs/max_concurrent", int(v))

    @property
    def thumbnail_size(self) -> int:
        return int(self.value("browser/thumbnail_size", 160, int))

    @thumbnail_size.setter
    def thumbnail_size(self, v: int) -> None:
        self.set("browser/thumbnail_size", int(v))

    @property
    def default_destination_root(self) -> str:
        return self.value(
            "production/default_root",
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
            or str(Path.home()),
            str,
        )

    @default_destination_root.setter
    def default_destination_root(self, v: str) -> None:
        self.set("production/default_root", v)

    @property
    def show_hidden_files(self) -> bool:
        return bool(self.value("browser/show_hidden", False, bool))

    @show_hidden_files.setter
    def show_hidden_files(self, v: bool) -> None:
        self.set("browser/show_hidden", bool(v))

    @property
    def eject_after_offload(self) -> bool:
        return bool(self.value("jobs/eject_after_offload", False, bool))

    @eject_after_offload.setter
    def eject_after_offload(self, v: bool) -> None:
        self.set("jobs/eject_after_offload", bool(v))

    # ---- card log (MongoDB) -------------------------------------------------
    @property
    def mongo_uri(self) -> str:
        """Connection string from Settings, falling back to the MONGO_URI env var / .env."""
        stored = self.value("cardlog/mongo_uri", "", str)
        return stored or os.environ.get("MONGO_URI", "")

    @mongo_uri.setter
    def mongo_uri(self, v: str) -> None:
        self.set("cardlog/mongo_uri", v.strip())

    @property
    def mongo_uri_from_env(self) -> bool:
        return not self.value("cardlog/mongo_uri", "", str) and bool(os.environ.get("MONGO_URI"))

    @property
    def mongo_db(self) -> str:
        return self.value("cardlog/mongo_db", os.environ.get("MONGO_DB", "test"), str) or "test"

    @mongo_db.setter
    def mongo_db(self, v: str) -> None:
        self.set("cardlog/mongo_db", v.strip())

    @property
    def mongo_collection(self) -> str:
        return self.value("cardlog/mongo_collection", os.environ.get("MONGO_COLLECTION", "tables"), str) or "tables"

    @mongo_collection.setter
    def mongo_collection(self, v: str) -> None:
        self.set("cardlog/mongo_collection", v.strip())

    @property
    def cardlog_refresh_seconds(self) -> int:
        return int(self.value("cardlog/refresh_seconds", 60, int))

    # ---- recents ----------------------------------------------------------
    @property
    def recent_productions(self) -> list[str]:
        v = self.value("production/recent", [])
        if isinstance(v, str):
            v = [v]
        return [p for p in (v or []) if Path(p).is_dir()]

    def add_recent_production(self, root: Path | str) -> None:
        root = str(root)
        items = [p for p in self.recent_productions if p != root]
        items.insert(0, root)
        self.set("production/recent", items[:MAX_RECENT])

    @property
    def last_production(self) -> str | None:
        rec = self.recent_productions
        return rec[0] if rec else None

    # ---- history for combo boxes --------------------------------------------
    def history(self, key: str) -> list[str]:
        v = self.value(f"history/{key}", [])
        if isinstance(v, str):
            v = [v]
        return list(v or [])

    def add_history(self, key: str, value: str, limit: int = 20) -> None:
        value = value.strip()
        if not value:
            return
        items = [x for x in self.history(key) if x.lower() != value.lower()]
        items.insert(0, value)
        self.set(f"history/{key}", items[:limit])

    # ---- window -----------------------------------------------------------
    def save_geometry(self, name: str, data: bytes) -> None:
        self.set(f"window/{name}", data)

    def geometry(self, name: str):
        return self.value(f"window/{name}")


def cache_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    p = Path(base or Path.home() / ".lumdit-cache") / "thumbnails"
    p.mkdir(parents=True, exist_ok=True)
    return p

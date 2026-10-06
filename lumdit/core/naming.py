"""Folder naming helpers: templates, date folders and cross-platform sanitising.

Names must be safe on Windows (NTFS), macOS (APFS/HFS+) and exFAT (SD cards and
shuttle drives), so we apply the union of all three rule sets.
"""

from __future__ import annotations

import re
from datetime import date, datetime

DEFAULT_CARD_FOLDER_TEMPLATE = "{camera} - {operator} ({card})"
LEGACY_CARD_FOLDER_TEMPLATE = "{camera} - {operator} (#{card})"
DATE_FOLDER_FORMAT = "%m.%d.%Y"  # MM.DD.YYYY

# Characters illegal on at least one of NTFS / exFAT / HFS+ (plus control chars).
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_DATE_FOLDER_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def sanitize_name(name: str, replacement: str = "_") -> str:
    """Return a version of *name* that is a legal single path component everywhere."""
    cleaned = _ILLEGAL_CHARS.sub(replacement, name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Windows silently strips trailing dots/spaces; exFAT rejects them.
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "Untitled"
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_WINDOWS:
        cleaned = f"{replacement}{cleaned}"
    # Keep well under the 255-byte component limit; leave room for suffixes.
    return cleaned[:200]


def format_card_number(card: int | str) -> str:
    """Card label for folder names: ``12`` -> ``#12``; ``"Internal"`` stays as text."""
    if isinstance(card, bool):
        card = int(card)
    if isinstance(card, int):
        return f"#{card}"
    text = str(card).strip()
    if text.isdigit():
        return f"#{int(text)}"
    return text


def format_card_folder(
    camera: str,
    operator: str,
    card: int | str,
    template: str = DEFAULT_CARD_FOLDER_TEMPLATE,
) -> str:
    """Render the per-card folder name, e.g. ``FX3 - Germaine (#12)`` or ``Pocket4 - Chris (Internal)``.

    *operator* is normally the shooter's first name. *card* is the number
    printed on the physical card (int) or a text label such as ``Internal``.
    """
    if template == LEGACY_CARD_FOLDER_TEMPLATE:
        template = DEFAULT_CARD_FOLDER_TEMPLATE
    card_str = format_card_number(card)
    try:
        rendered = template.format(
            camera=camera.strip(),
            operator=operator.strip(),
            card=card_str,
        )
    except (KeyError, IndexError, ValueError):
        rendered = DEFAULT_CARD_FOLDER_TEMPLATE.format(
            camera=camera.strip(), operator=operator.strip(), card=card_str
        )
    return sanitize_name(rendered)


def format_date_folder(day: date) -> str:
    return day.strftime(DATE_FOLDER_FORMAT)


def parse_date_folder(name: str) -> date | None:
    m = _DATE_FOLDER_RE.match(name.strip())
    if not m:
        return None
    try:
        return datetime.strptime(name.strip(), DATE_FOLDER_FORMAT).date()
    except ValueError:
        return None


def validate_template(template: str) -> bool:
    """True if the template renders without error using the supported fields."""
    try:
        template.format(camera="A", operator="B", card="#1")
        return True
    except (KeyError, IndexError, ValueError):
        return False

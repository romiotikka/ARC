from __future__ import annotations

import re
import unicodedata


ARC_POSITIONS = frozenset({"G", "F", "C", "G-F", "F-C"})

_POSITION_ALIASES = {
    "G": "G",
    "F": "F",
    "PG": "G",
    "SG": "G",
    "SF": "F",
    "PF": "F",
    "C": "C",
    "1": "G",
    "2": "G",
    "3": "F",
    "4": "F",
    "5": "C",
    "G+F": "G-F",
    "G-F": "G-F",
    "F+C": "F-C",
    "F-C": "F-C",
}


def normalize_alias(value: str) -> str:
    """Return a stable comparison key for a player name or alias."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "", without_marks.lower())


def normalize_position(value: str | None) -> str | None:
    """Convert documented provider position forms to ARC internal positions."""

    if value is None:
        return None

    normalized = value.strip().upper().replace(" ", "")
    return _POSITION_ALIASES.get(normalized)

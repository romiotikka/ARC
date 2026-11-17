from __future__ import annotations

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
    normalized: list[str] = []
    latin_base = False
    for character in decomposed.casefold():
        if unicodedata.category(character).startswith("M"):
            if not latin_base:
                normalized.append(character)
        elif character.isalnum():
            normalized.append(character)
            latin_base = unicodedata.name(character, "").startswith("LATIN ")
        else:
            latin_base = False
    return "".join(normalized)


def normalize_position(value: str | None) -> str | None:
    """Convert documented provider position forms to ARC internal positions."""

    if value is None:
        return None

    normalized = value.strip().upper().replace(" ", "")
    return _POSITION_ALIASES.get(normalized)

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..models import PlayerCandidate

_LOGGER = logging.getLogger(__name__)
_CACHE_VERSION = 1
_STATUS_TTL = {
    "success": 7 * 24 * 60 * 60,
    "empty": 24 * 60 * 60,
    "unsupported": 24 * 60 * 60,
    "error": 5 * 60,
}


@dataclass(frozen=True, slots=True)
class CachedRoster:
    status: str
    roster: tuple[PlayerCandidate, ...] = ()
    error: str | None = None


class RosterCache:
    """File-backed roster cache shared by per-game resolver bridge processes."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or _default_cache_dir()

    def get(
        self,
        provider: str,
        league_id: str,
        season_id: str,
        team_id: str,
    ) -> CachedRoster | None:
        path = self._path(provider, league_id, season_id, team_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != _CACHE_VERSION:
                return None
            status = payload.get("status")
            ttl = _STATUS_TTL.get(status)
            if ttl is None or time.time() - float(payload["stored_at"]) > ttl:
                return None
            roster = tuple(
                _candidate_from_dict(candidate)
                for candidate in payload.get("roster", [])
            )
            return CachedRoster(status, roster, payload.get("error"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _LOGGER.warning("Ignoring invalid provider roster cache %s: %s", path, exc)
            return None

    def put(
        self,
        provider: str,
        league_id: str,
        season_id: str,
        team_id: str,
        status: str,
        roster: tuple[PlayerCandidate, ...] = (),
        error: str | None = None,
    ) -> None:
        if status not in _STATUS_TTL:
            raise ValueError(f"Unsupported roster cache status: {status}")
        path = self._path(provider, league_id, season_id, team_id)
        payload = {
            "version": _CACHE_VERSION,
            "stored_at": time.time(),
            "status": status,
            "error": error,
            "roster": [asdict(candidate) for candidate in roster],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{path.stem}_", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=True)
                os.replace(temporary_name, path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        except OSError as exc:
            _LOGGER.warning("Could not write provider roster cache %s: %s", path, exc)

    def _path(self, provider: str, league_id: str, season_id: str, team_id: str) -> Path:
        key = json.dumps(
            [provider, league_id, season_id, team_id],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"


def _candidate_from_dict(payload: dict[str, object]) -> PlayerCandidate:
    values = dict(payload)
    values["aliases"] = tuple(values.get("aliases", ()))
    return PlayerCandidate(**values)


def _default_cache_dir() -> Path:
    configured = os.getenv("ARC_IDENTITY_ROSTER_CACHE_DIR")
    if configured:
        return Path(configured)
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_CACHE_HOME")
    return Path(base) / "ARC" / "identity-rosters" if base else Path.home() / ".cache" / "arc" / "identity-rosters"
from __future__ import annotations

from abc import ABC, abstractmethod

from ..exceptions import ProviderNotImplementedError
from ..models import PlayerCandidate


class IdentityProvider(ABC):
    """Contract for all external identity data providers.

    A provider is a pure adapter: it accepts provider-native external identifiers,
    performs I/O, and returns normalized PlayerCandidate objects.

    Providers must never receive or interpret ARC-internal IDs.
    The resolver maps ARC IDs to provider-native IDs using player_external_ids,
    team_external_ids, league_external_ids, and season_external_ids before
    calling any provider method. Providers never receive ARC IDs.
    """

    provider_name: str

    @abstractmethod
    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        """Return all players on a team's roster for the given season.

        external_team_id: provider-native team ID (as stored in team_external_ids)
        external_season:  provider-native season ID (as stored in season_external_ids)
        """
        raise NotImplementedError

    def search_players(self, search: str) -> tuple[PlayerCandidate, ...]:
        """Search for players by name. Not all providers support this.

        Providers that support name search override this method.
        """
        raise ProviderNotImplementedError(
            f"{type(self).__name__} does not support player name search"
        )

from __future__ import annotations

from ..exceptions import ProviderNotImplementedError
from ..models import PlayerCandidate
from .base import IdentityProvider


class BasketProvider(IdentityProvider):
    """Local-provider adapter for Basket.ee and comparable domestic sources."""

    provider_name = "basket"

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        """Return roster candidates once Basket.ee integration is implemented."""
        raise ProviderNotImplementedError("Basket provider is not implemented yet")

"""Identity provider interfaces and provider-specific adapters."""

# Keep the provider contract importable for the offline LiveStats importer.
# Concrete adapters may have optional HTTP/configuration dependencies.
from .base import IdentityProvider

__all__ = ["ApiSportsProvider", "BasketProvider", "IdentityProvider"]


def __getattr__(name: str):
    if name == "ApiSportsProvider":
        from .api_sports import ApiSportsProvider

        return ApiSportsProvider
    if name == "BasketProvider":
        from .basket import BasketProvider

        return BasketProvider
    raise AttributeError(name)

class IdentityError(Exception):
    """Base exception for Identity Layer failures."""


class IdentityResolutionError(IdentityError):
    """Raised when an identity cannot be resolved safely."""


class ManualReviewRequired(IdentityResolutionError):
    """Raised when available evidence is insufficient for a safe match."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ProviderError(IdentityError):
    """Base exception for identity-provider failures."""


class ProviderNotImplementedError(ProviderError, NotImplementedError):
    """Raised by provider stubs before their integration is implemented."""

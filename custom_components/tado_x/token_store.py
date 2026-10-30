"""Token persistence in the flat keys of the config entry data."""

from typing import override

from homeassistant.core import HomeAssistant

from .client import TokenSet, TokenStore
from .runtime import TadoXConfigEntry


class ConfigEntryTokenStore(TokenStore):
    """Keep the tokens in access_token, refresh_token and token_expiry of entry.data.

    The three keys are always written together; token_expiry is aware UTC
    ISO 8601, and a naive or unreadable expiry loads as expired.
    """

    def __init__(self, hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
        """Initialize for one config entry."""
        raise NotImplementedError

    @override
    def load(self) -> TokenSet | None:
        """Return the tokens currently stored in the entry."""
        raise NotImplementedError

    @override
    async def async_save(self, tokens: TokenSet) -> None:
        """Write the tokens into the entry data in one update."""
        raise NotImplementedError

"""Token persistence in the flat keys of the config entry data."""

from datetime import UTC, datetime
from typing import Final, override

from homeassistant.core import HomeAssistant

from .client import TokenSet, TokenStore
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRY, LOGGER
from .runtime import TadoXConfigEntry

EXPIRED: Final = datetime(1970, 1, 1, tzinfo=UTC)


def parse_expiry(raw: object) -> datetime:
    """Return an aware expiry; a naive, missing or unreadable value is expired.

    1.8.x wrote naive local times. The host time zone is never guessed: the
    token is refreshed once instead.
    """
    if not isinstance(raw, str):
        return EXPIRED
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return EXPIRED
    return parsed if parsed.tzinfo is not None else EXPIRED


class ConfigEntryTokenStore(TokenStore):
    """Keep the tokens in access_token, refresh_token and token_expiry of entry.data.

    The three keys are always written together; token_expiry is aware UTC
    ISO 8601, and a naive or unreadable expiry loads as expired. Only the
    expiry is ever logged, never a token.
    """

    def __init__(self, hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
        """Initialize for one config entry."""
        self._hass = hass
        self._entry = entry

    @override
    def load(self) -> TokenSet | None:
        """Return the tokens currently stored in the entry."""
        data = self._entry.data
        access = data.get(CONF_ACCESS_TOKEN)
        refresh = data.get(CONF_REFRESH_TOKEN)
        if not isinstance(access, str) or not isinstance(refresh, str) or not refresh:
            LOGGER.debug("The config entry of %s holds no tokens", self._entry.title)
            return None
        expires_at = parse_expiry(data.get(CONF_TOKEN_EXPIRY))
        if expires_at is EXPIRED:
            LOGGER.debug(
                "The token expiry of %s is missing, naive or unreadable; "
                "the access token is refreshed first",
                self._entry.title,
            )
        return TokenSet(
            access_token=access, refresh_token=refresh, expires_at=expires_at
        )

    @override
    async def async_save(self, tokens: TokenSet) -> None:
        """Write the tokens into the entry data in one update."""
        LOGGER.debug(
            "Writing the rotated tado tokens of %s into its config entry; "
            "the access token expires at %s",
            self._entry.title,
            tokens.expires_at.isoformat(),
        )
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_TOKEN_EXPIRY: tokens.expires_at.astimezone(UTC).isoformat(),
            },
        )

"""OAuth tokens for the tado API: refresh with rotation, and device-code login."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final, Protocol, override

from aiohttp import ClientSession

from .models import DeviceAuthorization, TokenSet, utcnow


class TokenStore(Protocol):
    """Persistence of one grant's tokens."""

    def load(self) -> TokenSet | None:
        """Return the latest persisted tokens; cheap and synchronous."""
        ...

    async def async_save(self, tokens: TokenSet) -> None:
        """Persist rotated tokens before anyone uses them."""
        ...


class MemoryTokenStore(TokenStore):
    """Token store that keeps the tokens in memory only."""

    def __init__(self, tokens: TokenSet | None = None) -> None:
        """Initialize with optional tokens."""
        raise NotImplementedError

    @override
    def load(self) -> TokenSet | None:
        """Return the stored tokens."""
        raise NotImplementedError

    @override
    async def async_save(self, tokens: TokenSet) -> None:
        """Store the tokens."""
        raise NotImplementedError


class TokenManager:
    """Single-flight access token refresh for one grant.

    A rotated refresh token is persisted before the new access token is
    used. Transient refresh failures back off and never count as auth
    failures; an auth failure is sticky until the entry reloads.
    """

    BACKOFF: Final = (5, 15, 60, 300)

    def __init__(
        self,
        session: ClientSession,
        store: TokenStore,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Initialize from the persisted tokens; nothing is refreshed yet."""
        raise NotImplementedError

    @property
    def auth_failed(self) -> bool:
        """Return True after a definitive auth failure."""
        raise NotImplementedError

    @property
    def expires_at(self) -> datetime | None:
        """Return the expiry of the current access token."""
        raise NotImplementedError

    async def async_get_access_token(self) -> str:
        """Return a fresh access token, refreshing it when needed."""
        raise NotImplementedError

    async def async_handle_unauthorized(self, rejected_access_token: str) -> str:
        """Return a new access token after the API rejected the given one."""
        raise NotImplementedError

    async def async_close(self, timeout: float = 20.0) -> None:
        """Wait for an in-flight refresh, then refuse new ones."""
        raise NotImplementedError


class DeviceCodeFlow:
    """The OAuth device-code login of login.tado.com."""

    def __init__(
        self,
        session: ClientSession,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initialize with an injectable clock and sleep."""
        raise NotImplementedError

    async def async_start(self) -> DeviceAuthorization:
        """Request a device code and the verification URL to show."""
        raise NotImplementedError

    async def async_wait_for_token(self, auth: DeviceAuthorization) -> TokenSet:
        """Poll until the user confirmed the login, it was denied or it expired."""
        raise NotImplementedError

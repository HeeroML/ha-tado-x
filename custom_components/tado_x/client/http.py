"""HTTP transport of the tado X client: gate, bearer, headers, retry and errors."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from aiohttp import ClientSession
from yarl import URL

from .auth import TokenManager
from .const import REQUEST_TIMEOUT
from .models import utcnow
from .ratelimit import Priority, QuotaTracker

type PayloadObserver = Callable[[int, str, Any, datetime], None]


class Transport:
    """Send authenticated requests and map every outcome to the error taxonomy.

    The quota gate is checked first, rate limit headers are observed before
    any status handling, a 401 is retried once with a refreshed token, and
    any 2xx is a success.
    """

    def __init__(
        self,
        session: ClientSession,
        tokens: TokenManager,
        quota: QuotaTracker,
        *,
        clock: Callable[[], datetime] = utcnow,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Initialize with the shared session, the grant and the quota tracker."""
        raise NotImplementedError

    def set_payload_observer(self, cb: PayloadObserver | None) -> None:
        """Set the callback that receives (home_id, key, raw, at) of cached reads."""
        raise NotImplementedError

    async def request(
        self,
        method: str,
        url: str | URL,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        priority: Priority = Priority.POLL,
        cache: tuple[int, str] | None = None,
    ) -> Any:
        """Send one request and return the parsed JSON body, or None if empty."""
        raise NotImplementedError

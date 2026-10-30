"""HTTP transport of the tado X client: gate, bearer, headers, retry and errors."""

from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPStatus
import json
import logging
import time
from typing import Any, Final

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .auth import TokenManager
from .const import CONNECT_TIMEOUT, HOPS_API_URL, REQUEST_TIMEOUT
from .errors import (
    TadoXAuthError,
    TadoXConnectionError,
    TadoXError,
    TadoXForbiddenError,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
)
from .models import utcnow
from .ratelimit import Priority, QuotaTracker, parse_rate_limit

_LOGGER = logging.getLogger(__package__)

_HOPS_HOST: Final = URL(HOPS_API_URL).host
_NOT_X_TITLE: Final = "Timezone of home"

type PayloadObserver = Callable[[int, str, Any, datetime], None]


def _decode(raw: bytes, endpoint: str) -> Any:
    """Parse a 2xx body; an empty body is None whatever the content type says."""
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as err:
        raise TadoXParseError(endpoint, "$") from err


def _first_error(raw: bytes) -> tuple[str | None, str | None]:
    """Return code and title of the first entry of an {"errors": [...]} body."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None, None
    errors = body.get("errors") if isinstance(body, dict) else None
    first = errors[0] if isinstance(errors, list) and errors else None
    if not isinstance(first, dict):
        return None, None
    code, title = first.get("code"), first.get("title")
    return (
        code if isinstance(code, str) else None,
        title if isinstance(title, str) else None,
    )


def _not_x_home_id(url: URL, title: str | None) -> int | None:
    """Return the home id of a hops 404 that means "this is no tado X home" (#40)."""
    if url.host != _HOPS_HOST or not (title or "").startswith(_NOT_X_TITLE):
        return None
    parts = url.parts
    if len(parts) > 2 and parts[1] == "homes" and parts[2].isdecimal():
        return int(parts[2])
    return None


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
        self._session = session
        self._tokens = tokens
        self._quota = quota
        self._clock = clock
        self._timeout = ClientTimeout(total=timeout, connect=CONNECT_TIMEOUT)
        self._observer: PayloadObserver | None = None

    def set_payload_observer(self, cb: PayloadObserver | None) -> None:
        """Set the callback that receives (home_id, key, raw, at) of cached reads."""
        self._observer = cb

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
        target = URL(url)
        self._quota.check(priority)
        token = await self._tokens.async_get_access_token()
        status, raw = await self._send(method, target, token, json, params)
        if status == HTTPStatus.UNAUTHORIZED:
            token = await self._tokens.async_handle_unauthorized(token)
            status, raw = await self._send(method, target, token, json, params)
            if status == HTTPStatus.UNAUTHORIZED:
                raise TadoXAuthError("unauthorized_after_refresh")
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            raise self._error(method, target, status, raw)
        data = _decode(raw, cache[1] if cache else target.path)
        if cache is not None and data is not None and self._observer is not None:
            self._observer(cache[0], cache[1], data, self._clock())
        return data

    async def _send(
        self,
        method: str,
        url: URL,
        token: str,
        body: Any,
        params: Mapping[str, str] | None,
    ) -> tuple[int, bytes]:
        """Send one attempt, count it and observe its rate limit headers."""
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        self._quota.record_call()
        started = time.monotonic()
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=body,
                params=params,
                timeout=self._timeout,
            ) as response:
                status = response.status
                response_headers = response.headers
                raw = await response.read()
        except (ClientError, TimeoutError) as err:
            raise TadoXConnectionError(f"cannot reach {url.host}") from err
        info = parse_rate_limit(response_headers, self._clock())
        self._quota.observe(info, status=status)
        _LOGGER.debug(
            "%s %s%s: HTTP %s, r=%s, %.2f s",
            method,
            url.host,
            url.path,
            status,
            None if info is None else info.remaining,
            time.monotonic() - started,
        )
        return status, raw

    def _error(self, method: str, url: URL, status: int, raw: bytes) -> TadoXError:
        """Map a non-2xx answer other than 401 to the error taxonomy."""
        _LOGGER.debug(
            "%s %s%s failed with HTTP %s: %.300s",
            method,
            url.host,
            url.path,
            status,
            raw.decode(errors="replace"),
        )
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            return TadoXRateLimitError(
                self._quota.latest, self._quota.blocked_until, local=False
            )
        if not HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR:
            return TadoXServerError(status)
        code, title = _first_error(raw)
        if status == HTTPStatus.NOT_FOUND:
            if (home_id := _not_x_home_id(url, title)) is not None:
                return TadoXHomeNotXError(home_id, code, title)
            return TadoXNotFoundError(status, code, title)
        if status == HTTPStatus.FORBIDDEN:
            return TadoXForbiddenError(status, code, title)
        return TadoXRequestError(status, code, title)

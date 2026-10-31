"""HTTP transport of the tado X client: gate, bearer, headers, retry and errors."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
import json
import time
from typing import Any, Final

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .auth import TokenManager
from .const import CONNECT_TIMEOUT, ENERGY_IQ_API_URL, HOPS_API_URL, REQUEST_TIMEOUT
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
from .models import RateLimitInfo, utcnow
from .ratelimit import Priority, QuotaTracker, parse_rate_limit
from .telemetry import (
    Exchange,
    Telemetry,
    describe_tado_error,
    endpoint_template,
    shape,
)

_HOPS_HOST: Final = URL(HOPS_API_URL).host
_ENERGY_IQ_HOST: Final = URL(ENERGY_IQ_API_URL).host
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


def _total_seconds(info: RateLimitInfo | None) -> int | None:
    if info is None or info.reset_in is None:
        return None
    return int(info.reset_in.total_seconds())


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _answer_error(status: int, err: TadoXError) -> str:
    """Return the Exchange.error text of an answer that raised err."""
    if isinstance(err, TadoXParseError):
        return "invalid JSON"
    if isinstance(err, TadoXRequestError) and (
        text := describe_tado_error(err.code, err.title)
    ):
        return text
    return f"HTTP {status}"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Answer:
    """The answer to one attempt, with its rate limit headers and timing."""

    at: datetime
    attempt: int
    status: int
    raw: bytes
    info: RateLimitInfo | None
    duration_ms: int


def _is_energy_iq_refusal(url: URL, data: Any) -> bool:
    """Return True for an Energy IQ 2xx that carries a message and no id (API §5.2)."""
    return (
        url.host == _ENERGY_IQ_HOST
        and isinstance(data, dict)
        and "message" in data
        and "id" not in data
    )


class Transport:
    """Send authenticated requests and map every outcome to the error taxonomy.

    The quota gate is checked first, rate limit headers are observed before
    any status handling, a 401 is retried once with a refreshed token, and
    any 2xx is a success, except an Energy IQ refusal. Every attempt is
    recorded in the telemetry, and every error raised is tagged with the
    endpoint template of its request unless it names one already.
    """

    def __init__(
        self,
        session: ClientSession,
        tokens: TokenManager,
        quota: QuotaTracker,
        *,
        clock: Callable[[], datetime] = utcnow,
        timeout: float = REQUEST_TIMEOUT,
        telemetry: Telemetry | None = None,
    ) -> None:
        """Initialize with the shared session, the grant and the quota tracker."""
        self._session = session
        self._tokens = tokens
        self._quota = quota
        self._clock = clock
        self._timeout = ClientTimeout(total=timeout, connect=CONNECT_TIMEOUT)
        self._telemetry = telemetry if telemetry is not None else Telemetry(clock=clock)
        self._observer: PayloadObserver | None = None

    @property
    def telemetry(self) -> Telemetry:
        """Return the telemetry the attempts are recorded in."""
        return self._telemetry

    def set_payload_observer(self, cb: PayloadObserver | None) -> None:
        """Set the callback that receives (home_id, key, raw, at) of cached reads."""
        self._observer = cb

    def publish(self, home_id: int, key: str, raw: Any) -> None:
        """Hand the raw payload of a cached read to the observer.

        Callers publish only after the endpoint parser accepted the payload,
        so a malformed answer never replaces what a restart starts from.
        """
        if self._observer is not None:
            self._observer(home_id, key, raw, self._clock())

    async def request(
        self,
        method: str,
        url: str | URL,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        priority: Priority = Priority.POLL,
        endpoint: str | None = None,
    ) -> Any:
        """Send one request and return the parsed JSON body, or None if empty.

        endpoint names the payload in a parse error; the URL path is the default.
        """
        data, _ = await self._exchange(
            method, URL(url), json, params, priority, endpoint
        )
        return data

    async def command(
        self, method: str, url: str | URL, *, json: Any = None
    ) -> Exchange:
        """Send a write with command priority and return its final exchange."""
        _, exchange = await self._exchange(
            method, URL(url), json, None, Priority.COMMAND, None
        )
        return exchange

    async def _exchange(
        self,
        method: str,
        url: URL,
        body: Any,
        params: Mapping[str, str] | None,
        priority: Priority,
        endpoint: str | None,
    ) -> tuple[Any, Exchange]:
        """Run a request; tag the error it raises with its endpoint template."""
        template = endpoint_template(
            method, url.update_query(params) if params else url
        )
        try:
            return await self._attempts(
                method, url, template, body, params, priority, endpoint or url.path
            )
        except TadoXError as err:
            if err.request is None:
                err.request = template
            raise

    async def _attempts(
        self,
        method: str,
        url: URL,
        template: str,
        body: Any,
        params: Mapping[str, str] | None,
        priority: Priority,
        endpoint: str,
    ) -> tuple[Any, Exchange]:
        """Pass the gate, send, retry once after a 401 and map the final answer."""
        self._quota.check(priority)
        token = await self._tokens.async_get_access_token()
        answer = await self._send(method, url, template, token, body, params, 1)
        if answer.status == HTTPStatus.UNAUTHORIZED:
            self._record(method, template, answer, outcome="retried", error="HTTP 401")
            token = await self._tokens.async_handle_unauthorized(token)
            answer = await self._send(method, url, template, token, body, params, 2)
        try:
            data = self._result(method, url, answer, endpoint)
        except TadoXError as err:
            self._record(
                method,
                template,
                answer,
                outcome=type(err).__name__,
                error=_answer_error(answer.status, err),
            )
            raise
        exchange = self._record(
            method,
            template,
            answer,
            shape=None if data is None else shape(data),
        )
        return data, exchange

    def _result(self, method: str, url: URL, answer: _Answer, endpoint: str) -> Any:
        """Return the parsed body of a 2xx answer; raise the error of any other."""
        status = answer.status
        if status == HTTPStatus.UNAUTHORIZED:
            raise TadoXAuthError("unauthorized_after_refresh")
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            raise self._error(method, url, status, answer.raw)
        data = _decode(answer.raw, endpoint)
        if _is_energy_iq_refusal(url, data):
            message = data["message"]
            raise TadoXRequestError(
                status, None, message if isinstance(message, str) else None
            )
        return data

    async def _send(
        self,
        method: str,
        url: URL,
        template: str,
        token: str,
        body: Any,
        params: Mapping[str, str] | None,
        attempt: int,
    ) -> _Answer:
        """Send one attempt, count it and observe its rate limit headers."""
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        ticket = self._quota.record_call()
        at = self._clock()
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
            self._telemetry.record_exchange(
                Exchange(
                    at=at,
                    method=method,
                    endpoint=template,
                    status=None,
                    duration_ms=_elapsed_ms(started),
                    attempt=attempt,
                    outcome=TadoXConnectionError.__name__,
                    error=type(err).__name__,
                )
            )
            raise TadoXConnectionError(f"cannot reach {url.host}") from err
        duration_ms = _elapsed_ms(started)
        info = parse_rate_limit(response_headers, self._clock())
        self._quota.observe(info, status=status, ticket=ticket)
        return _Answer(
            at=at,
            attempt=attempt,
            status=status,
            raw=raw,
            info=info,
            duration_ms=duration_ms,
        )

    def _record(
        self,
        method: str,
        template: str,
        answer: _Answer,
        *,
        outcome: str = "ok",
        error: str | None = None,
        shape: object | None = None,
    ) -> Exchange:
        """Record the exchange of an answer; never with its body."""
        info = answer.info
        exchange = Exchange(
            at=answer.at,
            method=method,
            endpoint=template,
            status=answer.status,
            duration_ms=answer.duration_ms,
            attempt=answer.attempt,
            limit=None if info is None else info.limit,
            remaining=None if info is None else info.remaining,
            reset_in_s=_total_seconds(info),
            outcome=outcome,
            shape=shape,
            error=error,
        )
        self._telemetry.record_exchange(exchange)
        return exchange

    def _error(self, method: str, url: URL, status: int, raw: bytes) -> TadoXError:
        """Map a non-2xx answer other than 401 to the error taxonomy."""
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

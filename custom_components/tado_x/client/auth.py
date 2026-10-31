"""OAuth tokens for the tado API: refresh with rotation, and device-code login."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import datetime, timedelta
from http import HTTPStatus
import json
import logging
import time
from typing import Any, Final, Protocol, override

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .const import (
    CLIENT_ID,
    CONNECT_TIMEOUT,
    DEVICE_AUTHORIZE_URL,
    DEVICE_FLOW_MIN_INTERVAL,
    DEVICE_FLOW_SLOW_DOWN_STEP,
    GRANT_TYPE_DEVICE_CODE,
    GRANT_TYPE_REFRESH_TOKEN,
    REQUEST_TIMEOUT,
    SCOPE,
    TOKEN_URL,
)
from .errors import (
    TadoXAuthError,
    TadoXConnectionError,
    TadoXDeviceFlowError,
    TadoXError,
    TadoXParseError,
    TadoXRequestError,
    TadoXServerError,
    TadoXTransientError,
)
from .models import DeviceAuthorization, TokenSet, utcnow
from .telemetry import Exchange, Telemetry, endpoint_template, error_details

_LOGGER = logging.getLogger(__package__)

_TOKEN_REQUEST: Final = endpoint_template("POST", URL(TOKEN_URL))
_LOGIN_TIMEOUT: Final = ClientTimeout(total=REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
# RFC 6749 5.2 errors that reject the grant or the client. Any other 4xx, for
# example a proxy or CDN page, leaves the refresh token valid.
_GRANT_REJECTED: Final = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_request"}
)

type RefreshTaskFactory = Callable[
    [Coroutine[Any, Any, TokenSet]], asyncio.Task[TokenSet]
]


async def _post_form(
    session: ClientSession, url: str, form: Mapping[str, str]
) -> tuple[int, dict[str, Any]]:
    """POST a form to login.tado.com; return the status and the JSON object body."""
    try:
        async with session.post(url, data=form, timeout=_LOGIN_TIMEOUT) as response:
            status = response.status
            raw = await response.read()
    except (ClientError, TimeoutError) as err:
        raise TadoXConnectionError(
            "cannot reach login.tado.com", cause=type(err).__name__
        ) from None
    try:
        body = json.loads(raw)
    except ValueError:
        return status, {}
    return status, body if isinstance(body, dict) else {}


def _is_success(status: int) -> bool:
    return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES


def _is_transient(status: int) -> bool:
    return (
        status == HTTPStatus.TOO_MANY_REQUESTS
        or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    )


def _oauth_error(body: Mapping[str, Any]) -> str | None:
    error = body.get("error")
    return error if isinstance(error, str) and error else None


def _reason(err: TadoXAuthError) -> str:
    """Return the reason of an auth error if it looks like an OAuth code."""
    return error_details(err)[1] or "unknown"


def _failure(err: TadoXError) -> str:
    """Return the class, the status and the safe message of a refresh failure."""
    status, message = error_details(err)
    parts = [type(err).__name__]
    if status is not None:
        parts.append(f"HTTP {status}")
    if message:
        parts.append(message)
    return ", ".join(parts)


def _login_error(status: int | None, err: TadoXError) -> str:
    """Return the Exchange.error text of a failed token request; never its body."""
    if isinstance(err, TadoXAuthError):
        return _reason(err)
    if isinstance(err, TadoXConnectionError):
        return err.cause or type(err).__name__
    if isinstance(err, TadoXParseError):
        return "invalid token response"
    return f"HTTP {status}"


def _retrieve_outcome(task: asyncio.Task[TokenSet]) -> None:
    """Mark the outcome of a refresh as seen; its caller may have been cancelled."""
    if not task.cancelled():
        task.exception()


class TokenStore(Protocol):
    """Persistence of one grant's tokens."""

    def load(self) -> TokenSet | None:
        """Return the latest persisted tokens; cheap and synchronous."""
        ...

    async def async_save(self, tokens: TokenSet) -> None:
        """Persist rotated tokens before anyone uses them.

        The tokens must be stored before the first await, so the load the
        manager does right before a save cannot be outdated by then.
        """
        ...


class MemoryTokenStore(TokenStore):
    """Token store that keeps the tokens in memory only."""

    def __init__(self, tokens: TokenSet | None = None) -> None:
        """Initialize with optional tokens."""
        self._tokens = tokens

    @override
    def load(self) -> TokenSet | None:
        """Return the stored tokens."""
        return self._tokens

    @override
    async def async_save(self, tokens: TokenSet) -> None:
        """Store the tokens."""
        self._tokens = tokens


class TokenManager:
    """Single-flight access token refresh for one grant.

    The refresh request and the persisting of its rotation run in one task the
    manager owns, so cancelling a caller never loses a rotation tado already
    made. A rotated refresh token is persisted before the new access token is
    used, unless another writer stored tokens after the refresh began; those
    win over its rotation and over its rejection. Transient refresh failures
    back off and never count as auth failures; an auth failure is sticky
    until the entry reloads.

    Every token request is recorded in the telemetry, and failed refreshes
    are recorded as errors in the context "token refresh".
    """

    BACKOFF: Final = (5, 15, 60, 300)

    def __init__(
        self,
        session: ClientSession,
        store: TokenStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        create_task: RefreshTaskFactory = asyncio.create_task,
        telemetry: Telemetry | None = None,
    ) -> None:
        """Initialize from the persisted tokens; nothing is refreshed yet.

        create_task starts the refresh task; a host passes its own factory to
        track the task, for example to wait for it on shutdown.
        """
        self._session = session
        self._store = store
        self._clock = clock
        self._create_task = create_task
        self._telemetry = telemetry if telemetry is not None else Telemetry(clock=clock)
        self._persisted = store.load()
        self._tokens = self._persisted
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[TokenSet] | None = None
        self._auth_error: TadoXAuthError | None = None
        self._closed = False
        self._failures = 0
        self._backoff: tuple[datetime, TadoXError] | None = None

    @property
    def auth_failed(self) -> bool:
        """Return True after a definitive auth failure."""
        return self._auth_error is not None

    @property
    def expires_at(self) -> datetime | None:
        """Return the expiry of the current access token."""
        return None if self._tokens is None else self._tokens.expires_at

    async def async_get_access_token(self) -> str:
        """Return a fresh access token, refreshing it when needed."""
        self._raise_if_auth_failed()
        tokens = self._tokens
        if tokens is not None and tokens.is_fresh(self._clock()):
            return tokens.access_token
        async with self._lock:
            self._raise_if_auth_failed()
            tokens = self._adopt_persisted()
            if tokens.is_fresh(self._clock()):
                return tokens.access_token
            return await self._refresh(tokens, force=False)

    async def async_handle_unauthorized(self, rejected_access_token: str) -> str:
        """Return a new access token after the API rejected the given one."""
        self._raise_if_auth_failed()
        async with self._lock:
            self._raise_if_auth_failed()
            tokens = self._adopt_persisted()
            if tokens.access_token != rejected_access_token and tokens.is_fresh(
                self._clock()
            ):
                _LOGGER.debug("tado rejected an access token that was replaced already")
                return tokens.access_token
            _LOGGER.debug("tado rejected the access token; refreshing it")
            return await self._refresh(tokens, force=True)

    async def async_close(self, timeout: float = 10.0) -> None:
        """Wait for an in-flight refresh, then refuse new ones.

        A still valid access token keeps being served until it expires.
        """
        self._closed = True
        task = self._refresh_task
        if task is None or task.done():
            return
        _, pending = await asyncio.wait((task,), timeout=timeout)
        if pending:
            _LOGGER.debug("Closed the tado client while a token refresh was running")

    def _raise_if_auth_failed(self) -> None:
        if (failure := self._auth_error) is not None:
            err = TadoXAuthError(failure.reason, status=failure.status)
            err.request = failure.request
            raise err

    def _adopt_persisted(self) -> TokenSet:
        """Return the current tokens, taking those another writer stored.

        Another writer is a reload, a reauth or a reconfigure. Comparing with
        the last persisted tokens, not the current ones, keeps a rotation whose
        save failed.
        """
        self._stored_since(self._persisted)
        if self._tokens is None:
            raise self._fail(TadoXAuthError("no_tokens"))
        return self._tokens

    def _stored_since(self, seen: TokenSet | None) -> TokenSet | None:
        """Adopt and return the stored tokens if another writer replaced seen."""
        stored = self._store.load()
        if stored is None or stored == seen:
            return None
        self._persisted = self._tokens = stored
        return stored

    async def _refresh(self, tokens: TokenSet, *, force: bool) -> str:
        """Join or start the refresh; on a transient failure use a still valid token."""
        if self._backoff is not None and self._clock() < self._backoff[0]:
            _LOGGER.debug("Not refreshing the tado access token during its backoff")
            return self._fallback(tokens, self._backoff[1], force=force)
        task = self._refresh_task
        if task is None or task.done():
            if self._closed:
                raise TadoXConnectionError("the tado client is closed")
            task = self._create_task(self._async_rotate(tokens, self._persisted))
            task.add_done_callback(_retrieve_outcome)
            self._refresh_task = task
        # Unlike shield(), wait() neither cancels the task with the caller nor
        # reports its exception to the loop once the caller is gone.
        await asyncio.wait((task,))
        try:
            new = task.result()
        except (TadoXTransientError, TadoXParseError) as err:
            return self._fallback(tokens, err, force=force)
        return new.access_token

    async def _async_rotate(self, tokens: TokenSet, seen: TokenSet | None) -> TokenSet:
        """Refresh the grant and persist the rotation, whether or not anyone waits.

        seen is the persisted tokens this manager knew when it decided to
        refresh. A caller may adopt newer tokens while the request runs, so
        comparing with the live _persisted would miss that the answer is
        outdated.
        """
        try:
            new = await self._post_refresh(tokens)
        except (TadoXTransientError, TadoXParseError) as err:
            self._note_failure(err)
            raise
        except TadoXAuthError as err:
            if (stored := self._stored_since(seen)) is not None:
                # Another writer rotated the grant while this request ran.
                return await self._async_use(stored)
            self._fail(err)
            raise
        if (stored := self._stored_since(seen)) is not None:
            # A reauth or reconfigure stored a new login while this request ran.
            return await self._async_use(stored)
        try:
            await self._store.async_save(new)
        except Exception:
            # tado revoked the old refresh token already; dropping the new one
            # would force a new login. The next refresh saves its successor.
            _LOGGER.exception("Storing the rotated tado tokens failed")
        else:
            self._persisted = new
            _LOGGER.debug(
                "Stored the rotated tado tokens; the access token expires at %s",
                new.expires_at.isoformat(),
            )
        self._tokens = new
        if self._failures:
            _LOGGER.info(
                "Refreshing the tado access token works again after %s failed attempts",
                self._failures,
            )
        self._failures = 0
        self._backoff = None
        return new

    async def _async_use(self, stored: TokenSet) -> TokenSet:
        """Return tokens another writer stored, refreshed first if they are stale."""
        if stored.is_fresh(self._clock()):
            return stored
        return await self._async_rotate(stored, stored)

    async def _post_refresh(self, tokens: TokenSet) -> TokenSet:
        """POST the refresh token, record the exchange and tag any error with it."""
        sent_at = self._clock()
        started = time.monotonic()
        status: int | None = None
        try:
            status, body = await _post_form(
                self._session,
                TOKEN_URL,
                {
                    "client_id": CLIENT_ID,
                    "grant_type": GRANT_TYPE_REFRESH_TOKEN,
                    "refresh_token": tokens.refresh_token,
                },
            )
            new = self._token_result(status, body, sent_at, tokens)
        except TadoXError as err:
            err.request = _TOKEN_REQUEST
            self._record(sent_at, started, status, err)
            raise
        self._record(sent_at, started, status, None)
        return new

    @staticmethod
    def _token_result(
        status: int, body: Mapping[str, Any], sent_at: datetime, tokens: TokenSet
    ) -> TokenSet:
        """Return the rotated tokens of a token answer, or raise its error."""
        if _is_success(status):
            return TokenSet.from_token_response(
                body, sent_at=sent_at, previous_refresh=tokens.refresh_token
            )
        error = _oauth_error(body)
        if error in _GRANT_REJECTED and not _is_transient(status):
            raise TadoXAuthError(error, status=status)
        raise TadoXServerError(status)

    def _record(
        self,
        sent_at: datetime,
        started: float,
        status: int | None,
        err: TadoXError | None,
    ) -> None:
        self._telemetry.record_exchange(
            Exchange(
                at=sent_at,
                method="POST",
                endpoint=_TOKEN_REQUEST,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
                outcome="ok" if err is None else type(err).__name__,
                error=None if err is None else _login_error(status, err),
            )
        )

    def _fallback(self, tokens: TokenSet, err: TadoXError, *, force: bool) -> str:
        if not force and self._clock() < tokens.expires_at:
            return tokens.access_token
        raise err.with_traceback(None)

    def _note_failure(self, err: TadoXError) -> None:
        delay = self.BACKOFF[min(self._failures, len(self.BACKOFF) - 1)]
        self._failures += 1
        self._backoff = (self._clock() + timedelta(seconds=delay), err)
        self._telemetry.record_tado_error(err, context="token refresh")
        _LOGGER.log(
            logging.INFO if self._failures == 1 else logging.DEBUG,
            "Refreshing the tado access token failed (%s), retrying in %s s",
            _failure(err),
            delay,
        )

    def _fail(self, err: TadoXAuthError) -> TadoXAuthError:
        self._auth_error = err
        self._telemetry.record_tado_error(err, context="token refresh")
        _LOGGER.info(
            "tado rejected the login (%s); a new login is needed", _reason(err)
        )
        return err


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
        self._session = session
        self._clock = clock
        self._sleep = sleep

    async def async_start(self) -> DeviceAuthorization:
        """Request a device code and the verification URL to show."""
        sent_at = self._clock()
        status, body = await _post_form(
            self._session,
            DEVICE_AUTHORIZE_URL,
            {"client_id": CLIENT_ID, "scope": SCOPE},
        )
        if _is_success(status):
            return DeviceAuthorization.from_api(body, received_at=sent_at)
        if _is_transient(status):
            raise TadoXServerError(status)
        description = body.get("error_description")
        raise TadoXRequestError(
            status,
            _oauth_error(body),
            description if isinstance(description, str) else None,
        )

    async def async_wait_for_token(self, auth: DeviceAuthorization) -> TokenSet:
        """Poll until the user confirmed the login, it was denied or it expired.

        Transient errors are retried until the code expires; the last one is
        raised then.
        """
        interval = max(auth.interval, DEVICE_FLOW_MIN_INTERVAL)
        form = {
            "client_id": CLIENT_ID,
            "device_code": auth.device_code,
            "grant_type": GRANT_TYPE_DEVICE_CODE,
        }
        last_error: TadoXTransientError | None = None
        while True:
            await self._sleep(interval.total_seconds())
            sent_at = self._clock()
            if sent_at >= auth.expires_at:
                raise last_error or TadoXDeviceFlowError("expired")
            try:
                status, body = await _post_form(self._session, TOKEN_URL, form)
            except TadoXConnectionError as err:
                last_error = err
                continue
            if _is_success(status):
                return TokenSet.from_token_response(
                    body, sent_at=sent_at, previous_refresh=None
                )
            if _is_transient(status):
                last_error = TadoXServerError(status)
                continue
            last_error = None
            match _oauth_error(body):
                case "authorization_pending":
                    pass
                case "slow_down":
                    interval += DEVICE_FLOW_SLOW_DOWN_STEP
                case "expired_token":
                    raise TadoXDeviceFlowError("expired")
                case "access_denied":
                    raise TadoXDeviceFlowError("denied")
                case error:
                    raise TadoXAuthError(error or f"http_{status}", status=status)

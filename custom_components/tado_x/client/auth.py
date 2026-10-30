"""OAuth tokens for the tado API: refresh with rotation, and device-code login."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import datetime, timedelta
from http import HTTPStatus
import json
import logging
from typing import Any, Final, Protocol, override

from aiohttp import ClientError, ClientSession, ClientTimeout

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

_LOGGER = logging.getLogger(__package__)

_LOGIN_TIMEOUT: Final = ClientTimeout(total=REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
_REJECTED: Final = frozenset(
    {HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
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
        raise TadoXConnectionError("cannot reach login.tado.com") from err
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
    used, unless another writer stored tokens meanwhile; those win. Transient
    refresh failures back off and never count as auth failures; an auth
    failure is sticky until the entry reloads.
    """

    BACKOFF: Final = (5, 15, 60, 300)

    def __init__(
        self,
        session: ClientSession,
        store: TokenStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        create_task: RefreshTaskFactory = asyncio.create_task,
    ) -> None:
        """Initialize from the persisted tokens; nothing is refreshed yet.

        create_task starts the refresh task; a host passes its own factory to
        track the task, for example to wait for it on shutdown.
        """
        self._session = session
        self._store = store
        self._clock = clock
        self._create_task = create_task
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
                return tokens.access_token
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
        if self._auth_error is not None:
            raise TadoXAuthError(self._auth_error.reason)

    def _adopt_persisted(self) -> TokenSet:
        """Return the current tokens, taking those another writer stored."""
        self._stored_elsewhere()
        if self._tokens is None:
            raise self._fail(TadoXAuthError("no_tokens"))
        return self._tokens

    def _stored_elsewhere(self) -> TokenSet | None:
        """Adopt and return tokens stored since this manager last read or wrote them.

        Another writer is a reload, a reauth or a reconfigure. Comparing with
        the last persisted tokens, not the current ones, keeps a rotation whose
        save failed.
        """
        stored = self._store.load()
        if stored is None or stored == self._persisted:
            return None
        self._persisted = self._tokens = stored
        return stored

    async def _refresh(self, tokens: TokenSet, *, force: bool) -> str:
        """Join or start the refresh; on a transient failure use a still valid token."""
        if self._backoff is not None and self._clock() < self._backoff[0]:
            return self._fallback(tokens, self._backoff[1], force=force)
        task = self._refresh_task
        if task is None or task.done():
            if self._closed:
                raise TadoXConnectionError("the tado client is closed")
            task = self._create_task(self._async_rotate(tokens))
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

    async def _async_rotate(self, tokens: TokenSet) -> TokenSet:
        """Refresh the grant and persist the rotation, whether or not anyone waits."""
        try:
            new = await self._post_refresh(tokens)
        except (TadoXTransientError, TadoXParseError) as err:
            self._note_failure(err)
            raise
        except TadoXAuthError as err:
            if (stored := self._stored_elsewhere()) is not None:
                # Another writer rotated the grant while this request ran.
                return await self._async_use(stored)
            self._fail(err)
            raise
        if (stored := self._stored_elsewhere()) is not None:
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
        self._tokens = new
        self._failures = 0
        self._backoff = None
        return new

    async def _async_use(self, stored: TokenSet) -> TokenSet:
        """Return tokens another writer stored, refreshed first if they are stale."""
        if stored.is_fresh(self._clock()):
            return stored
        return await self._async_rotate(stored)

    async def _post_refresh(self, tokens: TokenSet) -> TokenSet:
        sent_at = self._clock()
        status, body = await _post_form(
            self._session,
            TOKEN_URL,
            {
                "client_id": CLIENT_ID,
                "grant_type": GRANT_TYPE_REFRESH_TOKEN,
                "refresh_token": tokens.refresh_token,
            },
        )
        if _is_success(status):
            return TokenSet.from_token_response(
                body, sent_at=sent_at, previous_refresh=tokens.refresh_token
            )
        if status in _REJECTED:
            raise TadoXAuthError(_oauth_error(body) or f"http_{status}")
        raise TadoXServerError(status)

    def _fallback(self, tokens: TokenSet, err: TadoXError, *, force: bool) -> str:
        if not force and self._clock() < tokens.expires_at:
            return tokens.access_token
        raise err.with_traceback(None)

    def _note_failure(self, err: TadoXError) -> None:
        delay = self.BACKOFF[min(self._failures, len(self.BACKOFF) - 1)]
        self._failures += 1
        self._backoff = (self._clock() + timedelta(seconds=delay), err)
        if self._failures == 1:
            _LOGGER.warning(
                "Refreshing the tado access token failed, retrying in %s s: %s",
                delay,
                err,
            )

    def _fail(self, err: TadoXAuthError) -> TadoXAuthError:
        self._auth_error = err
        _LOGGER.warning(
            "tado rejected the login (%s); a new login is needed", err.reason
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
                    raise TadoXAuthError(error or f"http_{status}")

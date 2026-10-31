"""Tests for the single-flight token refresh of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, override
from unittest.mock import Mock

from aiohttp import (
    ClientConnectorError,
    ClientSession,
    ServerDisconnectedError,
    ServerTimeoutError,
)
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tado_x.client import (
    CLIENT_ID,
    MemoryTokenStore,
    QuotaTracker,
    TadoXAuthError,
    TadoXConnectionError,
    TadoXParseError,
    TadoXServerError,
    TadoXTransientError,
    Telemetry,
    TokenManager,
    TokenSet,
    Transport,
)
from custom_components.tado_x.client.const import HOPS_API_URL, TOKEN_URL
from tests.tado_mock import HOME_B, HOME_C, Call, TadoCloudMock, entry_tokens

ACCESS_B, REFRESH_B = entry_tokens(HOME_B)
ACCESS_C, REFRESH_C = entry_tokens(HOME_C)
ROOMS_B = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"
CLIENT_LOGGER = "custom_components.tado_x.client"
TOKEN_REQUEST = "POST login:/oauth2/token"


def _client_log(
    caplog: pytest.LogCaptureFixture, minimum: int = logging.DEBUG
) -> list[tuple[int, str]]:
    """Return level and message of the client's records from minimum up."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == CLIENT_LOGGER and record.levelno >= minimum
    ]


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


def _tokens(
    expires_in: float, access: str = ACCESS_B, refresh: str = REFRESH_B
) -> TokenSet:
    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


def _refreshes(mock_tado: TadoCloudMock) -> list[Call]:
    return mock_tado.calls("POST", TOKEN_URL)


async def _until(condition: Callable[[], bool]) -> None:
    """Let other tasks run until the condition holds."""
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


class RecordingStore(MemoryTokenStore):
    """Token store that calls a hook before it stores rotated tokens."""

    def __init__(self, tokens: TokenSet, hook: Callable[[TokenSet], None]) -> None:
        """Initialize with tokens and the hook."""
        super().__init__(tokens)
        self._hook = hook

    @override
    async def async_save(self, tokens: TokenSet) -> None:
        """Run the hook, then store."""
        self._hook(tokens)
        await super().async_save(tokens)


async def test_fresh_token_needs_no_refresh(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A fresh stored token is used as is: 0 POSTs at startup."""
    tokens = _tokens(600)
    manager = TokenManager(session, MemoryTokenStore(tokens))
    assert await manager.async_get_access_token() == ACCESS_B
    assert manager.expires_at == tokens.expires_at
    assert not manager.auth_failed
    assert _refreshes(mock_tado) == []


async def test_stale_token_is_refreshed_and_persisted(
    mock_tado: TadoCloudMock, session: ClientSession, freezer: FrozenDateTimeFactory
) -> None:
    """A token inside the refresh margin is refreshed and the rotation stored."""
    store = MemoryTokenStore(_tokens(30))
    manager = TokenManager(session, store)

    assert await manager.async_get_access_token() == "fake-access-token-0001"

    [call] = _refreshes(mock_tado)
    assert call.body == {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_B,
    }
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token-0001"
    assert stored.expires_at == datetime.now(UTC) + timedelta(seconds=599)
    assert manager.expires_at == stored.expires_at


async def test_failed_persistence_keeps_the_rotation(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed save keeps the rotation for the next refresh, whose save stores it."""
    saved: list[TokenSet] = []

    def save_fails_once(tokens: TokenSet) -> None:
        saved.append(tokens)
        if len(saved) == 1:
            raise OSError("disk full")

    store = RecordingStore(_tokens(0), save_fails_once)
    manager = TokenManager(session, store)

    assert await manager.async_get_access_token() == "fake-access-token-0001"
    assert await manager.async_get_access_token() == "fake-access-token-0001"
    assert len(_refreshes(mock_tado)) == 1
    freezer.tick(timedelta(minutes=10))
    assert await manager.async_get_access_token() == "fake-access-token-0002"

    assert [call.body["refresh_token"] for call in _refreshes(mock_tado)] == [
        REFRESH_B,
        "fake-refresh-token-0001",
    ]
    assert not manager.auth_failed
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token-0002"
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == "Storing the rotated tado tokens failed"
    assert "fake-refresh-token-0001" not in caplog.text


async def test_concurrent_callers_share_one_refresh(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """10 concurrent callers with a stale token cause exactly 1 POST."""
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    release = mock_tado.hold("POST", TOKEN_URL)
    tasks = [asyncio.create_task(manager.async_get_access_token()) for _ in range(10)]
    await _until(lambda: len(_refreshes(mock_tado)) == 1)
    await asyncio.sleep(0)
    assert len(_refreshes(mock_tado)) == 1

    release.set()
    assert set(await asyncio.gather(*tasks)) == {"fake-access-token-0001"}
    assert len(_refreshes(mock_tado)) == 1


async def test_concurrent_401s_share_one_refresh(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Two requests rejected with the same token cause 1 refresh."""
    manager = TokenManager(session, MemoryTokenStore(_tokens(600)))
    release = mock_tado.hold("POST", TOKEN_URL)
    tasks = [
        asyncio.create_task(manager.async_handle_unauthorized(ACCESS_B))
        for _ in range(2)
    ]
    await _until(lambda: len(_refreshes(mock_tado)) == 1)
    release.set()
    assert set(await asyncio.gather(*tasks)) == {"fake-access-token-0001"}
    assert len(_refreshes(mock_tado)) == 1


async def test_401_with_a_replaced_token_needs_no_refresh(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A 401 for a token that was replaced meanwhile reuses the current one."""
    manager = TokenManager(session, MemoryTokenStore(_tokens(600)))
    assert await manager.async_handle_unauthorized("an-older-token") == ACCESS_B
    assert _refreshes(mock_tado) == []


async def test_rotation_is_persisted_before_use(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """The rotated tokens are stored before any request uses them."""
    seen: list[tuple[str, int, datetime | None]] = []

    def before_save(tokens: TokenSet) -> None:
        api_calls = [
            call for call in mock_tado.calls() if call.url.host != "login.tado.com"
        ]
        seen.append((tokens.access_token, len(api_calls), manager.expires_at))

    old = _tokens(0)
    manager = TokenManager(session, RecordingStore(old, before_save))
    transport = Transport(session, manager, QuotaTracker())

    rooms = await transport.request("GET", ROOMS_B)

    assert len(rooms) == 6
    assert seen == [("fake-access-token-0001", 0, old.expires_at)]
    [api_call] = mock_tado.calls("GET", ROOMS_B)
    assert api_call.headers["Authorization"] == "Bearer fake-access-token-0001"


async def test_tokens_persisted_elsewhere_are_adopted(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Tokens another instance stored meanwhile are used without a POST."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    await store.async_save(_tokens(600, ACCESS_C, REFRESH_C))
    assert await manager.async_get_access_token() == ACCESS_C
    assert _refreshes(mock_tado) == []


@pytest.mark.parametrize(
    ("stored_expires_in", "expected", "posts"),
    [(600, ACCESS_C, 1), (0, "fake-access-token-0001", 2)],
)
async def test_invalid_grant_after_rotation_elsewhere(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    stored_expires_in: float,
    expected: str,
    posts: int,
) -> None:
    """invalid_grant while another instance rotated: its tokens are used, or refreshed once."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    task = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    mock_tado.valid_refresh_tokens.discard(REFRESH_B)
    await store.async_save(_tokens(stored_expires_in, ACCESS_C, REFRESH_C))
    release.set()

    assert await task == expected
    assert len(_refreshes(mock_tado)) == posts
    assert _refreshes(mock_tado)[-1].body["refresh_token"] == (
        REFRESH_B if posts == 1 else REFRESH_C
    )
    assert not manager.auth_failed


@pytest.mark.parametrize(
    ("stored_expires_in", "expected", "posts"),
    [(600, ACCESS_C, 1), (0, "fake-access-token-0002", 2)],
)
async def test_tokens_stored_during_a_refresh_win(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    stored_expires_in: float,
    expected: str,
    posts: int,
) -> None:
    """A login stored while a refresh runs is kept; the refresh result is dropped."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    task = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    await store.async_save(_tokens(stored_expires_in, ACCESS_C, REFRESH_C))
    release.set()

    assert await task == expected
    stored = store.load()
    assert stored is not None
    assert stored.access_token == expected
    assert len(_refreshes(mock_tado)) == posts
    assert _refreshes(mock_tado)[-1].body["refresh_token"] == (
        REFRESH_B if posts == 1 else REFRESH_C
    )
    assert await manager.async_get_access_token() == expected
    assert len(_refreshes(mock_tado)) == posts


async def test_second_invalid_grant_is_sticky(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """When the other instance's tokens are rejected too, the auth failure sticks."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    task = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    mock_tado.valid_refresh_tokens.clear()
    await store.async_save(_tokens(0, ACCESS_C, REFRESH_C))
    release.set()

    with pytest.raises(TadoXAuthError) as err:
        await task
    assert err.value.reason == "invalid_grant"
    assert manager.auth_failed
    assert len(_refreshes(mock_tado)) == 2


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ({"exc": ClientConnectorError(Mock(), OSError("dns"))}, TadoXConnectionError),
        ({"exc": ServerDisconnectedError()}, TadoXConnectionError),
        ({"exc": ServerTimeoutError()}, TadoXConnectionError),
        ({"exc": TimeoutError()}, TadoXConnectionError),
        ({"status": 429}, TadoXServerError),
        ({"status": 503}, TadoXServerError),
        ({"status": 200, "body": {"token_type": "Bearer"}}, TadoXParseError),
        ({"status": 200, "body": ["not", "an", "object"]}, TadoXParseError),
    ],
)
async def test_transient_refresh_failures(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    failure: dict[str, Any],
    error: type[Exception],
) -> None:
    """Network, timeout, 429, 5xx and garbage on refresh never fail the auth."""
    mock_tado.fail("POST", TOKEN_URL, **failure)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    with pytest.raises(error) as err:
        await manager.async_get_access_token()
    if isinstance(err.value, TadoXServerError):
        assert err.value.status == failure["status"]
    assert not manager.auth_failed
    assert not isinstance(err.value, TadoXAuthError)


async def test_unreadable_login_answer_is_transient(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A proxy error page from login.tado.com is transient, not an auth failure."""
    aioclient_mock.post(TOKEN_URL, status=502, text="<html>Bad gateway</html>")
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    with pytest.raises(TadoXServerError):
        await manager.async_get_access_token()
    assert not manager.auth_failed


async def test_old_token_is_used_while_refresh_fails(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A transient refresh failure falls back to the still valid access token."""
    mock_tado.fail("POST", TOKEN_URL, status=503)
    manager = TokenManager(session, MemoryTokenStore(_tokens(30)))
    assert await manager.async_get_access_token() == ACCESS_B
    assert len(_refreshes(mock_tado)) == 1

    with pytest.raises(TadoXServerError):
        await manager.async_handle_unauthorized(ACCESS_B)
    assert len(_refreshes(mock_tado)) == 1


async def test_backoff_suppresses_refreshes(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After transient failures no POST is sent for 5, 15, 60 and 300 s."""
    caplog.set_level(logging.DEBUG)
    mock_tado.fail("POST", TOKEN_URL, status=503)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    expected_posts = 0
    for delay in (5, 15, 60, 300, 300):
        with pytest.raises(TadoXServerError):
            await manager.async_get_access_token()
        expected_posts += 1
        assert len(_refreshes(mock_tado)) == expected_posts

        freezer.tick(timedelta(seconds=delay - 1))
        with pytest.raises(TadoXServerError):
            await manager.async_get_access_token()
        assert len(_refreshes(mock_tado)) == expected_posts
        freezer.tick(timedelta(seconds=1))

    mock_tado.clear_failures()
    assert await manager.async_get_access_token() == "fake-access-token-0001"

    mock_tado.fail("POST", TOKEN_URL, status=503)
    freezer.tick(timedelta(seconds=600))
    with pytest.raises(TadoXServerError):
        await manager.async_get_access_token()
    freezer.tick(timedelta(seconds=5))
    with pytest.raises(TadoXServerError):
        await manager.async_get_access_token()
    assert len(_refreshes(mock_tado)) == expected_posts + 3
    # Only the first failure of a series and the recovery are state changes.
    assert _client_log(caplog, logging.INFO) == [
        (
            logging.INFO,
            "Refreshing the tado access token failed (TadoXServerError, HTTP 503), "
            "retrying in 5 s",
        ),
        (
            logging.INFO,
            "Refreshing the tado access token works again after 5 failed attempts",
        ),
        (
            logging.INFO,
            "Refreshing the tado access token failed (TadoXServerError, HTTP 503), "
            "retrying in 5 s",
        ),
    ]
    debug = [
        message for level, message in _client_log(caplog) if level == logging.DEBUG
    ]
    for delay in (15, 60, 300):
        assert (
            "Refreshing the tado access token failed (TadoXServerError, HTTP 503), "
            f"retrying in {delay} s" in debug
        )
    assert "Not refreshing the tado access token during its backoff" in debug


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (400, None, "invalid_grant"),
        (400, {"error": "invalid_request"}, "invalid_request"),
        (401, {"error": "invalid_client"}, "invalid_client"),
        (400, {"error": "unauthorized_client"}, "unauthorized_client"),
    ],
)
async def test_rejected_refresh_is_sticky(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    status: int,
    body: dict[str, Any] | None,
    reason: str,
) -> None:
    """An OAuth rejection of the refresh token fails the auth until reload."""
    mock_tado.fail("POST", TOKEN_URL, status=status, body=body)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    with pytest.raises(TadoXAuthError) as err:
        await manager.async_get_access_token()
    assert err.value.reason == reason
    assert manager.auth_failed

    mock_tado.clear_failures()
    for call in (
        manager.async_get_access_token(),
        manager.async_handle_unauthorized(ACCESS_B),
    ):
        with pytest.raises(TadoXAuthError) as err:
            await call
        assert err.value.reason == reason
    assert len(_refreshes(mock_tado)) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, "<html>Request blocked by the CDN</html>"),
        (400, "<html>400 Request Header Or Cookie Too Large</html>"),
        (401, '{"message": "proxy authentication required"}'),
        (403, '{"error": ""}'),
        (400, '{"error": "unsupported_grant_type"}'),
    ],
    ids=["waf", "proxy_400", "proxy_401", "empty_error", "other_oauth_error"],
)
async def test_login_rejection_without_oauth_error_is_transient(
    aioclient_mock: AiohttpClientMocker,
    session: ClientSession,
    freezer: FrozenDateTimeFactory,
    status: int,
    body: str,
) -> None:
    """A 4xx that is no OAuth rejection of the grant backs off and keeps the token."""
    aioclient_mock.post(TOKEN_URL, status=status, text=body)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))

    with pytest.raises(TadoXServerError) as err:
        await manager.async_get_access_token()

    assert err.value.status == status
    assert not manager.auth_failed
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "fake-access-token-0001", "expires_in": 599},
    )
    freezer.tick(timedelta(seconds=5))
    assert await manager.async_get_access_token() == "fake-access-token-0001"
    [(_, _, form, _)] = aioclient_mock.mock_calls
    assert form["refresh_token"] == REFRESH_B


async def test_reused_refresh_token_is_an_auth_failure(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A refresh token that was already rotated answers invalid_grant."""
    mock_tado.valid_refresh_tokens.discard(REFRESH_B)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    with pytest.raises(TadoXAuthError) as err:
        await manager.async_get_access_token()
    assert err.value.reason == "invalid_grant"


async def test_missing_tokens_are_an_auth_failure(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Without stored tokens there is nothing to refresh."""
    manager = TokenManager(session, MemoryTokenStore())
    assert manager.expires_at is None
    with pytest.raises(TadoXAuthError) as err:
        await manager.async_get_access_token()
    assert err.value.reason == "no_tokens"
    assert manager.auth_failed
    assert _refreshes(mock_tado) == []


async def test_close_waits_for_the_running_refresh(
    mock_tado: TadoCloudMock, session: ClientSession, freezer: FrozenDateTimeFactory
) -> None:
    """Closing waits until the rotation is stored, then refuses new refreshes."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    refresh = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)
    waiter = asyncio.create_task(manager.async_handle_unauthorized(ACCESS_B))
    close = asyncio.create_task(manager.async_close())
    await asyncio.sleep(0)
    assert not close.done()

    release.set()
    assert await refresh == "fake-access-token-0001"
    await close
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token-0001"
    assert await waiter == "fake-access-token-0001"
    assert await manager.async_get_access_token() == "fake-access-token-0001"
    with pytest.raises(TadoXConnectionError):
        await manager.async_handle_unauthorized("fake-access-token-0001")
    freezer.tick(timedelta(minutes=10))
    with pytest.raises(TadoXTransientError):
        await manager.async_get_access_token()
    assert len(_refreshes(mock_tado)) == 1


async def test_cancelled_caller_leaves_the_refresh_running(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Cancelling the caller neither aborts the POST nor loses the rotation."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    caller = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    waiter = asyncio.create_task(manager.async_get_access_token())
    await asyncio.sleep(0)
    release.set()

    assert await waiter == "fake-access-token-0001"
    assert len(_refreshes(mock_tado)) == 1
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token-0001"


async def test_close_waits_for_a_refresh_whose_caller_was_cancelled(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A poll cancelled by an unload does not stop close from waiting for its refresh."""
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    release = mock_tado.hold("POST", TOKEN_URL)
    caller = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    caller.cancel()
    close = asyncio.create_task(manager.async_close())
    await _until(caller.done)
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close

    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token-0001"
    assert REFRESH_B not in mock_tado.valid_refresh_tokens


async def test_failed_refresh_of_a_cancelled_caller_is_kept(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """An invalid_grant nobody waited for still makes the auth failure sticky."""
    mock_tado.valid_refresh_tokens.discard(REFRESH_B)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    release = mock_tado.hold("POST", TOKEN_URL)
    caller = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    caller.cancel()
    await _until(caller.done)
    release.set()
    await manager.async_close()

    assert manager.auth_failed
    with pytest.raises(TadoXAuthError):
        await manager.async_get_access_token()
    assert len(_refreshes(mock_tado)) == 1


@pytest.mark.parametrize(
    ("old_grant_valid", "login_expires_in", "expected", "posts"),
    [
        (True, 600, ACCESS_C, 1),
        (False, 600, ACCESS_C, 1),
        (True, 0, "fake-access-token-0002", 2),
        (False, 0, "fake-access-token-0001", 2),
    ],
    ids=["rotated", "rejected", "rotated_stale_login", "rejected_stale_login"],
)
async def test_login_adopted_during_a_cancelled_callers_refresh_wins(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    old_grant_valid: bool,
    login_expires_in: float,
    expected: str,
    posts: int,
) -> None:
    """A login another caller adopted before the old refresh answers is kept.

    Neither the rotation of the old grant nor its rejection may replace the
    login or fail the auth; a stale login is refreshed instead.
    """
    store = MemoryTokenStore(_tokens(0))
    manager = TokenManager(session, store)
    if not old_grant_valid:
        mock_tado.valid_refresh_tokens.discard(REFRESH_B)
    release = mock_tado.hold("POST", TOKEN_URL)
    caller = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    login = _tokens(login_expires_in, ACCESS_C, REFRESH_C)
    await store.async_save(login)
    adopter = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: manager.expires_at == login.expires_at)
    release.set()

    assert await adopter == expected
    await manager.async_close()
    assert not manager.auth_failed
    stored = store.load()
    assert stored is not None
    assert stored.access_token == expected
    assert len(_refreshes(mock_tado)) == posts
    assert _refreshes(mock_tado)[-1].body["refresh_token"] == (
        REFRESH_B if posts == 1 else REFRESH_C
    )
    assert await manager.async_get_access_token() == expected


async def test_login_stored_before_the_refresh_task_starts_wins(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A login stored after a refresh was decided but before its task ran is kept."""
    store = MemoryTokenStore(_tokens(0))
    login = _tokens(600, ACCESS_C, REFRESH_C)

    def create_task(coro: Coroutine[Any, Any, TokenSet]) -> asyncio.Task[TokenSet]:
        # Tasks start in creation order, so the login is stored first.
        asyncio.get_running_loop().create_task(store.async_save(login))
        return asyncio.create_task(coro)

    manager = TokenManager(session, store, create_task=create_task)

    assert await manager.async_get_access_token() == ACCESS_C
    assert store.load() == login
    assert len(_refreshes(mock_tado)) == 1


async def test_refresh_runs_in_the_injected_task(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """The refresh runs in a task of the given factory, so the host can track it."""
    created: list[asyncio.Task[TokenSet]] = []

    def create_task(coro: Coroutine[Any, Any, TokenSet]) -> asyncio.Task[TokenSet]:
        task = asyncio.create_task(coro)
        created.append(task)
        return task

    manager = TokenManager(
        session, MemoryTokenStore(_tokens(0)), create_task=create_task
    )

    assert await manager.async_get_access_token() == "fake-access-token-0001"
    [task] = created
    assert task.done()
    assert task.result().access_token == "fake-access-token-0001"


async def test_refresh_task_cancelled_by_the_host(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A refresh task the host cancels ends its callers; the next call starts anew."""
    created: list[asyncio.Task[TokenSet]] = []

    def create_task(coro: Coroutine[Any, Any, TokenSet]) -> asyncio.Task[TokenSet]:
        created.append(asyncio.create_task(coro))
        return created[-1]

    manager = TokenManager(
        session, MemoryTokenStore(_tokens(0)), create_task=create_task
    )
    release = mock_tado.hold("POST", TOKEN_URL)
    caller = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    created[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()

    assert await manager.async_get_access_token() == "fake-access-token-0001"
    assert len(created) == 2
    assert not manager.auth_failed


async def test_close_gives_up_waiting(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Closing does not hang on a refresh that never finishes."""
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)))
    release = mock_tado.hold("POST", TOKEN_URL)
    refresh = asyncio.create_task(manager.async_get_access_token())
    await _until(lambda: len(_refreshes(mock_tado)) == 1)

    await manager.async_close(timeout=0.01)

    release.set()
    assert await refresh == "fake-access-token-0001"


async def test_routine_refresh_logs_at_debug_only(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A routine refresh is one exchange and debug lines without any token value."""
    caplog.set_level(logging.DEBUG)
    telemetry = Telemetry()
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)), telemetry=telemetry)

    assert await manager.async_get_access_token() == "fake-access-token-0001"

    expiry = (datetime.now(UTC) + timedelta(seconds=599)).isoformat()
    log = _client_log(caplog)
    assert log[0][0] == logging.DEBUG
    assert log[0][1].startswith(f"{TOKEN_REQUEST} -> 200 in ")
    assert log[1:] == [
        (
            logging.DEBUG,
            f"Stored the rotated tado tokens; the access token expires at {expiry}",
        )
    ]
    [exchange] = telemetry.exchanges()
    assert (exchange.at, exchange.method, exchange.endpoint) == (
        datetime.now(UTC),
        "POST",
        TOKEN_REQUEST,
    )
    assert (exchange.status, exchange.outcome, exchange.shape, exchange.error) == (
        200,
        "ok",
        None,
        None,
    )
    assert telemetry.errors() == []
    for secret in ("fake-access-token", "fake-refresh-token", REFRESH_B):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("failure", "outcome", "error", "message"),
    [
        (
            {"exc": ClientConnectorError(Mock(), OSError("dns of 192.168.100.200"))},
            "TadoXConnectionError",
            "ClientConnectorError",
            "TadoXConnectionError, cannot reach login.tado.com (ClientConnectorError)",
        ),
        ({"status": 502}, "TadoXServerError", "HTTP 502", "TadoXServerError, HTTP 502"),
        (
            {"status": 200, "body": {"token_type": "Bearer"}},
            "TadoXParseError",
            "invalid token response",
            "TadoXParseError, unexpected token payload at $.access_token (missing)",
        ),
    ],
    ids=["connection", "server", "parse"],
)
async def test_failed_refresh_is_recorded_once(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
    failure: dict[str, Any],
    outcome: str,
    error: str,
    message: str,
) -> None:
    """A failed refresh is an info line, an exchange and one error record."""
    caplog.set_level(logging.DEBUG)
    telemetry = Telemetry()
    mock_tado.fail("POST", TOKEN_URL, **failure)
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)), telemetry=telemetry)
    transport = Transport(session, manager, QuotaTracker(), telemetry=telemetry)

    for _ in range(2):
        with pytest.raises((TadoXTransientError, TadoXParseError)) as err:
            await transport.request("GET", ROOMS_B)
        telemetry.record_tado_error(err.value, context="poll rooms")

    assert err.value.request == TOKEN_REQUEST
    [exchange] = telemetry.exchanges()
    assert (exchange.endpoint, exchange.outcome, exchange.error) == (
        TOKEN_REQUEST,
        outcome,
        error,
    )
    [record] = telemetry.errors()
    assert (record.context, record.endpoint, record.kind) == (
        "token refresh",
        TOKEN_REQUEST,
        outcome,
    )
    assert _client_log(caplog, logging.INFO) == [
        (
            logging.INFO,
            f"Refreshing the tado access token failed ({message}), retrying in 5 s",
        )
    ]
    for secret in ("fake-access-token", "fake-refresh-token", "192.168"):
        assert secret not in caplog.text


async def test_rejected_login_logs_its_code_only(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An invalid_grant is one info line by its code; the login body never shows."""
    caplog.set_level(logging.DEBUG)
    telemetry = Telemetry()
    mock_tado.fail(
        "POST",
        TOKEN_URL,
        status=400,
        body={
            "error": "invalid_grant",
            "error_description": f"{REFRESH_B} of user@example.invalid was revoked",
        },
    )
    manager = TokenManager(session, MemoryTokenStore(_tokens(0)), telemetry=telemetry)

    with pytest.raises(TadoXAuthError) as first:
        await manager.async_get_access_token()
    with pytest.raises(TadoXAuthError) as sticky:
        await manager.async_get_access_token()

    assert first.value.request == sticky.value.request == TOKEN_REQUEST
    assert _client_log(caplog, logging.INFO) == [
        (logging.INFO, "tado rejected the login (invalid_grant); a new login is needed")
    ]
    [exchange] = telemetry.exchanges()
    assert (exchange.status, exchange.outcome, exchange.error) == (
        400,
        "TadoXAuthError",
        "invalid_grant",
    )
    assert telemetry.last_error is not None
    assert telemetry.last_error.message == "invalid_grant"
    for secret in ("fake-refresh-token", "user@example", "revoked"):
        assert secret not in caplog.text


async def test_missing_tokens_are_recorded(
    mock_tado: TadoCloudMock, session: ClientSession, freezer: FrozenDateTimeFactory
) -> None:
    """A grant without tokens is an auth error without a request."""
    telemetry = Telemetry()
    manager = TokenManager(session, MemoryTokenStore(), telemetry=telemetry)
    with pytest.raises(TadoXAuthError):
        await manager.async_get_access_token()
    assert telemetry.exchanges() == []
    [record] = telemetry.errors()
    assert (record.kind, record.endpoint, record.message) == (
        "TadoXAuthError",
        None,
        "no_tokens",
    )


async def test_401_handling_is_logged_at_debug(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token that was replaced already needs no refresh, and says so."""
    caplog.set_level(logging.DEBUG)
    manager = TokenManager(session, MemoryTokenStore(_tokens(600)))
    assert await manager.async_handle_unauthorized("an-older-token") == ACCESS_B
    assert _client_log(caplog) == [
        (logging.DEBUG, "tado rejected an access token that was replaced already")
    ]

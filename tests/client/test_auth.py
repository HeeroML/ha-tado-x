"""Tests for the single-flight token refresh of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator, Callable
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
    TokenManager,
    TokenSet,
    Transport,
)
from custom_components.tado_x.client.const import HOPS_API_URL, TOKEN_URL
from tests.tado_mock import HOME_B, HOME_C, Call, TadoCloudMock, entry_tokens

ACCESS_B, REFRESH_B = entry_tokens(HOME_B)
ACCESS_C, REFRESH_C = entry_tokens(HOME_C)
ROOMS_B = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"


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
    mock_tado: TadoCloudMock, session: ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    """When storing fails the rotated tokens are still used, and it is logged."""

    def broken_save(tokens: TokenSet) -> None:
        raise OSError("disk full")

    manager = TokenManager(session, RecordingStore(_tokens(0), broken_save))

    assert await manager.async_get_access_token() == "fake-access-token-0001"
    assert await manager.async_get_access_token() == "fake-access-token-0001"
    assert len(_refreshes(mock_tado)) == 1
    assert not manager.auth_failed
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
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    mock_tado.fail("POST", TOKEN_URL, status=503)
    freezer.tick(timedelta(seconds=600))
    with pytest.raises(TadoXServerError):
        await manager.async_get_access_token()
    freezer.tick(timedelta(seconds=5))
    with pytest.raises(TadoXServerError):
        await manager.async_get_access_token()
    assert len(_refreshes(mock_tado)) == expected_posts + 3


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (400, None, "invalid_grant"),
        (400, {"error": "invalid_client"}, "invalid_client"),
        (401, {"message": "nope"}, "http_401"),
        (403, {"error": ""}, "http_403"),
    ],
)
async def test_rejected_refresh_is_sticky(
    mock_tado: TadoCloudMock,
    session: ClientSession,
    status: int,
    body: dict[str, Any] | None,
    reason: str,
) -> None:
    """400, 401 and 403 from the token endpoint fail the auth until reload."""
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
    mock_tado: TadoCloudMock, session: ClientSession
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
    with pytest.raises(TadoXConnectionError):
        await waiter
    with pytest.raises(TadoXTransientError):
        await manager.async_get_access_token()
    assert len(_refreshes(mock_tado)) == 1


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

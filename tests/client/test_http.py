"""Tests for the HTTP transport of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from aiohttp import ClientConnectionError, ClientSession
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tado_x.client import (
    MemoryTokenStore,
    Priority,
    QuotaTracker,
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
    TokenManager,
    TokenSet,
    Transport,
)
from custom_components.tado_x.client.const import HOPS_API_URL, MY_API_URL, TOKEN_URL
from tests.tado_mock import HOME_A, HOME_B, TadoCloudMock, entry_tokens

ACCESS_B, REFRESH_B = entry_tokens(HOME_B)
ROOMS_B = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"
STATE_B = f"{MY_API_URL}/homes/{HOME_B}/state"

type Observed = list[tuple[int, str, Any, datetime]]


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def quota() -> QuotaTracker:
    """Return the quota tracker of the transport."""
    return QuotaTracker()


@pytest.fixture
def tokens(session: ClientSession) -> TokenManager:
    """Return a token manager with a fresh token of home 1000002."""
    fresh = TokenSet(
        access_token=ACCESS_B,
        refresh_token=REFRESH_B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return TokenManager(session, MemoryTokenStore(fresh))


@pytest.fixture
def observed() -> Observed:
    """Return the payloads the observer received."""
    return []


@pytest.fixture
def transport(
    session: ClientSession,
    tokens: TokenManager,
    quota: QuotaTracker,
    observed: Observed,
) -> Transport:
    """Return a transport whose payload observer records into observed."""
    transport = Transport(session, tokens, quota)
    transport.set_payload_observer(lambda *args: observed.append(args))
    return transport


async def test_json_without_content_type(
    mock_tado: TadoCloudMock,
    transport: Transport,
    observed: Observed,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Hops answers JSON without a content type; cached reads reach the observer."""
    rooms = await transport.request("GET", ROOMS_B, cache=(HOME_B, "rooms"))
    assert [room["id"] for room in rooms] == [1, 2, 3, 4, 5, 6]
    assert observed == [(HOME_B, "rooms", rooms, datetime.now(UTC))]

    state = await transport.request("GET", STATE_B)
    assert state == {"presence": "HOME", "presenceLocked": False}
    assert len(observed) == 1

    [call] = mock_tado.calls("GET", ROOMS_B)
    assert call.headers["Authorization"] == f"Bearer {ACCESS_B}"
    assert "Content-Type" not in call.headers


async def test_empty_2xx_bodies(
    mock_tado: TadoCloudMock, transport: Transport, observed: Observed
) -> None:
    """A 204 and an empty 200 are a success with no body."""
    url = f"{ROOMS_B}/2/manualControl"
    assert await transport.request("DELETE", url, priority=Priority.COMMAND) is None
    assert (
        await transport.request(
            "POST",
            f"{HOPS_API_URL}/homes/{HOME_B}/quickActions/boost",
            cache=(HOME_B, "boost"),
        )
        is None
    )
    assert observed == []


async def test_json_body_and_params(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """A JSON body is sent with its content type; params go into the query."""
    body = {"homePresence": "AWAY"}
    await transport.request(
        "PUT", f"{MY_API_URL}/homes/{HOME_B}/presenceLock", json=body
    )
    [call] = mock_tado.calls("PUT")
    assert call.body == body
    assert call.headers["Content-Type"] == "application/json"

    await transport.request(
        "GET",
        f"{MY_API_URL}/homes/{HOME_B}/zones/1/dayReport",
        params={"date": "2026-10-30"},
    )
    [call] = mock_tado.calls("GET", "/dayReport")
    assert call.url.query["date"] == "2026-10-30"


async def test_401_refresh_and_retried_204(
    mock_tado: TadoCloudMock, transport: Transport, quota: QuotaTracker
) -> None:
    """A 401 refreshes the token once and a 204 on the retry succeeds (T-6)."""
    mock_tado.revoke_access_tokens()
    url = f"{ROOMS_B}/2/manualControl"
    assert await transport.request("DELETE", url, priority=Priority.COMMAND) is None

    first, retry = mock_tado.calls("DELETE", url)
    assert first.headers["Authorization"] == f"Bearer {ACCESS_B}"
    assert retry.headers["Authorization"] == "Bearer fake-access-token-0001"
    assert len(mock_tado.calls("POST", TOKEN_URL)) == 1
    assert quota.calls.total(datetime.now(UTC)) == 2


async def test_second_401_is_an_auth_error(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """A 401 after a successful refresh is a definitive auth error."""
    mock_tado.fail("GET", ROOMS_B, status=401)
    with pytest.raises(TadoXAuthError) as err:
        await transport.request("GET", ROOMS_B)
    assert err.value.reason == "unauthorized_after_refresh"
    assert len(mock_tado.calls("GET", ROOMS_B)) == 2
    assert len(mock_tado.calls("POST", TOKEN_URL)) == 1


async def test_concurrent_401s_refresh_once(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """Two requests rejected with the same token share one refresh."""
    mock_tado.revoke_access_tokens()
    release = mock_tado.hold("POST", TOKEN_URL)
    rooms = asyncio.create_task(transport.request("GET", ROOMS_B))
    state = asyncio.create_task(transport.request("GET", STATE_B))
    for _ in range(100):
        if len(mock_tado.calls("GET")) == 2:
            break
        await asyncio.sleep(0)
    assert len(mock_tado.calls("GET")) == 2

    release.set()
    assert len(await rooms) == 6
    assert (await state)["presence"] == "HOME"
    assert len(mock_tado.calls("GET")) == 4
    assert len(mock_tado.calls("POST", TOKEN_URL)) == 1


async def test_home_not_x(mock_tado: TadoCloudMock, transport: Transport) -> None:
    """Hops' "Timezone of home" 404 means the home is no tado X home (#40)."""
    with pytest.raises(TadoXHomeNotXError) as err:
        await transport.request("GET", f"{HOPS_API_URL}/homes/{HOME_A}/rooms")
    assert err.value.home_id == HOME_A
    assert err.value.code == "not.found"
    assert err.value.title == f"Timezone of home {HOME_A} not found"


@pytest.mark.parametrize(
    "url",
    [f"{HOPS_API_URL}/features", f"{MY_API_URL}/homes/{HOME_B}/features"],
)
async def test_timezone_title_elsewhere_is_not_found(
    mock_tado: TadoCloudMock, transport: Transport, url: str
) -> None:
    """Only a hops home path with that title is read as a non-X home."""
    body = {"errors": [{"code": "not.found", "title": "Timezone of home x not found"}]}
    mock_tado.fail("GET", url, status=404, body=body)
    with pytest.raises(TadoXNotFoundError) as err:
        await transport.request("GET", url)
    assert not isinstance(err.value, TadoXHomeNotXError)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, TadoXForbiddenError),
        (404, TadoXNotFoundError),
        (409, TadoXRequestError),
        (500, TadoXServerError),
        (503, TadoXServerError),
    ],
)
async def test_error_statuses(
    mock_tado: TadoCloudMock,
    transport: Transport,
    status: int,
    error: type[TadoXError],
) -> None:
    """4xx answers are request errors and 5xx answers are transient."""
    mock_tado.fail("GET", ROOMS_B, status=status)
    with pytest.raises(error) as err:
        await transport.request("GET", ROOMS_B)
    assert type(err.value) is error
    if isinstance(err.value, TadoXRequestError):
        assert (err.value.status, err.value.code) == (status, "error")
    else:
        assert isinstance(err.value, TadoXServerError)
        assert err.value.status == status


async def test_422_keeps_tado_error(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """The first entry of the errors body is kept for the caller."""
    with pytest.raises(TadoXRequestError) as err:
        await transport.request("POST", f"{ROOMS_B}/1/openWindow")
    assert (err.value.status, err.value.code, err.value.title) == (
        422,
        "openWindow.notDetected",
        "No open window detected",
    )


async def test_429_headers_are_observed_before_raising(
    mock_tado: TadoCloudMock,
    transport: Transport,
    quota: QuotaTracker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 429 closes the gate from its headers; later requests send nothing."""
    mock_tado.fail("GET", ROOMS_B, status=429)
    with pytest.raises(TadoXRateLimitError) as err:
        await transport.request("GET", ROOMS_B)
    assert not err.value.local
    assert err.value.reset_at == datetime.now(UTC) + timedelta(seconds=7112)
    assert err.value.info is not None
    assert err.value.info.remaining == 0
    assert quota.blocked_until == err.value.reset_at

    mock_tado.clear_calls()
    for priority in Priority:
        with pytest.raises(TadoXRateLimitError) as err:
            await transport.request("GET", STATE_B, priority=priority)
        assert err.value.local
    assert mock_tado.calls() == []


async def test_gate_refuses_before_any_token_refresh(
    mock_tado: TadoCloudMock, session: ClientSession, quota: QuotaTracker
) -> None:
    """A closed gate sends nothing at all, not even a token refresh."""
    stale = TokenSet(
        access_token=ACCESS_B,
        refresh_token=REFRESH_B,
        expires_at=datetime.now(UTC),
    )
    transport = Transport(
        session, TokenManager(session, MemoryTokenStore(stale)), quota
    )
    quota.observe(None, status=429)
    with pytest.raises(TadoXRateLimitError):
        await transport.request("GET", ROOMS_B, priority=Priority.COMMAND)
    assert mock_tado.calls() == []


@pytest.mark.parametrize(
    "exc", [ClientConnectionError("connection reset"), TimeoutError()]
)
async def test_connection_errors(
    mock_tado: TadoCloudMock,
    transport: Transport,
    quota: QuotaTracker,
    exc: Exception,
) -> None:
    """No answer is a connection error that names the host only; it still counts."""
    mock_tado.fail("GET", ROOMS_B, exc=exc)
    with pytest.raises(TadoXConnectionError) as err:
        await transport.request("GET", ROOMS_B)
    assert str(err.value) == "cannot reach hops.tado.com"
    assert quota.calls.total(datetime.now(UTC)) == 1


async def test_token_is_never_logged(
    mock_tado: TadoCloudMock, transport: Transport, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging names method, host, path and status, never the token."""
    mock_tado.revoke_access_tokens()
    mock_tado.fail("GET", STATE_B, status=500, times=1)
    with caplog.at_level(logging.DEBUG):
        await transport.request("GET", ROOMS_B)
        with pytest.raises(TadoXServerError):
            await transport.request("GET", STATE_B)
    assert f"GET hops.tado.com/homes/{HOME_B}/rooms: HTTP 401" in caplog.text
    assert ACCESS_B not in caplog.text
    assert "fake-access-token-0001" not in caplog.text
    assert REFRESH_B not in caplog.text


@pytest.mark.parametrize(
    ("response", "error", "code", "title"),
    [
        (
            {"status": 400, "text": "<html>Bad request</html>"},
            TadoXRequestError,
            None,
            None,
        ),
        (
            {"status": 400, "json": ["not", "an", "object"]},
            TadoXRequestError,
            None,
            None,
        ),
        ({"status": 422, "json": {"errors": ["oops"]}}, TadoXRequestError, None, None),
        (
            {"status": 400, "json": {"errors": [{"code": 5, "title": None}]}},
            TadoXRequestError,
            None,
            None,
        ),
        ({"status": 304}, TadoXServerError, None, None),
    ],
)
async def test_unusual_error_bodies(
    aioclient_mock: AiohttpClientMocker,
    transport: Transport,
    response: dict[str, Any],
    error: type[TadoXError],
    code: str | None,
    title: str | None,
) -> None:
    """Error bodies that are not tado's error format still map by status."""
    aioclient_mock.get(ROOMS_B, **response)
    with pytest.raises(error) as err:
        await transport.request("GET", ROOMS_B)
    if isinstance(err.value, TadoXRequestError):
        assert (err.value.code, err.value.title) == (code, title)


@pytest.mark.parametrize(
    ("cache", "endpoint"),
    [((HOME_B, "rooms"), "rooms"), (None, f"/homes/{HOME_B}/rooms")],
)
async def test_invalid_json_is_a_parse_error(
    aioclient_mock: AiohttpClientMocker,
    transport: Transport,
    observed: Observed,
    cache: tuple[int, str] | None,
    endpoint: str,
) -> None:
    """A 2xx whose body is not JSON fails as a parse error and is not cached."""
    aioclient_mock.get(ROOMS_B, text="[{]")
    with pytest.raises(TadoXParseError) as err:
        await transport.request("GET", ROOMS_B, cache=cache)
    assert err.value.endpoint == endpoint
    assert observed == []


async def test_without_observer(mock_tado: TadoCloudMock, transport: Transport) -> None:
    """Cached reads work when no observer is set."""
    transport.set_payload_observer(None)
    assert len(await transport.request("GET", ROOMS_B, cache=(HOME_B, "rooms"))) == 6

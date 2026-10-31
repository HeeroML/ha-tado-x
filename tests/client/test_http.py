"""Tests for the HTTP transport of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
import logging
import re
import traceback
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
    Telemetry,
    TokenManager,
    TokenSet,
    Transport,
)
from custom_components.tado_x.client.const import (
    ENERGY_IQ_API_URL,
    HOPS_API_URL,
    MY_API_URL,
    TOKEN_URL,
)
from tests.tado_mock import HOME_A, HOME_B, TadoCloudMock, entry_tokens

ACCESS_B, REFRESH_B = entry_tokens(HOME_B)
ROOMS_B = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"
DEVICE_B = f"{HOPS_API_URL}/homes/{HOME_B}/roomsAndDevices/devices/VA0000000101"
STATE_B = f"{MY_API_URL}/homes/{HOME_B}/state"
ROOMS = "GET hops:/homes/{home}/rooms"
CLIENT_LOGGER = "custom_components.tado_x.client"

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
    mock_tado: TadoCloudMock, transport: Transport, observed: Observed
) -> None:
    """Hops answers JSON without a content type; a request alone caches nothing."""
    rooms = await transport.request("GET", ROOMS_B, endpoint="rooms")
    assert [room["id"] for room in rooms] == [1, 2, 3, 4, 5, 6]
    assert observed == []

    state = await transport.request("GET", STATE_B)
    assert state == {"presence": "HOME", "presenceLocked": False}

    [call] = mock_tado.calls("GET", ROOMS_B)
    assert call.headers["Authorization"] == f"Bearer {ACCESS_B}"
    assert "Content-Type" not in call.headers


async def test_empty_2xx_bodies(mock_tado: TadoCloudMock, transport: Transport) -> None:
    """A 204 and an empty 200 are a success with no body."""
    url = f"{ROOMS_B}/2/manualControl"
    assert await transport.request("DELETE", url, priority=Priority.COMMAND) is None
    assert (
        await transport.request(
            "POST", f"{HOPS_API_URL}/homes/{HOME_B}/quickActions/boost"
        )
        is None
    )


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


async def test_connection_error_does_not_chain_its_cause(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """The aiohttp error, whose text holds the URL, is kept by its class name only."""
    secret = f"Connection timeout to host {DEVICE_B}"
    mock_tado.fail("PATCH", DEVICE_B, exc=ClientConnectionError(secret))

    with pytest.raises(TadoXConnectionError) as err:
        await transport.command("PATCH", DEVICE_B, json={"childLockEnabled": True})

    assert err.value.cause == "ClientConnectionError"
    assert err.value.__cause__ is None
    trace = "".join(traceback.format_exception(err.value))
    assert "ClientConnectionError" not in trace
    for leaked in ("VA0000000101", str(HOME_B), "Connection timeout"):
        assert leaked not in trace


def _client_log(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Return level and message of the client's records, with durations as N."""
    return [
        (record.levelno, re.sub(r"in \d+ ms", "in N ms", record.getMessage()))
        for record in caplog.records
        if record.name == CLIENT_LOGGER
    ]


async def test_token_is_never_logged(
    mock_tado: TadoCloudMock, transport: Transport, caplog: pytest.LogCaptureFixture
) -> None:
    """Each attempt is one debug line by its endpoint template, never the token."""
    mock_tado.revoke_access_tokens()
    mock_tado.fail("GET", STATE_B, status=500, times=1)
    with caplog.at_level(logging.DEBUG):
        await transport.request("GET", ROOMS_B)
        with pytest.raises(TadoXServerError):
            await transport.request("GET", STATE_B)
    log = _client_log(caplog)
    assert {level for level, _ in log} == {logging.DEBUG}
    for line in (
        f"{ROOMS} -> 401 in N ms, quota 17832/20000 left: retried (HTTP 401)",
        "tado rejected the access token; refreshing it",
        "POST login:/oauth2/token -> 200 in N ms",
        f"{ROOMS} -> 200 in N ms, quota 17831/20000 left, attempt 2 after 401",
        "GET my:/homes/{home}/state -> 500 in N ms, quota 17830/20000 left: "
        "TadoXServerError (HTTP 500)",
    ):
        assert (logging.DEBUG, line) in log
    for secret in (ACCESS_B, "fake-access-token-0001", REFRESH_B, str(HOME_B)):
        assert secret not in caplog.text


async def test_exchanges_are_recorded(
    mock_tado: TadoCloudMock, transport: Transport, freezer: FrozenDateTimeFactory
) -> None:
    """Every attempt is an exchange with quota numbers and the shape of its body."""
    mock_tado.revoke_access_tokens()
    await transport.request("GET", ROOMS_B, endpoint="rooms")
    await transport.request("DELETE", f"{ROOMS_B}/2/manualControl")

    retried, rooms, deleted = transport.telemetry.exchanges()
    assert (retried.attempt, retried.status, retried.outcome, retried.error) == (
        1,
        401,
        "retried",
        "HTTP 401",
    )
    assert retried.shape is None
    assert (rooms.at, rooms.method, rooms.endpoint) == (datetime.now(UTC), "GET", ROOMS)
    assert (rooms.attempt, rooms.status, rooms.outcome, rooms.error) == (
        2,
        200,
        "ok",
        None,
    )
    assert (rooms.limit, rooms.remaining, rooms.reset_in_s) == (20000, 17831, None)
    assert isinstance(rooms.shape, dict)
    assert rooms.shape["list"] == 6
    assert rooms.shape["item"]["name"] == "<redacted>"
    assert rooms.shape["item"]["id"] == "int"
    assert (deleted.endpoint, deleted.status, deleted.shape) == (
        "DELETE hops:/homes/{home}/rooms/{room}/manualControl",
        204,
        None,
    )
    assert transport.telemetry.requests_by_endpoint() == {
        "DELETE hops:/homes/{home}/rooms/{room}/manualControl": 1,
        ROOMS: 2,
    }


async def test_reset_time_is_recorded(
    mock_tado: TadoCloudMock,
    transport: Transport,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ratelimit header with t= gives the seconds until the reset."""
    mock_tado.set_quota(remaining=100, reset_in=7082)
    caplog.set_level(logging.DEBUG)
    await transport.request("GET", ROOMS_B)
    [exchange] = transport.telemetry.exchanges()
    assert (exchange.remaining, exchange.reset_in_s) == (99, 7082)
    assert (
        logging.DEBUG,
        f"{ROOMS} -> 200 in N ms, quota 99/20000 left, reset in 7082 s",
    ) in _client_log(caplog)


async def test_error_answers_log_code_and_title_only(
    mock_tado: TadoCloudMock, transport: Transport, caplog: pytest.LogCaptureFixture
) -> None:
    """An error body full of secrets logs the redacted code and title, nothing else."""
    body = {
        "errors": [
            {
                "code": "invalid.request",
                "title": f"Token {ACCESS_B} of user@example.invalid is invalid",
                "detail": "Musterstrasse 1, 00000 Musterstadt",
            }
        ],
        "token": REFRESH_B,
    }
    mock_tado.fail("GET", ROOMS_B, status=400, body=body)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TadoXRequestError) as err:
        await transport.request("GET", ROOMS_B)

    details = "code=invalid.request title=Token <redacted> of <email> is invalid"
    assert _client_log(caplog) == [
        (
            logging.DEBUG,
            f"{ROOMS} -> 400 in N ms, quota 17832/20000 left: "
            f"TadoXRequestError ({details})",
        )
    ]
    assert str(err.value) == f"tado answered HTTP 400: {details}"
    assert err.value.request == ROOMS
    [exchange] = transport.telemetry.exchanges()
    assert (exchange.outcome, exchange.error, exchange.shape) == (
        "TadoXRequestError",
        details,
        None,
    )
    for secret in (ACCESS_B, REFRESH_B, "user@example", "Musterstr", str(HOME_B)):
        assert secret not in caplog.text


async def test_home_not_x_is_logged_without_the_home_id(
    mock_tado: TadoCloudMock, transport: Transport, caplog: pytest.LogCaptureFixture
) -> None:
    """The 404 title of a non-X home names the home only as "#"."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TadoXHomeNotXError) as err:
        await transport.request("GET", f"{HOPS_API_URL}/homes/{HOME_A}/rooms")

    details = "code=not.found title=Timezone of home # not found"
    assert _client_log(caplog) == [
        (
            logging.DEBUG,
            f"{ROOMS} -> 404 in N ms, quota 17832/20000 left: "
            f"TadoXHomeNotXError ({details})",
        )
    ]
    assert str(err.value) == f"tado answered HTTP 404: {details}"
    assert str(HOME_A) not in caplog.text


async def test_connection_error_is_recorded(
    mock_tado: TadoCloudMock, transport: Transport, caplog: pytest.LogCaptureFixture
) -> None:
    """No answer is an exchange without status that names the cause by its class."""
    mock_tado.fail("GET", STATE_B, exc=TimeoutError("timeout of 192.168.100.200"))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TadoXConnectionError) as err:
        await transport.request("GET", STATE_B)

    [exchange] = transport.telemetry.exchanges()
    assert (exchange.status, exchange.outcome, exchange.error) == (
        None,
        "TadoXConnectionError",
        "TimeoutError",
    )
    assert _client_log(caplog) == [
        (
            logging.DEBUG,
            "GET my:/homes/{home}/state -> no answer in N ms: "
            "TadoXConnectionError (TimeoutError)",
        )
    ]
    assert err.value.request == "GET my:/homes/{home}/state"
    assert "192.168" not in caplog.text


async def test_errors_name_their_request(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Errors carry the template of the request that failed, the token one too."""
    telemetry = Telemetry()
    quota = QuotaTracker(telemetry=telemetry)
    stale = TokenSet(
        access_token=ACCESS_B, refresh_token=REFRESH_B, expires_at=datetime.now(UTC)
    )
    tokens = TokenManager(session, MemoryTokenStore(stale), telemetry=telemetry)
    transport = Transport(session, tokens, quota, telemetry=telemetry)
    assert transport.telemetry is telemetry
    aioclient_mock.post(TOKEN_URL, status=503)
    aioclient_mock.get(ROOMS_B, text="[{]")

    with pytest.raises(TadoXServerError) as refresh:
        await transport.request("GET", ROOMS_B)
    assert refresh.value.request == "POST login:/oauth2/token"

    fresh = TokenManager(
        session,
        MemoryTokenStore(
            TokenSet(
                access_token=ACCESS_B,
                refresh_token=REFRESH_B,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        ),
    )
    transport = Transport(session, fresh, quota, telemetry=telemetry)
    with pytest.raises(TadoXParseError) as parse:
        await transport.request("GET", ROOMS_B, params={"date": "2026-10-30"})
    assert parse.value.request == f"{ROOMS}?date"

    quota.observe(None, status=429)
    with pytest.raises(TadoXRateLimitError) as refused:
        await transport.request("GET", ROOMS_B)
    assert refused.value.request == ROOMS

    login, rooms = telemetry.exchanges()
    assert (login.endpoint, login.outcome, login.error) == (
        "POST login:/oauth2/token",
        "TadoXServerError",
        "HTTP 503",
    )
    assert (rooms.endpoint, rooms.outcome, rooms.error) == (
        f"{ROOMS}?date",
        "TadoXParseError",
        "invalid JSON",
    )
    assert [record.context for record in telemetry.errors()] == [
        "token refresh",
        "quota",
    ]


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
    ("name", "endpoint"),
    [("rooms", "rooms"), (None, ROOMS)],
)
async def test_invalid_json_is_a_parse_error(
    aioclient_mock: AiohttpClientMocker,
    transport: Transport,
    name: str | None,
    endpoint: str,
) -> None:
    """A 2xx whose body is not JSON fails as a parse error named by its endpoint.

    Without an endpoint key the template names it, never the URL path.
    """
    aioclient_mock.get(ROOMS_B, text="[{]")
    with pytest.raises(TadoXParseError) as err:
        await transport.request("GET", ROOMS_B, endpoint=name)
    assert err.value.endpoint == endpoint
    assert err.value.__cause__ is None
    assert "JSONDecodeError" not in "".join(traceback.format_exception(err.value))


async def test_parse_error_of_a_write_holds_no_ids(
    aioclient_mock: AiohttpClientMocker, transport: Transport
) -> None:
    """A device write answered with a page instead of JSON names no serial or home."""
    aioclient_mock.patch(DEVICE_B, text="<html>OK</html>")

    with pytest.raises(TadoXParseError) as err:
        await transport.command("PATCH", DEVICE_B, json={"childLockEnabled": True})

    template = "PATCH hops:/homes/{home}/roomsAndDevices/devices/{serial}"
    assert (err.value.endpoint, err.value.path) == (template, "$")
    assert str(err.value) == f"unexpected {template} payload at $"
    trace = "".join(traceback.format_exception(err.value))
    for secret in ("VA0000000101", "0000000101", str(HOME_B)):
        assert secret not in trace


async def test_publish(
    transport: Transport, observed: Observed, freezer: FrozenDateTimeFactory
) -> None:
    """A published payload reaches the observer with the time it was published."""
    transport.publish(HOME_B, "rooms", [{"id": 1}])
    assert observed == [(HOME_B, "rooms", [{"id": 1}], datetime.now(UTC))]

    transport.set_payload_observer(None)
    transport.publish(HOME_B, "rooms", [])
    assert len(observed) == 1


async def test_energy_iq_refusal_in_a_2xx(
    mock_tado: TadoCloudMock, transport: Transport
) -> None:
    """Energy IQ may refuse with a 2xx that carries a message and no id (API §5.2)."""
    url = f"{ENERGY_IQ_API_URL}/homes/{HOME_B}/meterReadings"
    body = {"date": "2026-10-30", "reading": 1}
    reason = "Reading must be greater than previous reading"
    mock_tado.fail("POST", url, status=200, body={"message": reason}, times=1)

    with pytest.raises(TadoXRequestError) as err:
        await transport.request("POST", url, json=body, priority=Priority.COMMAND)

    assert (err.value.status, err.value.code, err.value.title) == (200, None, reason)
    stored = await transport.request("POST", url, json=body, priority=Priority.COMMAND)
    assert stored["reading"] == 1


@pytest.mark.parametrize("message", [None, 5])
async def test_energy_iq_refusal_without_text(
    mock_tado: TadoCloudMock, transport: Transport, message: object
) -> None:
    """A refusal whose message is not text still fails, without a title."""
    url = f"{ENERGY_IQ_API_URL}/homes/{HOME_B}/tariffs"
    mock_tado.fail("POST", url, status=201, body={"message": message})

    with pytest.raises(TadoXRequestError) as err:
        await transport.request("POST", url, json={}, priority=Priority.COMMAND)

    assert (err.value.status, err.value.title) == (201, None)


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (
            f"{ENERGY_IQ_API_URL}/homes/{HOME_B}/tariffs",
            {"id": "00000000-0000-4000-9000-000000000001", "message": "stored"},
        ),
        (f"{ENERGY_IQ_API_URL}/homes/{HOME_B}/tariffs", [{"message": "a list"}]),
        (ROOMS_B, {"message": "not Energy IQ"}),
    ],
    ids=["with_id", "list", "other_host"],
)
async def test_messages_that_are_no_refusal(
    mock_tado: TadoCloudMock, transport: Transport, url: str, body: Any
) -> None:
    """An answer with an id, a list or another host's message is plain data."""
    mock_tado.fail("*", url, status=200, body=body)

    assert await transport.request("POST", url, priority=Priority.COMMAND) == body

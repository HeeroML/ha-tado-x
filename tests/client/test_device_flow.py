"""Tests for the device-code login of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientConnectionError, ClientSession
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.tado_x.client import (
    CLIENT_ID,
    DeviceAuthorization,
    DeviceCodeFlow,
    TadoXAuthError,
    TadoXConnectionError,
    TadoXDeviceFlowError,
    TadoXParseError,
    TadoXRequestError,
    TadoXServerError,
)
from custom_components.tado_x.client.const import DEVICE_AUTHORIZE_URL, TOKEN_URL
from tests.tado_mock import TadoCloudMock

DEVICE_CODE = "fake-device-code-0000000000000000"


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


class FakeSleep:
    """Injected sleep: records the delays, moves the frozen clock, runs hooks."""

    def __init__(self, freezer: FrozenDateTimeFactory) -> None:
        """Initialize with the frozen clock."""
        self.delays: list[float] = []
        self.hooks: dict[int, Callable[[], None]] = {}
        self._freezer = freezer

    async def __call__(self, seconds: float) -> None:
        """Record the delay and let the time pass."""
        self.delays.append(seconds)
        self._freezer.tick(timedelta(seconds=seconds))
        if (hook := self.hooks.get(len(self.delays))) is not None:
            hook()


@pytest.fixture
def sleep(freezer: FrozenDateTimeFactory) -> FakeSleep:
    """Return the injected sleep."""
    return FakeSleep(freezer)


@pytest.fixture
def flow(session: ClientSession, sleep: FakeSleep) -> DeviceCodeFlow:
    """Return a device-code flow with the injected sleep."""
    return DeviceCodeFlow(session, sleep=sleep)


def _auth(interval: float = 5, lifetime: float = 300) -> DeviceAuthorization:
    now = datetime.now(UTC)
    return DeviceAuthorization(
        device_code=DEVICE_CODE,
        user_code="ABC123",
        verification_url="https://login.tado.com/oauth2/device",
        interval=timedelta(seconds=interval),
        expires_at=now + timedelta(seconds=lifetime),
    )


async def test_start(mock_tado: TadoCloudMock, flow: DeviceCodeFlow) -> None:
    """The verification URL carries the user code and the client id (#76)."""
    now = datetime.now(UTC)
    auth = await flow.async_start()

    [call] = mock_tado.calls("POST", DEVICE_AUTHORIZE_URL)
    assert call.body == {"client_id": CLIENT_ID, "scope": "offline_access"}
    url = URL(auth.verification_url)
    assert url.with_query(None) == URL("https://login.tado.com/oauth2/device")
    assert dict(url.query) == {"user_code": "ABC123", "client_id": CLIENT_ID}
    assert auth.user_code == "ABC123"
    assert auth.device_code == DEVICE_CODE
    assert auth.interval == timedelta(seconds=5)
    assert auth.expires_at == now + timedelta(seconds=300)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ({"status": 503}, TadoXServerError),
        ({"status": 429}, TadoXServerError),
        ({"exc": ClientConnectionError("down")}, TadoXConnectionError),
        ({"status": 200, "body": {"user_code": "ABC123"}}, TadoXParseError),
    ],
)
async def test_start_failures(
    mock_tado: TadoCloudMock,
    flow: DeviceCodeFlow,
    failure: dict[str, Any],
    error: type[Exception],
) -> None:
    """Transient failures and garbage at the start raise the matching error."""
    mock_tado.fail("POST", DEVICE_AUTHORIZE_URL, **failure)
    with pytest.raises(error):
        await flow.async_start()


async def test_start_rejected(mock_tado: TadoCloudMock, flow: DeviceCodeFlow) -> None:
    """A 4xx at the start is a request error with tado's OAuth error."""
    mock_tado.fail(
        "POST",
        DEVICE_AUTHORIZE_URL,
        status=400,
        body={"error": "invalid_request", "error_description": "missing client_id"},
    )
    with pytest.raises(TadoXRequestError) as err:
        await flow.async_start()
    assert (err.value.status, err.value.code, err.value.title) == (
        400,
        "invalid_request",
        "missing client_id",
    )

    mock_tado.fail("POST", DEVICE_AUTHORIZE_URL, status=403, body={"error": 7})
    with pytest.raises(TadoXRequestError) as err:
        await flow.async_start()
    assert (err.value.code, err.value.title) == (None, None)


async def test_wait_until_confirmed(
    mock_tado: TadoCloudMock, flow: DeviceCodeFlow, sleep: FakeSleep
) -> None:
    """Pending answers are polled at the server interval until the login is done."""
    mock_tado.device_flow_pending = 2
    tokens = await flow.async_wait_for_token(_auth())

    assert sleep.delays == [5, 5, 5]
    assert tokens.access_token == "fake-access-token-0001"
    assert tokens.refresh_token == "fake-refresh-token-0001"
    assert tokens.expires_at == datetime.now(UTC) + timedelta(seconds=599)
    calls = mock_tado.calls("POST", TOKEN_URL)
    assert len(calls) == 3
    assert calls[0].body == {
        "client_id": CLIENT_ID,
        "device_code": DEVICE_CODE,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }


@pytest.mark.parametrize(("interval", "expected"), [(7, 7), (2, 5)])
async def test_server_interval_is_honoured(
    mock_tado: TadoCloudMock,
    flow: DeviceCodeFlow,
    sleep: FakeSleep,
    interval: float,
    expected: float,
) -> None:
    """The server's interval is used, but never below 5 seconds."""
    await flow.async_wait_for_token(_auth(interval=interval))
    assert sleep.delays == [expected]


async def test_slow_down(
    mock_tado: TadoCloudMock, flow: DeviceCodeFlow, sleep: FakeSleep
) -> None:
    """slow_down adds 5 seconds to the interval for every later poll."""
    mock_tado.device_flow_pending = 1
    sleep.hooks[2] = lambda: mock_tado.fail(
        "POST", TOKEN_URL, status=400, body={"error": "slow_down"}, times=1
    )
    await flow.async_wait_for_token(_auth())
    assert sleep.delays == [5, 5, 10]


@pytest.mark.parametrize(
    ("error", "reason"), [("expired_token", "expired"), ("access_denied", "denied")]
)
async def test_flow_ends_without_token(
    mock_tado: TadoCloudMock, flow: DeviceCodeFlow, error: str, reason: str
) -> None:
    """An expired code or a denied login ends the flow."""
    mock_tado.device_flow_error = error
    with pytest.raises(TadoXDeviceFlowError) as err:
        await flow.async_wait_for_token(_auth())
    assert err.value.reason == reason


async def test_code_expires_while_pending(
    mock_tado: TadoCloudMock, flow: DeviceCodeFlow, sleep: FakeSleep
) -> None:
    """Past the code's lifetime the flow ends as expired without another poll."""
    mock_tado.device_flow_pending = 1000
    with pytest.raises(TadoXDeviceFlowError) as err:
        await flow.async_wait_for_token(_auth(lifetime=20))
    assert err.value.reason == "expired"
    assert len(mock_tado.calls("POST", TOKEN_URL)) == 3
    assert sleep.delays == [5, 5, 5, 5]


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ({"exc": ClientConnectionError("down")}, TadoXConnectionError),
        ({"status": 503}, TadoXServerError),
    ],
)
async def test_transient_errors_are_retried_until_the_deadline(
    mock_tado: TadoCloudMock,
    flow: DeviceCodeFlow,
    failure: dict[str, Any],
    error: type[Exception],
) -> None:
    """Transient failures are retried; at the deadline the last one is raised."""
    mock_tado.fail("POST", TOKEN_URL, **failure)
    with pytest.raises(error):
        await flow.async_wait_for_token(_auth(lifetime=20))
    assert len(mock_tado.calls("POST", TOKEN_URL)) == 3


async def test_transient_error_then_success(
    mock_tado: TadoCloudMock, flow: DeviceCodeFlow
) -> None:
    """A transient failure in between does not end the flow."""
    mock_tado.fail("POST", TOKEN_URL, status=502, times=1)
    tokens = await flow.async_wait_for_token(_auth())
    assert tokens.access_token == "fake-access-token-0001"


@pytest.mark.parametrize(
    ("body", "reason"),
    [({"error": "invalid_grant"}, "invalid_grant"), ({}, "http_400")],
)
async def test_unknown_rejection(
    mock_tado: TadoCloudMock,
    flow: DeviceCodeFlow,
    body: dict[str, Any],
    reason: str,
) -> None:
    """Any other rejection of the device code is an auth error."""
    mock_tado.fail("POST", TOKEN_URL, status=400, body=body)
    with pytest.raises(TadoXAuthError) as err:
        await flow.async_wait_for_token(_auth())
    assert (err.value.reason, err.value.status) == (reason, 400)

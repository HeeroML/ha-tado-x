"""Self-tests of the fake tado cloud the other tests rely on."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from aiohttp import ClientConnectionError, ClientSession
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tado_x.client.const import (
    CLIENT_ID,
    HOPS_API_URL,
    MY_API_URL,
    TOKEN_URL,
)
from tests.tado_mock import HOME_A, HOME_B, HOME_C, TadoCloudMock, entry_tokens

ROOMS_B = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"
AUTH_B = {"Authorization": f"Bearer {entry_tokens(HOME_B)[0]}"}


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session: ClientSession = aioclient_mock.create_session(
        asyncio.get_running_loop()
    )
    yield client_session
    await client_session.close()


async def _request(
    session: ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> tuple[int, dict[str, str], Any]:
    async with session.request(method, url, headers=headers, **kwargs) as response:
        body = await response.read()
        return (
            response.status,
            dict(response.headers),
            await response.json() if body else None,
        )


async def _refresh(session: ClientSession, refresh_token: str) -> tuple[int, Any]:
    status, _, body = await _request(
        session,
        "POST",
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    return status, body


async def test_refresh_token_rotation(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """A refresh rotates the refresh token; reusing the old one is invalid_grant."""
    old = entry_tokens(HOME_B)[1]
    status, body = await _refresh(session, old)
    assert status == 200
    assert body["refresh_token"] != old
    assert body["expires_in"] == 599

    status, body = await _refresh(session, old)
    assert status == 400
    assert body["error"] == "invalid_grant"

    status, _ = await _refresh(session, entry_tokens(HOME_C)[1])
    assert status == 200


async def test_device_flow(mock_tado: TadoCloudMock, session: ClientSession) -> None:
    """The device grant is pending as configured, then succeeds."""
    mock_tado.device_flow_pending = 1
    form = {
        "client_id": CLIENT_ID,
        "device_code": "fake-device-code-0000000000000000",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    status, _, body = await _request(session, "POST", TOKEN_URL, data=form)
    assert (status, body["error"]) == (400, "authorization_pending")
    status, _, body = await _request(session, "POST", TOKEN_URL, data=form)
    assert status == 200
    assert body["access_token"] in mock_tado.valid_access_tokens

    status, _, body = await _request(
        session, "POST", TOKEN_URL, data={**form, "client_id": "other"}
    )
    assert (status, body["error_reason"]) == (400, "missing_client_id")


async def test_bearer_and_ratelimit_headers(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """API calls need a valid bearer token and carry the quota headers."""
    status, headers, rooms = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 200
    assert len(rooms) == 6
    assert headers["ratelimit"] == '"perday";r=17832'
    assert headers["ratelimit-policy"] == '"perday";q=20000;w=86400'

    status, headers, _ = await _request(
        session, "GET", f"{MY_API_URL}/me", headers={"Authorization": "Bearer nope"}
    )
    assert status == 401
    assert headers["RateLimit"] == '"perday";r=17831'

    mock_tado.revoke_access_tokens()
    status, _, _ = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 401


async def test_quota_exhaustion_and_reset(
    mock_tado: TadoCloudMock, session: ClientSession, freezer: FrozenDateTimeFactory
) -> None:
    """The last call shows r=0 and t=, then 429 until the quota refills."""
    mock_tado.set_quota(limit=100, remaining=1, reset_in=timedelta(seconds=600))
    status, headers, _ = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 200
    assert headers["ratelimit"] == '"perday";r=0;t=600'

    freezer.tick(timedelta(seconds=100))
    status, headers, _ = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 429
    assert headers["ratelimit"] == '"perday";r=0;t=500'

    freezer.tick(timedelta(seconds=500))
    status, headers, _ = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 200
    assert headers["ratelimit"] == '"perday";r=99'


async def test_v3_home_on_hops(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """The hops API answers 404 "Timezone of home" for a non-X home (#40)."""
    url = f"{HOPS_API_URL}/homes/{HOME_A}/rooms"
    status, _, body = await _request(session, "GET", url, headers=AUTH_B)
    assert status == 404
    assert body["errors"][0]["title"] == f"Timezone of home {HOME_A} not found"

    status, _, info = await _request(
        session, "GET", f"{MY_API_URL}/homes/{HOME_A}", headers=AUTH_B
    )
    assert (status, info["generation"]) == (200, "PRE_LINE_X")


async def test_me_lists_the_account_homes(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """/me lists the configured homes of the account."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    _, _, me = await _request(session, "GET", f"{MY_API_URL}/me", headers=AUTH_B)
    assert [home["name"] for home in me["homes"]] == ["Haus A", "Haus B", "Haus C"]


async def test_second_x_home(mock_tado: TadoCloudMock, session: ClientSession) -> None:
    """Home 1000003 has the same room ids and its own serial numbers."""
    url = f"{HOPS_API_URL}/homes/{HOME_C}/roomsAndDevices"
    _, _, topology = await _request(session, "GET", url, headers=AUTH_B)
    assert [room["roomId"] for room in topology["rooms"]] == [1, 2, 3, 4, 5, 6]
    assert topology["rooms"][0]["devices"][0]["serialNumber"] == "VA0000000201"
    assert topology["otherDevices"][0]["serialNumber"] == "IB0000000002"
    _, _, info = await _request(
        session, "GET", f"{MY_API_URL}/homes/{HOME_C}", headers=AUTH_B
    )
    assert (info["id"], info["name"]) == (HOME_C, "Haus C")


async def test_writes_change_state(
    mock_tado: TadoCloudMock, session: ClientSession
) -> None:
    """Commands change what later reads return, and calls are recorded."""
    body = {
        "setting": {"power": "ON", "isBoost": False, "temperature": {"value": 22.5}},
        "termination": {"type": "TIMER", "durationInSeconds": 3600},
    }
    status, _, _ = await _request(
        session, "POST", f"{ROOMS_B}/1/manualControl", headers=AUTH_B, json=body
    )
    assert status == 204
    room = mock_tado.home(HOME_B).room(1)
    assert room is not None
    assert room["setting"] == {"power": "ON", "temperature": {"value": 22.5}}
    assert room["manualControlTermination"]["type"] == "TIMER"

    patch_url = f"{HOPS_API_URL}/homes/{HOME_B}/roomsAndDevices/devices/VA0000000101"
    await _request(
        session, "PATCH", patch_url, headers=AUTH_B, json={"temperatureOffset": 0.5}
    )
    device = mock_tado.home(HOME_B).device("VA0000000101")
    assert device is not None
    assert device["temperatureOffset"] == 0.5

    status, _, _ = await _request(
        session, "POST", f"{ROOMS_B}/1/openWindow", headers=AUTH_B
    )
    assert status == 422

    await _request(
        session,
        "PUT",
        f"{MY_API_URL}/homes/{HOME_B}/presenceLock",
        headers=AUTH_B,
        json={"homePresence": "AWAY"},
    )
    assert mock_tado.home(HOME_B).state == {"presence": "AWAY", "presenceLocked": True}

    [call] = mock_tado.calls("POST", "/rooms/1/manualControl")
    assert call.body == body
    assert call.headers["Authorization"] == AUTH_B["Authorization"]
    assert len(mock_tado.calls("PATCH")) == 1
    mock_tado.clear_calls()
    assert mock_tado.calls() == []


async def test_fail_and_times(mock_tado: TadoCloudMock, session: ClientSession) -> None:
    """Injected failures apply the given number of times; exceptions are raised."""
    mock_tado.fail("GET", "/rooms", status=503, times=1)
    assert (await _request(session, "GET", ROOMS_B, headers=AUTH_B))[0] == 503
    assert (await _request(session, "GET", ROOMS_B, headers=AUTH_B))[0] == 200

    mock_tado.fail("GET", "/rooms", status=429)
    status, headers, _ = await _request(session, "GET", ROOMS_B, headers=AUTH_B)
    assert status == 429
    assert headers["ratelimit"] == '"perday";r=0;t=7082'
    mock_tado.clear_failures()

    mock_tado.fail(None, TOKEN_URL, exc=ClientConnectionError("down"), times=1)
    with pytest.raises(ClientConnectionError):
        await _refresh(session, entry_tokens(HOME_B)[1])
    assert (await _refresh(session, entry_tokens(HOME_B)[1]))[0] == 200

    with pytest.raises(ValueError):
        mock_tado.fail("GET", "/rooms")


async def test_hold(mock_tado: TadoCloudMock, session: ClientSession) -> None:
    """A held request waits until the event is set."""
    release = mock_tado.hold("GET", "/rooms")
    task = asyncio.create_task(_request(session, "GET", ROOMS_B, headers=AUTH_B))
    await asyncio.sleep(0)
    assert len(mock_tado.calls("GET", "/rooms")) == 1
    assert not task.done()
    release.set()
    assert (await task)[0] == 200

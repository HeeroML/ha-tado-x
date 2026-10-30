"""The config flow driven through the HTTP API of the frontend (#73).

#73 is an HTTP 500 from /api/config/config_entries/flow. These tests run the
flow through that view, with errors injected, and require HTTP 200 for every
step: the view also serializes each form schema, which is where a schema with
a lambda fails.
"""

from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast

from aiohttp import ClientConnectionError, web
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.tado_x.const import CONF_HOME_ID, DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.tado_mock import HOME_A, HOME_B, HOME_C, TadoCloudMock

type Client = TestClient[web.Request, web.Application]

FLOW_URL = "/api/config/config_entries/flow"
OPTIONS_FLOW_URL = "/api/config/config_entries/options/flow"


@pytest.fixture
async def client(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> Client:
    """Return an HTTP client of the Home Assistant API with the config views."""
    assert await async_setup_component(hass, "config", {})
    return cast(Client, await hass_client())


async def _request(
    client: Client, method: str, url: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send a request and require HTTP 200."""
    response = await client.request(method, url, json=body)
    assert response.status == HTTPStatus.OK, await response.text()
    result: dict[str, Any] = await response.json()
    return result


async def _sign_in(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Confirm the login; the device-code poll then succeeds."""
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_user_flow_over_http(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    client: Client,
) -> None:
    """Start, wait and finish the flow as the frontend does."""
    result = await _request(client, "POST", FLOW_URL, {"handler": DOMAIN})
    assert result["type"] == "progress"
    assert result["progress_action"] == "wait_for_device"
    flow_url = f"{FLOW_URL}/{result['flow_id']}"

    assert (await _request(client, "GET", flow_url))["type"] == "progress"
    await _sign_in(hass, freezer)
    result = await _request(client, "GET", flow_url)

    assert result["type"] == "create_entry"
    assert result["title"] == "Haus B"
    assert "data" not in result


async def test_select_home_over_http(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    client: Client,
) -> None:
    """The home selection form serializes and accepts the chosen home."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    result = await _request(client, "POST", FLOW_URL, {"handler": DOMAIN})
    flow_url = f"{FLOW_URL}/{result['flow_id']}"
    await _sign_in(hass, freezer)

    result = await _request(client, "GET", flow_url)
    assert result["type"] == "form"
    assert result["step_id"] == "select_home"
    assert [field["name"] for field in result["data_schema"]] == [CONF_HOME_ID]

    result = await _request(client, "POST", flow_url, {CONF_HOME_ID: str(HOME_C)})
    assert result["type"] == "create_entry"
    assert result["title"] == "Haus C"


@pytest.mark.parametrize(
    ("method", "path", "failure", "reason"),
    [
        ("POST", "/oauth2/device_authorize", {"status": 429}, "rate_limited"),
        ("POST", "/oauth2/device_authorize", {"status": 500}, "cannot_connect"),
        (
            "POST",
            "/oauth2/device_authorize",
            {"exc": RuntimeError("boom")},
            "unknown",
        ),
        ("POST", "/oauth2/token", {"exc": RuntimeError("boom")}, "unknown"),
        ("GET", "/api/v2/me", {"status": 429}, "rate_limited"),
        ("GET", "/api/v2/me", {"exc": TimeoutError()}, "cannot_connect"),
        ("GET", "/api/v2/me", {"exc": ClientConnectionError()}, "cannot_connect"),
        ("GET", "/api/v2/me", {"status": 500}, "cannot_connect"),
        ("GET", "/api/v2/me", {"status": 200, "body": "not json"}, "unknown"),
        ("GET", f"/api/v2/homes/{HOME_B}", {"status": 429}, "rate_limited"),
        ("GET", f"/api/v2/homes/{HOME_B}", {"status": 500}, "cannot_connect"),
        ("GET", f"/api/v2/homes/{HOME_B}", {"status": 200, "body": []}, "unknown"),
    ],
)
async def test_errors_answer_200_over_http(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    client: Client,
    method: str,
    path: str,
    failure: dict[str, Any],
    reason: str,
) -> None:
    """Every injected failure ends in an abort with HTTP 200, never a 500 (#73)."""
    mock_tado.fail(method, path, **failure)

    result = await _request(client, "POST", FLOW_URL, {"handler": DOMAIN})
    if result["type"] == "progress":
        await _sign_in(hass, freezer)
        result = await _request(client, "GET", f"{FLOW_URL}/{result['flow_id']}")

    assert result["type"] == "abort"
    assert result["reason"] == reason


async def test_options_flow_over_http(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    client: Client,
    config_entry: MockConfigEntry,
) -> None:
    """The options form with its sections serializes, and a change is stored."""
    config_entry.add_to_hass(hass)

    result = await _request(
        client, "POST", OPTIONS_FLOW_URL, {"handler": config_entry.entry_id}
    )

    assert result["type"] == "form"
    assert [field["name"] for field in result["data_schema"]] == [
        "polling",
        "features",
        "control",
    ]
    assert all(field["type"] == "expandable" for field in result["data_schema"])

    user_input = {
        "polling": {"daily_call_budget": 2500, "quota_reserve": 1000},
        "features": {
            "weather": True,
            "air_comfort": True,
            "running_times": True,
            "mobile_devices": False,
            "holiday_mode": False,
        },
        "control": {
            "manual_termination": "follow_app",
            "timer_minutes": 60,
            "off_follows_policy": False,
            "presence_zone": "zone.home",
        },
    }
    result = await _request(
        client, "POST", f"{OPTIONS_FLOW_URL}/{result['flow_id']}", user_input
    )

    assert result["type"] == "create_entry"
    assert config_entry.options["daily_call_budget"] == 2500

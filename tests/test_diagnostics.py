"""Tests for the diagnostics of a tado X entry."""

from datetime import timedelta
import json
import re

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from custom_components.tado_x.devices import (
    device_identifier,
    home_identifier,
    room_identifier,
)
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from tests.tado_mock import HOME_B, USER_ID, TadoCloudMock
from tests.test_climate import climate_id
from tests.test_init import make_entry

START = "2026-10-30T10:00:00+00:00"
SECRETS = (
    "fake-access-token",
    "fake-refresh-token",
    USER_ID,
    "user@example.invalid",
    "Musterstrasse",
    "Phone",
    "VA0000000101",
    "IB0000000001",
)
PRIVATE = (
    "fake-access-token-9999",
    "Bearer",
    "Wohnzimmer",
    "Kueche",
    "Musterstadt",
    "Haus B",
    "47.123456",
    f"home {HOME_B}",
)
FULL_SERIAL = re.compile(r"\b(?:VA|SU|IB)\d{10}\b")
NASTY_BODY = {
    "errors": [
        {
            "code": "not.allowed",
            "title": f"Token fake-access-token-9999 of user@example.invalid in home {HOME_B}",
            "detail": "Wohnzimmer, Musterstrasse 1, 47.123456, VA0000000101",
        }
    ]
}
NASTY_TEXT = (
    "<html>Bearer fake-access-token-9999 user@example.invalid Wohnzimmer "
    "Musterstrasse 1 47.123456 VA0000000101 Haus B</html>"
)


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the time before the HTTP client gets its access token."""
    freezer.move_to(START)


@pytest.fixture
async def entry(hass: HomeAssistant, mock_tado: TadoCloudMock) -> MockConfigEntry:
    """Set up home 1000002 with phones at a fixed time."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "mobile_devices": True}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entry_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Diagnostics show quota, plan and payloads without secrets or phones.

    The entry title is tado's name of the home, so the label stands for it.
    """
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics == snapshot
    assert diagnostics["entry"]["title"] == "home …0002"
    text = str(diagnostics)
    assert [secret for secret in SECRETS if secret in text] == []
    assert entry.title not in text
    assert "mobile_devices" not in diagnostics["payloads"]


@pytest.mark.parametrize(
    "identifier",
    [
        device_identifier("VA0000000101"),
        room_identifier(HOME_B, 1),
        home_identifier(HOME_B),
    ],
    ids=["valve", "room", "home"],
)
async def test_device_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
    identifier: tuple[str, str],
) -> None:
    """Device diagnostics add the topology entry of the device."""
    device = device_registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None

    diagnostics = await get_diagnostics_for_device(hass, hass_client, entry, device)

    assert diagnostics["device"] == snapshot
    text = str(diagnostics)
    assert [secret for secret in SECRETS if secret in text] == []
    assert entry.title not in text


async def test_diagnostics_after_errors_hold_no_secret(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    entry: MockConfigEntry,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
) -> None:
    """Errors with nasty bodies leave only templates and redacted texts behind."""
    data = entry.runtime_data
    mock_tado.fail("POST", "/manualControl", status=422, body=NASTY_BODY, times=1)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: climate_id(hass, 1), "temperature": 21},
            blocking=True,
        )
    rooms = f"/homes/{HOME_B}/rooms"
    mock_tado.fail("GET", rooms, status=422, body=NASTY_BODY, times=1)
    freezer.tick(timedelta(seconds=60))
    await data.rooms.async_refresh()
    mock_tado.fail("GET", rooms, status=503, body=NASTY_TEXT, times=1)
    freezer.tick(timedelta(seconds=60))
    await data.rooms.async_refresh()
    mock_tado.fail("GET", "/state", exc=ClientConnectionError(NASTY_TEXT), times=1)
    freezer.tick(timedelta(seconds=300))
    await data.home.async_refresh()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    text = json.dumps(diagnostics, ensure_ascii=False)
    assert [secret for secret in (*SECRETS, *PRIVATE) if secret in text] == []
    assert FULL_SERIAL.findall(text) == []
    telemetry = diagnostics["telemetry"]
    assert telemetry["errors"] == snapshot(name="errors")
    assert telemetry["last_error"] == telemetry["errors"][-1]
    assert telemetry["errors_by_kind_24h"] == {
        "TadoXConnectionError": 1,
        "TadoXRequestError": 2,
        "TadoXServerError": 1,
    }

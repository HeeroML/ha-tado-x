"""Tests for two tado X homes of one account with identical room ids (#92)."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.tado_x.devices import device_identifier, room_identifier
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr
from tests.tado_mock import HOME_B, HOME_C, TadoCloudMock, entry_tokens
from tests.test_climate import bodies, climate_id, manual_path
from tests.test_services import call, device_id

TIMER = {"type": "TIMER", "durationInSeconds": 600}


def meter_path(home_id: int) -> str:
    """Return the meter reading path of a home."""
    return f"/api/homes/{home_id}/meterReadings"


async def test_timer_reaches_the_home_of_each_room(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """Room 1 of each home gets one request at its own home's URL (#92)."""
    await call(
        hass,
        "set_climate_timer",
        {"temperature": 20, "duration": 10},
        target={
            ATTR_ENTITY_ID: [climate_id(hass, 1, HOME_B), climate_id(hass, 1, HOME_C)]
        },
    )

    for home_id in (HOME_B, HOME_C):
        assert [
            body["termination"]
            for body in bodies(mock_tado, "POST", manual_path(1, home_id))
        ] == [TIMER]
    assert len(mock_tado.calls("POST")) == 2


async def test_area_target_spans_both_homes(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
    area_registry: ar.AreaRegistry,
) -> None:
    """Both living rooms share an area; each home gets its own request."""
    area = area_registry.async_create("Wohnzimmer")
    for entry in two_home_entries:
        dr.async_get(hass).async_update_device(
            device_id(hass, entry, room_identifier(entry.data[CONF_HOME_ID], 1)),
            area_id=area.id,
        )

    await call(
        hass,
        "set_climate_timer",
        {"temperature": 20, "termination": "manual"},
        target={"area_id": area.id},
    )

    assert len(mock_tado.calls("POST", manual_path(1, HOME_B))) == 1
    assert len(mock_tado.calls("POST", manual_path(1, HOME_C))) == 1
    assert len(mock_tado.calls("POST")) == 2


async def test_offset_reaches_the_home_of_the_device(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """The offset of a valve of home 1000003 goes to that home only."""
    entry = two_home_entries[1]
    serial = "VA0000000201"

    await call(
        hass,
        "set_temperature_offset",
        {
            "device_id": device_id(hass, entry, device_identifier(serial)),
            "offset": 0.5,
        },
    )

    assert [(r.method, r.path) for r in mock_tado.calls("PATCH")] == [
        ("PATCH", f"/homes/{HOME_C}/roomsAndDevices/devices/{serial}")
    ]


async def test_home_services_need_the_entry(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """With two homes the entry must be named, and the call goes to that home."""
    with_c = {"config_entry_id": two_home_entries[1].entry_id}

    await call(
        hass, "add_meter_reading", {"reading": 7, "date": "2026-10-30", **with_c}
    )
    response = await call(hass, "get_holiday_mode", with_c, response=True)
    with pytest.raises(ServiceValidationError):
        await call(hass, "add_meter_reading", {"reading": 8})

    assert bodies(mock_tado, "POST", meter_path(HOME_C)) == [
        {"date": "2026-10-30", "reading": 7}
    ]
    assert mock_tado.calls("POST", meter_path(HOME_B)) == []
    assert response["enabled"] is False
    assert mock_tado.calls("GET", f"/homes/{HOME_C}/settings/holidayMode")


async def test_token_rotation_stays_in_its_entry(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """A refresh in one entry leaves the other entry's grant untouched."""
    entry_b, entry_c = two_home_entries
    tokens_c = {
        key: entry_c.data[key] for key in (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN)
    }
    mock_tado.valid_access_tokens.discard(entry_tokens(HOME_B)[0])

    await call(
        hass,
        "add_meter_reading",
        {"reading": 1, "config_entry_id": entry_b.entry_id},
    )
    await call(
        hass,
        "add_meter_reading",
        {"reading": 2, "config_entry_id": entry_c.entry_id},
    )

    assert entry_b.data[CONF_ACCESS_TOKEN] != entry_tokens(HOME_B)[0]
    assert entry_b.data[CONF_REFRESH_TOKEN] != entry_tokens(HOME_B)[1]
    assert {key: entry_c.data[key] for key in tokens_c} == tokens_c
    request_c = mock_tado.calls("POST", meter_path(HOME_C))[-1]
    assert request_c.headers["Authorization"] == f"Bearer {entry_tokens(HOME_C)[0]}"


async def test_services_reach_a_reloaded_entry(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """Nothing is captured at registration, so a reload changes no target."""
    entry_b = two_home_entries[0]
    assert await hass.config_entries.async_reload(entry_b.entry_id)
    await hass.async_block_till_done()
    assert entry_b.state is ConfigEntryState.LOADED

    await call(
        hass,
        "set_climate_timer",
        {"temperature": 20, "duration": 10},
        target={ATTR_ENTITY_ID: climate_id(hass, 2, HOME_B)},
    )
    await call(
        hass,
        "add_meter_reading",
        {"reading": 3, "config_entry_id": entry_b.entry_id},
    )

    assert len(mock_tado.calls("POST", manual_path(2, HOME_B))) == 1
    assert len(mock_tado.calls("POST", meter_path(HOME_B))) == 1
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []

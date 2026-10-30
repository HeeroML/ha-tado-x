"""Tests for the quick action and identify buttons."""

import copy

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.binary_sensor.const import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_climate import ROOMS, climate_id, entity_id, setup, state_of, tick

QUICK_ACTIONS = f"/homes/{HOME_B}/quickActions"


async def press(hass: HomeAssistant, entity: str) -> None:
    """Press a button and wait for it."""
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity}, blocking=True
    )


@pytest.mark.parametrize(
    ("key", "action"),
    [
        ("boost_all", "boost"),
        ("disable_all", "allOff"),
        ("resume_schedules", "resumeSchedule"),
    ],
)
async def test_quick_actions(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    key: str,
    action: str,
) -> None:
    """Each quick action is one POST, then one debounced read of the rooms."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await press(hass, entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_{key}"))
    await tick(hass, freezer, 5)

    assert [(call.method, call.path) for call in mock_tado.calls()] == [
        ("POST", f"{QUICK_ACTIONS}/{action}"),
        ("GET", ROOMS),
    ]


async def test_all_off_shows_before_the_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """After turning everything off every room shows off at once."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await press(hass, entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_disable_all"))

    assert mock_tado.calls("GET") == []
    for room_id in range(1, 7):
        state = state_of(hass, climate_id(hass, room_id))
        assert state.state == HVACMode.OFF
        assert state.attributes["manual_control_type"] == "manual"

    await tick(hass, freezer, 5)

    assert {state_of(hass, climate_id(hass, room)).state for room in range(1, 7)} == {
        HVACMode.OFF
    }


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_boost_shows_before_the_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """After boosting, every room shows a boost at once, until tado disagrees."""
    await setup(hass, freezer)
    home = mock_tado.home(HOME_B)
    before = copy.deepcopy(home.rooms)
    boosts = {
        room: entity_id(hass, BINARY_SENSOR_DOMAIN, f"{HOME_B}_{room}_boost")
        for room in range(1, 7)
    }
    mock_tado.clear_calls()

    await press(hass, entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_boost_all"))

    assert mock_tado.calls("GET") == []
    assert {state_of(hass, boost).state for boost in boosts.values()} == {STATE_ON}

    home.rooms = before
    await tick(hass, freezer, 5)

    assert {state_of(hass, boost).state for boost in boosts.values()} == {STATE_ON}

    await tick(hass, freezer, 60)

    assert {room: state_of(hass, boost).state for room, boost in boosts.items()} == {
        **dict.fromkeys(range(1, 6), STATE_OFF),
        6: STATE_ON,
    }


async def test_quick_action_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failed quick action raises a translated error and changes nothing."""
    await setup(hass, freezer)
    mock_tado.fail("POST", f"{QUICK_ACTIONS}/allOff", status=429)

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_disable_all"))

    assert raised.value.translation_key == "quota_exhausted"
    assert state_of(hass, climate_id(hass, 1)).state == HVACMode.AUTO


async def test_identify_is_disabled_by_default(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Identify is experimental and off by default; the bridge has none."""
    await setup(hass, freezer)

    identify = entity_registry.async_get(
        entity_id(hass, BUTTON_DOMAIN, "VA0000000101_identify")
    )

    assert identify is not None
    assert identify.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert identify.entity_category is not None
    assert not entity_registry.async_get_entity_id(
        BUTTON_DOMAIN, "tado_x", "IB0000000001_identify"
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_identify_device(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Identify asks the device to flash its display."""
    await setup(hass, freezer)
    identify = entity_id(hass, BUTTON_DOMAIN, "SU0000000102_identify")
    path = f"/homes/{HOME_B}/roomsAndDevices/devices/SU0000000102/identify"
    mock_tado.clear_calls()

    await press(hass, identify)

    assert [(call.method, call.path) for call in mock_tado.calls()] == [("POST", path)]

    mock_tado.fail("POST", path, status=404)
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, identify)
    assert raised.value.translation_key == "not_found"

"""Tests for the offset, termination timer and flow temperature numbers (#51, #94)."""

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.number.const import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_STEP,
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.select.const import DOMAIN as SELECT_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_OPTION,
    ATTR_UNIT_OF_MEASUREMENT,
    SERVICE_SELECT_OPTION,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock, load_fixture
from tests.test_climate import bodies, entity_id, setup, state_of
from tests.test_select import room_path, termination_id

VALVE = "VA0000000101"
DEVICE = f"/homes/{HOME_B}/roomsAndDevices/devices/{VALVE}"
FLOW = f"/homes/{HOME_B}/settings/flowTemperatureOptimization"


def timer_id(hass: HomeAssistant, room_id: int) -> str:
    """Return the entity id of a room's termination timer."""
    return entity_id(
        hass, NUMBER_DOMAIN, f"{HOME_B}_{room_id}_manual_termination_timer"
    )


def offset_id(hass: HomeAssistant, serial: str = VALVE) -> str:
    """Return the entity id of a device's offset number."""
    return entity_id(hass, NUMBER_DOMAIN, f"{serial}_offset")


async def set_value(hass: HomeAssistant, entity: str, value: float) -> None:
    """Set a number and wait for it."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity, ATTR_VALUE: value},
        blocking=True,
    )


def add_boiler(mock_tado: TadoCloudMock) -> None:
    """Give home 1000002 an OpenTherm boiler with flow temperature settings."""
    home = mock_tado.home(HOME_B)
    home.info["supportsFlowTemperatureOptimization"] = True
    home.flow_temperature = load_fixture("flow_temperature.json")


@pytest.mark.parametrize(
    ("room_id", "state"),
    [(1, STATE_UNAVAILABLE), (2, "60.0"), (3, STATE_UNAVAILABLE), (6, "30.0")],
)
async def test_timer_number_only_for_timer_rooms(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    room_id: int,
    state: str,
) -> None:
    """The timer shows tado's duration in minutes while the room uses a timer."""
    await setup(hass, freezer)

    timer = state_of(hass, timer_id(hass, room_id))

    assert timer.state == state
    assert timer.attributes[ATTR_UNIT_OF_MEASUREMENT] == "min"
    assert (timer.attributes[ATTR_MIN], timer.attributes[ATTR_MAX]) == (1, 1440)


async def test_set_timer_writes_the_app_setting(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A new duration is written as a timer default and shown without a read."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await set_value(hass, timer_id(hass, 2), 45)

    assert bodies(mock_tado, "PATCH", room_path(2)) == [
        {"deviceManualControlTermination": {"type": "TIMER", "durationInSeconds": 2700}}
    ]
    assert state_of(hass, timer_id(hass, 2)).state == "45.0"
    assert mock_tado.calls("GET") == []


async def test_timer_appears_when_the_room_switches_to_a_timer(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Choosing timer in the select makes the timer number available."""
    await setup(hass, freezer)

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: termination_id(hass, 1), ATTR_OPTION: "timer"},
        blocking=True,
    )

    assert state_of(hass, timer_id(hass, 1)).state == "60.0"


async def test_timer_after_restart_shows_the_server_value(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Nothing is restored: after a reload the number shows what tado says."""
    entry = await setup(hass, freezer)
    await set_value(hass, timer_id(hass, 2), 45)
    room = mock_tado.home(HOME_B).topology_room(2)
    assert room is not None
    room["deviceManualControlTermination"] = {
        "type": "TIMER",
        "durationInSeconds": 5400,
    }

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, timer_id(hass, 2)).state == "90.0"


async def test_offset_number(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Valves and sensors get an offset number; the bridge has none."""
    await setup(hass, freezer)

    offset = state_of(hass, offset_id(hass))

    assert offset.state == "-1.5"
    assert offset.attributes[ATTR_DEVICE_CLASS] == "temperature_delta"
    assert offset.attributes[ATTR_UNIT_OF_MEASUREMENT] == "°C"
    assert (offset.attributes[ATTR_MIN], offset.attributes[ATTR_MAX]) == (-9.9, 9.9)
    assert offset.attributes[ATTR_STEP] == 0.1
    assert state_of(hass, offset_id(hass, "SU0000000103")).state == "-0.3"
    assert not entity_registry.async_get_entity_id(
        NUMBER_DOMAIN, "tado_x", "IB0000000001_offset"
    )


async def test_set_offset_without_a_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The offset is written with one PATCH and shown at once, with no read (#52)."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await set_value(hass, offset_id(hass), -2.3)

    assert [(call.method, call.path) for call in mock_tado.calls()] == [
        ("PATCH", DEVICE)
    ]
    assert mock_tado.calls()[0].body == {"temperatureOffset": -2.3}
    assert state_of(hass, offset_id(hass)).state == "-2.3"


async def test_set_offset_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected offset raises instead of failing silently (#51)."""
    await setup(hass, freezer)
    mock_tado.fail("PATCH", DEVICE, status=422)

    with pytest.raises(HomeAssistantError) as raised:
        await set_value(hass, offset_id(hass), 1)

    assert raised.value.translation_key == "request_rejected"
    assert raised.value.translation_placeholders == {
        "status": "422",
        "title": "HTTP 422",
    }
    assert state_of(hass, offset_id(hass)).state == "-1.5"


async def test_no_flow_temperature_without_a_boiler(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A home without OpenTherm gets no flow temperature number (C20)."""
    await setup(hass, freezer)

    assert not entity_registry.async_get_entity_id(
        NUMBER_DOMAIN, "tado_x", f"{HOME_B}_max_flow_temperature"
    )


async def test_max_flow_temperature(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The flow temperature uses tado's limits and is written without a read."""
    add_boiler(mock_tado)
    await setup(hass, freezer)
    flow = entity_id(hass, NUMBER_DOMAIN, f"{HOME_B}_max_flow_temperature")

    state = state_of(hass, flow)
    assert state.state == "50"
    assert (state.attributes[ATTR_MIN], state.attributes[ATTR_MAX]) == (30, 80)

    mock_tado.clear_calls()
    await set_value(hass, flow, 55)

    assert bodies(mock_tado, "PATCH", FLOW) == [{"maxFlowTemperature": 55}]
    assert state_of(hass, flow).state == "55"
    assert mock_tado.calls("GET") == []

    mock_tado.fail("PATCH", FLOW, status=500)
    with pytest.raises(HomeAssistantError) as raised:
        await set_value(hass, flow, 60)
    assert raised.value.translation_key == "cannot_connect"

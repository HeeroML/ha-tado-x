"""Tests for the child lock, open-window and flow auto-adaptation switches."""

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.switch.const import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_climate import ROOMS, bodies, entity_id, setup, state_of, tick
from tests.test_number import FLOW, add_boiler

VALVE = "VA0000000101"
DEVICE = f"/homes/{HOME_B}/roomsAndDevices/devices/{VALVE}"


def window_path(room_id: int) -> str:
    """Return the open-window path of a room."""
    return f"/homes/{HOME_B}/rooms/{room_id}/openWindow"


def window_id(hass: HomeAssistant, room_id: int) -> str:
    """Return the entity id of a room's open-window switch."""
    return entity_id(hass, SWITCH_DOMAIN, f"{HOME_B}_{room_id}_open_window")


async def switch(hass: HomeAssistant, entity: str, *, on: bool) -> None:
    """Turn a switch on or off and wait for it."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity},
        blocking=True,
    )


async def test_child_lock_exists_where_tado_reports_it(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Valves report a child lock and get a switch; sensors do not."""
    await setup(hass, freezer)

    assert state_of(
        hass, entity_id(hass, SWITCH_DOMAIN, f"{VALVE}_child_lock")
    ).state == (STATE_OFF)
    assert state_of(
        hass, entity_id(hass, SWITCH_DOMAIN, "VA0000000103_child_lock")
    ).state == (STATE_ON)
    assert not entity_registry.async_get_entity_id(
        SWITCH_DOMAIN, "tado_x", "SU0000000101_child_lock"
    )


async def test_toggle_child_lock(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The lock is written with one PATCH and shown without a read."""
    await setup(hass, freezer)
    lock = entity_id(hass, SWITCH_DOMAIN, f"{VALVE}_child_lock")
    mock_tado.clear_calls()

    await switch(hass, lock, on=True)
    assert state_of(hass, lock).state == STATE_ON
    await switch(hass, lock, on=False)

    assert bodies(mock_tado, "PATCH", DEVICE) == [
        {"childLockEnabled": True},
        {"childLockEnabled": False},
    ]
    assert state_of(hass, lock).state == STATE_OFF
    assert mock_tado.calls("GET") == []


async def test_child_lock_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failed write raises a translated error."""
    await setup(hass, freezer)
    mock_tado.fail("PATCH", DEVICE, status=503)

    with pytest.raises(HomeAssistantError) as raised:
        await switch(
            hass, entity_id(hass, SWITCH_DOMAIN, f"{VALVE}_child_lock"), on=True
        )

    assert raised.value.translation_key == "cannot_connect"


async def test_open_window_states(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The switch is on while open-window mode pauses the heating."""
    await setup(hass, freezer)

    assert state_of(hass, window_id(hass, 5)).state == STATE_ON
    assert state_of(hass, window_id(hass, 1)).state == STATE_OFF


async def test_end_open_window_mode(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Off ends open-window mode with a DELETE and reads the rooms back once."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await switch(hass, window_id(hass, 5), on=False)

    assert len(mock_tado.calls("DELETE", window_path(5))) == 1
    assert state_of(hass, window_id(hass, 5)).state == STATE_OFF
    assert mock_tado.calls("GET") == []

    await tick(hass, freezer, 5)

    assert len(mock_tado.calls("GET", ROOMS)) == 1
    assert state_of(hass, window_id(hass, 5)).state == STATE_OFF


async def test_start_open_window_mode_after_detection(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """On starts open-window mode when tado detected an open window."""
    room = mock_tado.home(HOME_B).room(1)
    assert room is not None
    room["openWindow"] = {"activated": False, "expiryInSeconds": 900}
    await setup(hass, freezer)
    assert state_of(hass, window_id(hass, 1)).state == STATE_OFF

    await switch(hass, window_id(hass, 1), on=True)

    assert len(mock_tado.calls("POST", window_path(1))) == 1
    assert state_of(hass, window_id(hass, 1)).state == STATE_ON


async def test_open_window_mode_needs_a_detection(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without a detected window tado refuses, and the error says why (Q11).

    The refusal is no API error, so "Last API error" stays unknown.
    """
    entry = await setup(hass, freezer)

    with pytest.raises(ServiceValidationError) as raised:
        await switch(hass, window_id(hass, 1), on=True)

    assert raised.value.translation_key == "open_window_not_detected"
    assert state_of(hass, window_id(hass, 1)).state == STATE_OFF
    assert entry.runtime_data.telemetry.last_error is None


async def test_end_open_window_mode_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A refused DELETE is a request error, not a missing detection."""
    await setup(hass, freezer)
    mock_tado.fail("DELETE", window_path(5), status=409)

    with pytest.raises(HomeAssistantError) as raised:
        await switch(hass, window_id(hass, 5), on=False)

    assert raised.value.translation_key == "request_rejected"
    assert state_of(hass, window_id(hass, 5)).state == STATE_ON


async def test_flow_auto_adaptation(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The auto-adaptation switch exists with a boiler and writes without a read."""
    add_boiler(mock_tado)
    await setup(hass, freezer)
    adaptation = entity_id(hass, SWITCH_DOMAIN, f"{HOME_B}_flow_temp_auto_adaptation")
    assert state_of(hass, adaptation).state == STATE_OFF
    mock_tado.clear_calls()

    await switch(hass, adaptation, on=True)
    await switch(hass, adaptation, on=False)

    assert bodies(mock_tado, "PATCH", FLOW) == [
        {"autoAdaptation": {"enabled": True}},
        {"autoAdaptation": {"enabled": False}},
    ]
    assert state_of(hass, adaptation).state == STATE_OFF
    assert mock_tado.calls("GET") == []


async def test_no_auto_adaptation_without_a_boiler(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A home without OpenTherm gets no auto-adaptation switch."""
    await setup(hass, freezer)

    assert not entity_registry.async_get_entity_id(
        SWITCH_DOMAIN, "tado_x", f"{HOME_B}_flow_temp_auto_adaptation"
    )

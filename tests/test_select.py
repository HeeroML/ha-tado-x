"""Tests for the presence and room termination selects (#86, #94)."""

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.tado_x.const import OPT_TIMER_MINUTES
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.select.const import ATTR_OPTIONS, DOMAIN as SELECT_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_OPTION,
    SERVICE_SELECT_OPTION,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_climate import (
    PRESENCE,
    bodies,
    climate_id,
    entity_id,
    manual_path,
    setup,
    state_of,
)


def presence_id(hass: HomeAssistant) -> str:
    """Return the entity id of the presence select."""
    return entity_id(hass, SELECT_DOMAIN, f"{HOME_B}_presence_mode")


def termination_id(hass: HomeAssistant, room_id: int) -> str:
    """Return the entity id of a room's termination select."""
    return entity_id(hass, SELECT_DOMAIN, f"{HOME_B}_{room_id}_manual_termination")


def room_path(room_id: int) -> str:
    """Return the path of a room's settings."""
    return f"/homes/{HOME_B}/roomsAndDevices/rooms/{room_id}"


async def select(hass: HomeAssistant, entity: str, option: str) -> None:
    """Select an option and wait for it."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity, ATTR_OPTION: option},
        blocking=True,
    )


async def test_presence_select(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The presence select has a name in every language and lowercase options (#86)."""
    await setup(hass, freezer)

    state = state_of(hass, presence_id(hass))

    assert state.state == "auto"
    assert state.attributes[ATTR_OPTIONS] == ["home", "away", "auto"]
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Haus B Presence mode"


@pytest.mark.parametrize(
    ("option", "method", "body"),
    [
        ("home", "PUT", {"homePresence": "HOME"}),
        ("away", "PUT", {"homePresence": "AWAY"}),
        ("auto", "DELETE", None),
    ],
)
async def test_select_presence(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    option: str,
    method: str,
    body: dict[str, str] | None,
) -> None:
    """Home and away lock the presence, auto unlocks it, with no read back."""
    mock_tado.home(HOME_B).state = {"presence": "AWAY", "presenceLocked": True}
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await select(hass, presence_id(hass), option)

    assert bodies(mock_tado, method, PRESENCE) == [body]
    assert state_of(hass, presence_id(hass)).state == option
    assert mock_tado.calls("GET") == []


@pytest.mark.parametrize(
    ("option", "status", "error", "key"),
    [
        ("auto", 422, ServiceValidationError, "presence_auto_requires_auto_assist"),
        ("auto", 403, HomeAssistantError, "request_rejected"),
        ("away", 500, HomeAssistantError, "cannot_connect"),
    ],
)
async def test_presence_errors(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    option: str,
    status: int,
    error: type[HomeAssistantError],
    key: str,
) -> None:
    """Only a 422 on auto means geofencing needs Auto-Assist."""
    mock_tado.home(HOME_B).state = {"presence": "HOME", "presenceLocked": True}
    await setup(hass, freezer)
    mock_tado.fail(None, PRESENCE, status=status)

    with pytest.raises(error) as raised:
        await select(hass, presence_id(hass), option)

    assert raised.value.translation_key == key
    assert state_of(hass, presence_id(hass)).state == "home"


@pytest.mark.parametrize(
    ("room_id", "option"),
    [(1, "next_time_block"), (2, "timer"), (3, "manual")],
)
async def test_termination_select_shows_the_app_setting(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    room_id: int,
    option: str,
) -> None:
    """The select shows the room's setting of the tado app."""
    await setup(hass, freezer)

    entity = termination_id(hass, room_id)
    state = state_of(hass, entity)

    assert state.state == option
    assert state.attributes[ATTR_OPTIONS] == ["manual", "next_time_block", "timer"]
    registered = entity_registry.async_get(entity)
    assert registered is not None
    assert registered.entity_category is EntityCategory.CONFIG


async def test_termination_select_without_app_setting(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A room without a usable app setting shows unknown."""
    room = mock_tado.home(HOME_B).topology_room(4)
    assert room is not None
    del room["deviceManualControlTermination"]
    await setup(hass, freezer)

    assert state_of(hass, termination_id(hass, 4)).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("room_id", "option", "options", "termination"),
    [
        (2, "manual", {}, {"type": "MANUAL"}),
        (1, "next_time_block", {}, {"type": "NEXT_TIME_BLOCK"}),
        (1, "timer", {}, {"type": "TIMER", "durationInSeconds": 3600}),
        (
            1,
            "timer",
            {OPT_TIMER_MINUTES: 45},
            {"type": "TIMER", "durationInSeconds": 2700},
        ),
        (
            6,
            "timer",
            {OPT_TIMER_MINUTES: 45},
            {"type": "TIMER", "durationInSeconds": 1800},
        ),
    ],
    ids=["manual", "next_time_block", "timer_default", "timer_option", "timer_kept"],
)
async def test_select_termination(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    room_id: int,
    option: str,
    options: dict[str, Any],
    termination: dict[str, Any],
) -> None:
    """The select writes the app setting; a timer keeps tado's duration if it has one."""
    await setup(hass, freezer, **options)
    mock_tado.clear_calls()

    await select(hass, termination_id(hass, room_id), option)

    assert bodies(mock_tado, "PATCH", room_path(room_id)) == [
        {"deviceManualControlTermination": termination}
    ]
    assert state_of(hass, termination_id(hass, room_id)).state == option
    assert mock_tado.calls("GET") == []


async def test_new_setting_applies_to_the_next_change(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The next manual change uses the new setting; tado holds the only copy."""
    await setup(hass, freezer)

    await select(hass, termination_id(hass, 1), "manual")
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: climate_id(hass, 1), "temperature": 20},
        blocking=True,
    )

    assert bodies(mock_tado, "POST", manual_path(1))[0]["termination"] == {
        "type": "MANUAL"
    }


async def test_select_termination_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected write raises and leaves the select as tado reports it."""
    await setup(hass, freezer)
    mock_tado.fail("PATCH", room_path(1), status=404)

    with pytest.raises(HomeAssistantError) as raised:
        await select(hass, termination_id(hass, 1), "manual")

    assert raised.value.translation_key == "not_found"
    assert state_of(hass, termination_id(hass, 1)).state == "next_time_block"

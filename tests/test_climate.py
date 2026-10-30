"""Tests for the room climate entities and their writes (#94)."""

from datetime import timedelta
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tado_x.const import (
    DOMAIN,
    OPT_MANUAL_TERMINATION,
    OPT_OFF_FOLLOWS_POLICY,
    OPT_TIMER_MINUTES,
)
from homeassistant.components.climate.const import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_init import make_entry

START = "2026-10-30T10:00:00+00:00"
ROOMS = "/homes/1000002/rooms"
PRESENCE = "/api/v2/homes/1000002/presenceLock"
NEXT_TIME_BLOCK = {"type": "NEXT_TIME_BLOCK"}
MANUAL = {"type": "MANUAL"}
OFF = {"power": "OFF"}


def heat(temperature: float) -> dict[str, Any]:
    """Return the setting body of a manual target."""
    return {"power": "ON", "isBoost": False, "temperature": {"value": temperature}}


def manual_path(room_id: int, home_id: int = HOME_B) -> str:
    """Return the manual control path of a room."""
    return f"/homes/{home_id}/rooms/{room_id}/manualControl"


async def setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry | None = None,
    **options: Any,
) -> MockConfigEntry:
    """Set up an entry of home 1000002 at START with changed options."""
    freezer.move_to(START)
    entry = entry or make_entry()
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock forward and run what became due."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    """Return the entity id registered for a unique id."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found is not None
    return found


def climate_id(hass: HomeAssistant, room_id: int, home_id: int = HOME_B) -> str:
    """Return the entity id of a room's climate entity."""
    return entity_id(hass, CLIMATE_DOMAIN, f"{home_id}_{room_id}_climate")


def state_of(hass: HomeAssistant, entity: str) -> State:
    """Return the state of an entity, which must exist."""
    state = hass.states.get(entity)
    assert state is not None
    return state


def bodies(mock_tado: TadoCloudMock, method: str, path: str) -> list[Any]:
    """Return the bodies of the matching requests."""
    return [call.body for call in mock_tado.calls(method, path)]


async def climate_call(
    hass: HomeAssistant, service: str, entity: str, **data: Any
) -> None:
    """Call a climate service for one entity and wait for it."""
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: entity, **data}, blocking=True
    )


@pytest.mark.parametrize(
    ("room_id", "mode", "action", "target", "manual"),
    [
        (1, HVACMode.AUTO, HVACAction.HEATING, 21.0, None),
        (2, HVACMode.HEAT, HVACAction.HEATING, 22.5, "timer"),
        (3, HVACMode.OFF, HVACAction.OFF, None, "manual"),
        (4, HVACMode.HEAT, HVACAction.HEATING, 21.5, "next_time_block"),
        (5, HVACMode.AUTO, HVACAction.IDLE, 22.0, None),
        (6, HVACMode.AUTO, HVACAction.HEATING, 25.0, None),
    ],
)
async def test_room_states(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    room_id: int,
    mode: HVACMode,
    action: HVACAction,
    target: float | None,
    manual: str | None,
) -> None:
    """Manual control decides OFF or HEAT, else the schedule runs (AUTO)."""
    await setup(hass, freezer)

    state = state_of(hass, climate_id(hass, room_id))

    assert state.state == mode
    assert state.attributes[ATTR_HVAC_ACTION] == action
    assert state.attributes[ATTR_TEMPERATURE] == target
    assert state.attributes["manual_control_type"] == manual
    assert state.attributes[ATTR_PRESET_MODE] == "auto"


async def test_entity_attributes(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The room device names the entity; null sensor data means unknown."""
    await setup(hass, freezer)

    living = state_of(hass, climate_id(hass, 1))
    study = state_of(hass, climate_id(hass, 6))

    assert living.attributes[ATTR_FRIENDLY_NAME] == "Wohnzimmer"
    assert living.attributes[ATTR_CURRENT_TEMPERATURE] == 21.1
    assert living.attributes[ATTR_CURRENT_HUMIDITY] == 52.0
    assert living.attributes[ATTR_HVAC_MODES] == ["auto", "heat", "off"]
    assert living.attributes[ATTR_PRESET_MODES] == ["home", "away", "auto"]
    assert living.attributes[ATTR_MIN_TEMP] == 5.0
    assert living.attributes[ATTR_MAX_TEMP] == 30.0
    assert living.attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert study.attributes[ATTR_CURRENT_TEMPERATURE] is None
    assert ATTR_CURRENT_HUMIDITY not in study.attributes


async def test_locked_presence_is_the_preset(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A locked presence shows as home or away on every room."""
    mock_tado.home(HOME_B).state = {"presence": "AWAY", "presenceLocked": True}
    await setup(hass, freezer)

    assert state_of(hass, climate_id(hass, 3)).attributes[ATTR_PRESET_MODE] == "away"


@pytest.mark.parametrize(
    ("room_id", "termination"),
    [
        (1, NEXT_TIME_BLOCK),
        (2, {"type": "TIMER", "durationInSeconds": 3600}),
        (3, MANUAL),
        (5, NEXT_TIME_BLOCK),
    ],
    ids=["next_time_block", "timer", "manual", "missing"],
)
async def test_set_temperature_follows_the_app(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    room_id: int,
    termination: dict[str, Any],
) -> None:
    """follow_app sends the room's app setting; without one the next block (#94)."""
    bath = mock_tado.home(HOME_B).topology_room(5)
    assert bath is not None
    del bath["deviceManualControlTermination"]
    await setup(hass, freezer)

    await climate_call(
        hass, SERVICE_SET_TEMPERATURE, climate_id(hass, room_id), temperature=22.5
    )

    assert bodies(mock_tado, "POST", manual_path(room_id)) == [
        {"setting": heat(22.5), "termination": termination}
    ]


async def test_timer_without_duration_in_the_app_ends_at_the_next_block(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """An app timer without a duration is unusable, so the next block ends it."""
    kitchen = mock_tado.home(HOME_B).topology_room(2)
    assert kitchen is not None
    kitchen["deviceManualControlTermination"] = {
        "type": "TIMER",
        "durationInSeconds": None,
    }
    await setup(hass, freezer)

    await climate_call(
        hass, SERVICE_SET_TEMPERATURE, climate_id(hass, 2), temperature=20
    )

    assert bodies(mock_tado, "POST", manual_path(2))[0]["termination"] == (
        NEXT_TIME_BLOCK
    )


@pytest.mark.parametrize(
    ("options", "termination"),
    [
        ({OPT_MANUAL_TERMINATION: "manual"}, MANUAL),
        ({OPT_MANUAL_TERMINATION: "next_time_block"}, NEXT_TIME_BLOCK),
        (
            {OPT_MANUAL_TERMINATION: "timer", OPT_TIMER_MINUTES: 45},
            {"type": "TIMER", "durationInSeconds": 2700},
        ),
    ],
    ids=["manual", "next_time_block", "timer"],
)
async def test_termination_option_overrides_the_app(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any],
    termination: dict[str, Any],
) -> None:
    """A fixed policy wins over the app's timer of the kitchen."""
    await setup(hass, freezer, **options)

    await climate_call(
        hass, SERVICE_SET_TEMPERATURE, climate_id(hass, 2), temperature=21
    )

    assert bodies(mock_tado, "POST", manual_path(2)) == [
        {"setting": heat(21.0), "termination": termination}
    ]


async def test_heat_and_turn_on_follow_the_policy(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Heat and turn on keep the current target and end like set_temperature."""
    await setup(hass, freezer)
    living = climate_id(hass, 1)

    await climate_call(hass, SERVICE_SET_HVAC_MODE, living, hvac_mode=HVACMode.HEAT)
    await climate_call(hass, SERVICE_TURN_ON, living)

    assert (
        bodies(mock_tado, "POST", manual_path(1))
        == [{"setting": heat(21.0), "termination": NEXT_TIME_BLOCK}] * 2
    )


async def test_turn_on_temperature_fallbacks(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Turning on uses the last target, the next schedule target, then 21 °C."""
    home = mock_tado.home(HOME_B)
    bath, study = home.room(5), home.room(6)
    assert bath is not None
    assert study is not None
    bath["setting"] = {"power": "OFF", "temperature": None}
    study["setting"] = {"power": "OFF", "temperature": None}
    study["nextScheduleChange"] = None
    await setup(hass, freezer)

    await climate_call(hass, SERVICE_TURN_OFF, climate_id(hass, 1))
    await tick(hass, freezer, 5)
    for room_id in (1, 3, 5, 6):
        await climate_call(hass, SERVICE_TURN_ON, climate_id(hass, room_id))

    assert [
        bodies(mock_tado, "POST", manual_path(room_id))[-1]["setting"]
        for room_id in (1, 3, 5, 6)
    ] == [heat(21.0), heat(21.0), heat(19.0), heat(21.0)]


async def test_off_is_manual_unless_it_follows_the_policy(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Off stays until ended by default (C8); the option makes it follow the app."""
    entry = await setup(hass, freezer)
    kitchen = climate_id(hass, 2)

    await climate_call(hass, SERVICE_SET_HVAC_MODE, kitchen, hvac_mode=HVACMode.OFF)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_OFF_FOLLOWS_POLICY: True}
    )
    await climate_call(hass, SERVICE_TURN_OFF, kitchen)

    assert bodies(mock_tado, "POST", manual_path(2)) == [
        {"setting": OFF, "termination": MANUAL},
        {"setting": OFF, "termination": {"type": "TIMER", "durationInSeconds": 3600}},
    ]
    assert state_of(hass, kitchen).state == HVACMode.OFF


async def test_set_temperature_with_off_mode_turns_off(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A set_temperature call that also asks for off turns the room off."""
    await setup(hass, freezer)

    await climate_call(
        hass,
        SERVICE_SET_TEMPERATURE,
        climate_id(hass, 1),
        temperature=20,
        hvac_mode=HVACMode.OFF,
    )

    assert bodies(mock_tado, "POST", manual_path(1)) == [
        {"setting": OFF, "termination": MANUAL}
    ]


async def test_auto_resumes_the_schedule(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Auto ends the manual control with a DELETE and shows AUTO at once."""
    await setup(hass, freezer)
    kitchen = climate_id(hass, 2)
    mock_tado.clear_calls()

    await climate_call(hass, SERVICE_SET_HVAC_MODE, kitchen, hvac_mode=HVACMode.AUTO)

    assert len(mock_tado.calls("DELETE", manual_path(2))) == 1
    assert state_of(hass, kitchen).state == HVACMode.AUTO
    assert mock_tado.calls("GET", ROOMS) == []

    await tick(hass, freezer, 5)

    assert len(mock_tado.calls("GET", ROOMS)) == 1
    assert state_of(hass, kitchen).state == HVACMode.AUTO


@pytest.mark.parametrize(
    ("preset", "method", "body"),
    [
        ("away", "PUT", {"homePresence": "AWAY"}),
        ("home", "PUT", {"homePresence": "HOME"}),
        ("auto", "DELETE", None),
    ],
)
async def test_presets_set_the_home_presence(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    preset: str,
    method: str,
    body: dict[str, str] | None,
) -> None:
    """Presets lock the presence of the whole home, or unlock it."""
    mock_tado.home(HOME_B).state = {"presence": "HOME", "presenceLocked": True}
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await climate_call(
        hass, SERVICE_SET_PRESET_MODE, climate_id(hass, 1), preset_mode=preset
    )

    assert bodies(mock_tado, method, PRESENCE) == [body]
    assert state_of(hass, climate_id(hass, 4)).attributes[ATTR_PRESET_MODE] == preset
    assert mock_tado.calls("GET") == []


async def test_auto_preset_without_auto_assist(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without Auto-Assist, tado refuses geofencing with a 422."""
    mock_tado.home(HOME_B).state = {"presence": "HOME", "presenceLocked": True}
    await setup(hass, freezer)
    mock_tado.fail("DELETE", PRESENCE, status=422)

    with pytest.raises(ServiceValidationError) as raised:
        await climate_call(
            hass, SERVICE_SET_PRESET_MODE, climate_id(hass, 1), preset_mode="auto"
        )

    assert raised.value.translation_key == "presence_auto_requires_auto_assist"
    assert state_of(hass, climate_id(hass, 1)).attributes[ATTR_PRESET_MODE] == "home"


async def test_writes_show_before_the_verification_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The new target shows at once; three writes cost one debounced read."""
    await setup(hass, freezer)
    living = climate_id(hass, 1)
    mock_tado.clear_calls()

    for temperature in (22.0, 22.5, 23.0):
        await climate_call(
            hass, SERVICE_SET_TEMPERATURE, living, temperature=temperature
        )

    state = state_of(hass, living)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == 23.0
    assert state.attributes["manual_control_type"] == "next_time_block"
    assert mock_tado.calls("GET") == []

    await tick(hass, freezer, 5)

    assert len(mock_tado.calls("GET", ROOMS)) == 1
    assert state_of(hass, living).attributes[ATTR_TEMPERATURE] == 23.0


async def test_write_survives_a_stale_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A read that does not show the write yet keeps the written target."""
    await setup(hass, freezer)
    living = climate_id(hass, 1)
    room = mock_tado.home(HOME_B).room(1)
    assert room is not None
    stale = dict(room)

    await climate_call(hass, SERVICE_SET_TEMPERATURE, living, temperature=24)
    room.clear()
    room.update(stale)
    await tick(hass, freezer, 5)

    assert state_of(hass, living).attributes[ATTR_TEMPERATURE] == 24.0

    await tick(hass, freezer, 60)

    assert state_of(hass, living).attributes[ATTR_TEMPERATURE] == 21.0
    assert state_of(hass, living).state == HVACMode.AUTO


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        ({"status": 429}, "quota_exhausted"),
        ({"status": 422}, "request_rejected"),
        ({"exc": ClientConnectionError()}, "cannot_connect"),
    ],
    ids=["quota", "rejected", "network"],
)
async def test_write_errors_are_translated(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    failure: dict[str, Any],
    key: str,
) -> None:
    """A failed write raises a translated error and changes nothing."""
    await setup(hass, freezer)
    living = climate_id(hass, 1)
    mock_tado.fail("POST", manual_path(1), **failure)

    with pytest.raises(HomeAssistantError) as raised:
        await climate_call(hass, SERVICE_SET_TEMPERATURE, living, temperature=25)

    assert raised.value.translation_key == key
    assert state_of(hass, living).state == HVACMode.AUTO
    assert state_of(hass, living).attributes[ATTR_TEMPERATURE] == 21.0


async def test_rejected_sign_in_starts_reauth(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A 401 that survives a token refresh starts a reauth flow."""
    entry = await setup(hass, freezer)
    mock_tado.fail("POST", manual_path(1), status=401)

    with pytest.raises(HomeAssistantError) as raised:
        await climate_call(
            hass, SERVICE_SET_TEMPERATURE, climate_id(hass, 1), temperature=25
        )
    await hass.async_block_till_done()

    assert raised.value.translation_key == "auth_failed"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == entry.entry_id


async def test_disconnected_room_is_unavailable(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A room that lost its connection to the bridge cannot be controlled."""
    room = mock_tado.home(HOME_B).room(4)
    assert room is not None
    room["connection"] = {"state": "DISCONNECTED"}
    await setup(hass, freezer)

    assert state_of(hass, climate_id(hass, 4)).state == STATE_UNAVAILABLE
    assert state_of(hass, climate_id(hass, 1)).state == HVACMode.AUTO


async def test_room_added_later_gets_a_climate_entity(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A room tado reports after setup gets its entity on the next poll."""
    await setup(hass, freezer)
    home = mock_tado.home(HOME_B)
    room = home.room(1)
    assert room is not None
    home.rooms.append({**room, "id": 7, "name": "Gaestezimmer"})
    home.topology["rooms"].append(
        {"roomId": 7, "roomName": "Gaestezimmer", "devices": []}
    )

    await tick(hass, freezer, 45)

    unique_id = f"{HOME_B}_7_climate"
    assert not er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, unique_id)

    await tick(hass, freezer, 900)

    state = state_of(hass, climate_id(hass, 7))
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Gaestezimmer"

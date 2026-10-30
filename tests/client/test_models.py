"""Tests for the boundary parsers of the tado X client models."""

from collections.abc import Callable
import copy
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Any

import pytest
from yarl import URL

from custom_components.tado_x.client import (
    CLIENT_ID,
    Account,
    ActiveOverride,
    AirComfort,
    BatteryState,
    ConnectionState,
    ControlMode,
    DayReport,
    DayType,
    DeviceAuthorization,
    DeviceType,
    FlowTemperature,
    HolidayMode,
    HomeGeneration,
    HomeInfo,
    HomeState,
    HumidityLevel,
    MobileDevices,
    OpenWindow,
    Power,
    Presence,
    PresenceSetting,
    RateLimitInfo,
    RoomSchedule,
    RoomState,
    RunningTimes,
    ScheduleBlock,
    Setting,
    TadoXParseError,
    TemperatureLevel,
    Termination,
    TerminationType,
    TokenSet,
    Topology,
    Weather,
    WeatherState,
    format_clock,
    parse_enum,
    utcnow,
)
from tests.tado_mock import FIXTURES, load_fixture

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
TODAY = date(2026, 10, 30)

PARSERS: dict[str, Callable[[Any], object]] = {
    "air_comfort.json": AirComfort.from_api,
    "day_report.json": lambda raw: DayReport.from_api(raw, day=TODAY),
    "device_authorize.json": lambda raw: DeviceAuthorization.from_api(
        raw, received_at=NOW
    ),
    "flow_temperature.json": FlowTemperature.from_api,
    "holiday_mode.json": HolidayMode.from_api,
    "home_1000001.json": HomeInfo.from_api,
    "home_1000002.json": HomeInfo.from_api,
    "me.json": Account.from_api,
    "mobile_devices.json": MobileDevices.from_api,
    "rooms.json": RoomState.list_from_api,
    "rooms_and_devices.json": Topology.from_api,
    "running_times.json": lambda raw: RunningTimes.from_api(raw, day=TODAY),
    "schedule.json": lambda raw: RoomSchedule.from_api(raw, room_id=1),
    "state.json": HomeState.from_api,
    "state_locked.json": HomeState.from_api,
    "token.json": lambda raw: TokenSet.from_token_response(
        raw, sent_at=NOW, previous_refresh=None
    ),
    "weather.json": Weather.from_api,
}
NOT_MODELS = {
    "hops_home_not_x.json",
    "ratelimit_headers.json",
    "token_invalid_grant.json",
    "token_pending.json",
}


def _rooms() -> dict[int, RoomState]:
    return RoomState.list_from_api(load_fixture("rooms.json"))


def _topology() -> Topology:
    return Topology.from_api(load_fixture("rooms_and_devices.json"))


def _unknown_value_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "Unknown value" in record.getMessage()
    ]


def test_every_fixture_has_a_parser() -> None:
    """Every fixture file is either parsed below or a non-model payload."""
    assert {path.name for path in FIXTURES.glob("*.json")} == set(PARSERS) | NOT_MODELS


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_fixture_parses(name: str, caplog: pytest.LogCaptureFixture) -> None:
    """Every response fixture parses without unknown enum values."""
    assert PARSERS[name](load_fixture(name)) is not None
    assert not _unknown_value_warnings(caplog)


def test_rooms_list() -> None:
    """The six-state rooms fixture maps onto RoomState."""
    rooms = _rooms()
    assert list(rooms) == [1, 2, 3, 4, 5, 6]

    schedule = rooms[1]
    assert schedule.name == "Wohnzimmer"
    assert schedule.setting == Setting(power=Power.ON, temperature=21.0)
    assert schedule.current_temperature == 21.1
    assert schedule.humidity == 52.0
    assert schedule.heating_power == 12
    assert schedule.connection is ConnectionState.CONNECTED
    assert schedule.manual_control is None
    assert schedule.boost is None
    assert schedule.open_window is None
    assert schedule.next_time_block == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    assert schedule.next_schedule_change is not None
    assert schedule.next_schedule_change.start == datetime(
        2026, 10, 30, 21, 0, tzinfo=UTC
    )
    assert schedule.next_schedule_change.setting.temperature == 18.0

    assert rooms[2].manual_control == ActiveOverride(
        type=TerminationType.TIMER,
        remaining=timedelta(seconds=2710),
        projected_expiry=datetime(2026, 10, 30, 10, 45, 10, tzinfo=UTC),
    )

    off = rooms[3]
    assert off.setting == Setting(power=Power.OFF, temperature=None)
    assert off.manual_control == ActiveOverride(type=TerminationType.MANUAL)
    assert off.next_schedule_change is None

    assert rooms[4].manual_control is not None
    assert rooms[4].manual_control.type is TerminationType.NEXT_TIME_BLOCK
    assert rooms[5].open_window == OpenWindow(
        activated=True, expires_in=timedelta(seconds=840)
    )
    assert rooms[6].boost is not None
    assert rooms[6].boost.type is TerminationType.TIMER


def test_null_sensor_data_points() -> None:
    """A room without sensor data has unknown temperature and humidity."""
    room = _rooms()[6]
    assert room.current_temperature is None
    assert room.humidity is None


def test_open_window_reads_activated() -> None:
    """openWindow.activated decides; an object without it counts as activated."""
    raw = load_fixture("rooms.json")
    raw[4]["openWindow"] = {"activated": False, "expiryInSeconds": 0}
    raw[0]["openWindow"] = {"expiryInSeconds": 120}
    rooms = RoomState.list_from_api(raw)
    assert rooms[5].open_window == OpenWindow(activated=False, expires_in=timedelta(0))
    assert rooms[1].open_window == OpenWindow(
        activated=True, expires_in=timedelta(seconds=120)
    )


def test_next_schedule_change_without_setting() -> None:
    """A next schedule change without a usable setting is unknown."""
    raw = load_fixture("rooms.json")
    raw[0]["nextScheduleChange"] = {"start": "2026-10-30T21:00:00Z"}
    raw[1]["nextScheduleChange"] = {"setting": {"power": "ON"}}
    rooms = RoomState.list_from_api(raw)
    assert rooms[1].next_schedule_change is None
    assert rooms[2].next_schedule_change is None


def test_single_room() -> None:
    """GET rooms/{id} returns one room of the same shape."""
    assert RoomState.from_api(load_fixture("rooms.json")[1]).id == 2


def test_timestamps_are_aware_utc() -> None:
    """Naive timestamps count as UTC, offsets are converted, garbage is None."""
    raw = load_fixture("rooms.json")
    raw[0]["nextTimeBlock"] = {"start": "2026-10-30T21:00:00"}
    raw[1]["nextTimeBlock"] = {"start": "2026-10-30T23:00:00+02:00"}
    raw[2]["nextTimeBlock"] = {"start": "tomorrow"}
    rooms = RoomState.list_from_api(raw)
    assert rooms[1].next_time_block == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    assert rooms[2].next_time_block == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    assert rooms[3].next_time_block is None


def test_numbers_are_coerced() -> None:
    """Integral floats become ints, booleans are never numbers."""
    raw = load_fixture("rooms.json")
    raw[0]["heatingPower"] = {"percentage": 12.0}
    raw[1]["heatingPower"] = {"percentage": True}
    raw[2]["sensorDataPoints"]["insideTemperature"] = {"value": 18}
    rooms = RoomState.list_from_api(raw)
    assert rooms[1].heating_power == 12
    assert rooms[2].heating_power is None
    assert rooms[3].current_temperature == 18.0
    assert isinstance(rooms[3].current_temperature, float)


def test_missing_required_key_raises() -> None:
    """A bad required key raises TadoXParseError with its path."""
    room = load_fixture("rooms.json")[0]
    del room["id"]
    with pytest.raises(TadoXParseError) as err:
        RoomState.from_api(room)
    assert err.value.endpoint == "rooms"
    assert err.value.path == "$.id"

    with pytest.raises(TadoXParseError):
        RoomState.list_from_api({"rooms": []})
    with pytest.raises(TadoXParseError):
        Topology.from_api({"otherDevices": []})
    with pytest.raises(TadoXParseError):
        HomeState.from_api({"presenceLocked": True})
    with pytest.raises(TadoXParseError):
        Account.from_api({"homes": []})
    with pytest.raises(TadoXParseError):
        HomeInfo.from_api([])


def test_malformed_room_item_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """One bad room does not fail the whole poll."""
    raw = load_fixture("rooms.json")
    del raw[2]["setting"]
    raw.append("not a room")
    caplog.set_level(logging.DEBUG)
    rooms = RoomState.list_from_api(raw)
    assert list(rooms) == [1, 2, 4, 5, 6]
    assert "Skipping a malformed rooms item" in caplog.text


def test_unknown_power_fails_the_room() -> None:
    """The power of a room is required."""
    raw = load_fixture("rooms.json")
    raw[0]["setting"]["power"] = "STANDBY_TEST"
    assert 1 not in RoomState.list_from_api(raw)


def test_malformed_device_is_skipped() -> None:
    """A device without a serial number is skipped, its room is kept."""
    raw = load_fixture("rooms_and_devices.json")
    del raw["rooms"][0]["devices"][0]["serialNumber"]
    topology = Topology.from_api(raw)
    assert [device.serial for device in topology.rooms[1].devices] == ["SU0000000101"]


def _without(raw: dict[str, Any], path: tuple[str | int, ...]) -> None:
    """Delete the item or key at a path of a payload."""
    parent: Any = raw
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]


@pytest.mark.parametrize(
    "path",
    [
        ("rooms", 2, "roomId"),
        ("rooms", 3, "devices"),
        ("otherDevices", 0, "serialNumber"),
        ("otherDevices",),
    ],
)
def test_skipped_topology_items_make_it_incomplete(
    path: tuple[str | int, ...],
) -> None:
    """A room or device the parser skips, or a missing device list, is noted."""
    assert _topology().complete

    raw = load_fixture("rooms_and_devices.json")
    _without(raw, path)
    topology = Topology.from_api(raw)

    assert not topology.complete
    assert 1 in topology.rooms


def test_device_that_is_no_object_makes_the_topology_incomplete() -> None:
    """A device item that is no JSON object is skipped and noted."""
    raw = load_fixture("rooms_and_devices.json")
    raw["rooms"][0]["devices"].append("not a device")

    topology = Topology.from_api(raw)

    assert not topology.complete
    assert len(topology.rooms[1].devices) == 2


def test_skipped_items_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    """The first skip of a kind is a warning, repeats of it are debug messages."""
    raw = load_fixture("rooms_and_devices.json")
    raw["otherDevices"].append({"serialNumber": "TR0000000999"})
    raw["otherDevices"].append({"serialNumber": "TR0000000998"})
    caplog.set_level(logging.DEBUG)

    Topology.from_api(raw)
    Topology.from_api(raw)

    skips = [
        record
        for record in caplog.records
        if "Skipping a malformed topology item" in record.getMessage()
    ]
    assert [record.levelno for record in skips] == [logging.WARNING] + [
        logging.DEBUG
    ] * 3
    assert "$.otherDevices[1].type" in skips[0].getMessage()


def test_topology() -> None:
    """Rooms, devices and the bridge of roomsAndDevices."""
    topology = _topology()
    assert list(topology.rooms) == [1, 2, 3, 4, 5, 6]
    assert topology.rooms[1].name == "Wohnzimmer"
    assert len(topology.devices) == 13
    bridge = topology.bridge
    assert bridge is not None
    assert bridge.serial == "IB0000000001"
    assert bridge.firmware == "279.1"
    assert bridge.room_id is None
    assert not bridge.has_battery
    assert bridge.battery is None
    assert bridge.child_lock is None
    assert bridge.temperature_offset is None

    valve = topology.devices["VA0000000101"]
    assert valve.type is DeviceType.VA04
    assert valve.type_raw == "VA04"
    assert valve.room_id == 1
    assert valve.mounting_state == "CALIBRATED"
    assert valve.child_lock is False
    assert valve.temperature_measured == 22.4
    assert valve.temperature_offset == -1.5

    sensor = topology.devices["SU0000000101"]
    assert sensor.type is DeviceType.SU04
    assert sensor.child_lock is None
    assert sensor.mounting_state is None
    assert sensor.temperature_offset == 0.0
    assert topology.devices["VA0000000103"].connection is ConnectionState.DISCONNECTED


def test_other_devices_and_unknown_types(caplog: pytest.LogCaptureFixture) -> None:
    """Other devices keep their roomId; unknown device types get type None."""
    raw = load_fixture("rooms_and_devices.json")
    raw["otherDevices"] += [
        {
            "serialNumber": "TR0000000301",
            "type": "TR04",
            "firmwareVersion": 281,
            "connection": {"state": "CONNECTED"},
            "roomId": 2,
            "controlMode": "HEAT_PUMP",
        },
        {"serialNumber": "XX0000000001", "type": "XX99_TEST"},
    ]
    topology = Topology.from_api(raw)
    receiver = topology.devices["TR0000000301"]
    assert receiver.room_id == 2
    assert receiver.firmware == "281"
    assert receiver.control_mode is ControlMode.HEAT_PUMP
    unknown = topology.devices["XX0000000001"]
    assert unknown.type is None
    assert unknown.type_raw == "XX99_TEST"
    assert _unknown_value_warnings(caplog) == [
        "Unknown value 'XX99_TEST' for device.type from the tado API; it is treated as unknown"
    ]

    raw["otherDevices"] = []
    assert Topology.from_api(raw).bridge is None


def test_battery_states() -> None:
    """LOW and DEPLETED are low batteries (#85)."""
    devices = _topology().devices
    assert devices["VA0000000101"].battery is BatteryState.NORMAL
    assert devices["VA0000000102"].battery is BatteryState.LOW
    assert devices["VA0000000103"].battery is BatteryState.DEPLETED
    assert devices["VA0000000103"].has_battery
    assert not BatteryState.NORMAL.is_low
    assert BatteryState.LOW.is_low
    assert BatteryState.DEPLETED.is_low


def test_unknown_battery_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """An unknown battery state is None with a single warning per value."""
    raw = load_fixture("rooms_and_devices.json")
    raw["rooms"][0]["devices"][0]["batteryState"] = "CRITICAL_MODELS_TEST"
    raw["rooms"][1]["devices"][0]["batteryState"] = "CRITICAL_MODELS_TEST"
    topology = Topology.from_api(raw)
    Topology.from_api(raw)
    valve = topology.devices["VA0000000101"]
    assert valve.battery is None
    assert valve.has_battery
    assert topology.devices["VA0000000102"].battery is None
    assert len(_unknown_value_warnings(caplog)) == 1


def test_null_battery_is_still_a_battery_device() -> None:
    """A battery field that is null now creates the entity, its value unknown."""
    raw = load_fixture("rooms_and_devices.json")
    raw["rooms"][0]["devices"][0]["batteryState"] = None
    valve = Topology.from_api(raw).devices["VA0000000101"]
    assert valve.has_battery
    assert valve.battery is None


def test_parse_enum() -> None:
    """parse_enum maps null to None and never raises."""
    assert parse_enum(Power, "ON", field="test.power") is Power.ON
    assert parse_enum(Power, None, field="test.power") is None
    assert parse_enum(Power, 1, field="test.power") is None


@pytest.mark.parametrize(
    ("room_id", "expected"),
    [
        (1, Termination.next_time_block()),
        (2, Termination.timer(timedelta(hours=1))),
        (3, Termination.manual()),
        (6, Termination.timer(timedelta(minutes=30))),
    ],
)
def test_room_termination_defaults(room_id: int, expected: Termination) -> None:
    """The termination default is the tado app setting of a room (#94)."""
    assert _topology().rooms[room_id].manual_termination_default == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "TIMER",
        {"type": "TIMER", "durationInSeconds": None},
        {"type": "TIMER"},
        {"type": "TIMER", "durationInSeconds": 30},
        {"type": "UNTIL_TEST"},
        {"durationInSeconds": 60},
    ],
)
def test_room_termination_default_unknown(raw: object) -> None:
    """A missing, unknown or unusable app setting parses to None."""
    assert Termination.from_api(raw) is None


def test_room_termination_default_ignores_stray_duration() -> None:
    """A duration next to a non-timer type is ignored."""
    assert (
        Termination.from_api({"type": "MANUAL", "durationInSeconds": 3600})
        == Termination.manual()
    )


def test_termination_to_api() -> None:
    """Only a TIMER carries durationInSeconds."""
    assert Termination.manual().to_api() == {"type": "MANUAL"}
    assert Termination.next_time_block().to_api() == {"type": "NEXT_TIME_BLOCK"}
    assert Termination.timer(timedelta(minutes=45)).to_api() == {
        "type": "TIMER",
        "durationInSeconds": 2700,
    }


@pytest.mark.parametrize(
    ("kind", "duration"),
    [
        (TerminationType.TIMER, None),
        (TerminationType.TIMER, timedelta(seconds=59)),
        (TerminationType.TIMER, timedelta(hours=24, seconds=1)),
        (TerminationType.MANUAL, timedelta(minutes=5)),
        (TerminationType.NEXT_TIME_BLOCK, timedelta(minutes=5)),
    ],
)
def test_termination_validation(
    kind: TerminationType, duration: timedelta | None
) -> None:
    """A duration is required for, and only for, a TIMER of 60 s to 24 h."""
    with pytest.raises(ValueError):
        Termination(type=kind, duration=duration)


def test_termination_timer_bounds() -> None:
    """60 seconds and 24 hours are valid timers."""
    assert Termination.timer(timedelta(seconds=60)).duration == timedelta(seconds=60)
    assert Termination.timer(timedelta(hours=24)).duration == timedelta(hours=24)


def test_setting_to_api() -> None:
    """Manual control bodies match the catalogue examples."""
    on = Setting(power=Power.ON, temperature=22.5)
    assert {
        "setting": on.to_api(),
        "termination": Termination.timer(timedelta(hours=1)).to_api(),
    } == {
        "setting": {"power": "ON", "isBoost": False, "temperature": {"value": 22.5}},
        "termination": {"type": "TIMER", "durationInSeconds": 3600},
    }
    assert {
        "setting": Setting(power=Power.OFF).to_api(),
        "termination": Termination.manual().to_api(),
    } == {"setting": {"power": "OFF"}, "termination": {"type": "MANUAL"}}
    with pytest.raises(ValueError):
        Setting(power=Power.ON).to_api()


@pytest.mark.parametrize(
    ("fixture", "generation", "is_x"),
    [
        ("home_1000002.json", HomeGeneration.LINE_X, True),
        ("home_1000001.json", HomeGeneration.PRE_LINE_X, False),
    ],
)
def test_home_generation(fixture: str, generation: HomeGeneration, is_x: bool) -> None:
    """The generation tells a tado X home apart (#40)."""
    info = HomeInfo.from_api(load_fixture(fixture))
    assert info.generation is generation
    assert info.is_tado_x is is_x


def test_home_info_fields() -> None:
    """The fields the integration uses from my homes/{id}."""
    info = HomeInfo.from_api(load_fixture("home_1000002.json"))
    assert info.id == 1000002
    assert info.name == "Haus B"
    assert info.time_zone == "Europe/Berlin"
    assert info.temperature_unit == "CELSIUS"
    assert info.supports_flow_temperature is False
    assert info.air_comfort_eligible is True
    assert info.energy_iq_eligible is True


def test_home_generation_missing_or_unknown(caplog: pytest.LogCaptureFixture) -> None:
    """A missing or unknown generation is UNKNOWN, never tado X."""
    raw = load_fixture("home_1000002.json")
    del raw["generation"]
    assert HomeInfo.from_api(raw).generation is HomeGeneration.UNKNOWN
    raw["generation"] = "LINE_Y_TEST"
    info = HomeInfo.from_api(raw)
    assert info.generation is HomeGeneration.UNKNOWN
    assert not info.is_tado_x
    assert len(_unknown_value_warnings(caplog)) == 1


def test_account() -> None:
    """/me gives the user id and the homes, never the email address."""
    account = Account.from_api(load_fixture("me.json"))
    assert account.user_id == "00000000-0000-4000-8000-000000000001"
    assert [(home.id, home.name) for home in account.homes] == [
        (1000001, "Haus A"),
        (1000002, "Haus B"),
    ]
    assert "example.invalid" not in repr(account)


@pytest.mark.parametrize(
    ("fixture", "presence", "locked", "setting"),
    [
        ("state.json", Presence.HOME, False, PresenceSetting.AUTO),
        ("state_locked.json", Presence.AWAY, True, PresenceSetting.AWAY),
    ],
)
def test_home_state(
    fixture: str, presence: Presence, locked: bool, setting: PresenceSetting
) -> None:
    """The presence setting is AUTO while presence is not locked."""
    state = HomeState.from_api(load_fixture(fixture))
    assert state.presence is presence
    assert state.presence_locked is locked
    assert state.setting is setting


def test_home_state_without_auto_assist() -> None:
    """A missing presenceLocked means unlocked."""
    state = HomeState.from_api(
        {"presence": "HOME", "showHomePresenceSwitchButton": True}
    )
    assert not state.presence_locked
    assert state.setting is PresenceSetting.AUTO
    assert HomeState(presence=Presence.HOME, presence_locked=True).setting is (
        PresenceSetting.HOME
    )


def test_weather() -> None:
    """Outdoor temperature, solar intensity and state."""
    assert Weather.from_api(load_fixture("weather.json")) == Weather(
        outdoor_temperature=8.4,
        solar_intensity=12.0,
        state=WeatherState.CLOUDY_PARTLY,
    )
    assert Weather.from_api({}) == Weather()


def test_air_comfort_humidity_mapping() -> None:
    """The humidity level comes from humidityLevel; freshness is home-wide."""
    comfort = AirComfort.from_api(load_fixture("air_comfort.json"))
    assert comfort.freshness == "FAIR"
    assert comfort.rooms[2].humidity_level is HumidityLevel.HUMID
    assert comfort.rooms[2].temperature_level is TemperatureLevel.COMFY
    assert comfort.rooms[4].humidity_level is HumidityLevel.DRY
    assert comfort.rooms[3].temperature_level is TemperatureLevel.COLD
    assert comfort.rooms[6].humidity_level is None
    assert comfort.rooms[6].temperature_level is None


def test_mobile_devices() -> None:
    """Phones with and without geofencing and location."""
    parsed = MobileDevices.from_api(load_fixture("mobile_devices.json"))
    assert parsed.complete
    phones = parsed.phones
    assert list(phones) == [5000001, 5000002, 5000003]
    home = phones[5000001]
    assert home.geo_tracking
    assert home.at_home is True
    assert home.stale is False
    assert (home.platform, home.os_version, home.model) == ("Android", "15", "Model1")
    assert phones[5000002].at_home is False
    assert phones[5000002].stale is True
    tablet = phones[5000003]
    assert not tablet.geo_tracking
    assert tablet.at_home is None
    assert tablet.stale is None


@pytest.mark.parametrize(
    ("change", "readable"),
    [
        (lambda raw: raw[1].update(id="5000002"), [5000001, 5000003]),
        (lambda raw: raw.append("not a phone"), [5000001, 5000002, 5000003]),
    ],
)
def test_skipped_phone_makes_the_list_incomplete(
    change: Callable[[list[Any]], object], readable: list[int]
) -> None:
    """A phone the parser skips is noted; the readable ones are kept."""
    raw = load_fixture("mobile_devices.json")
    change(raw)

    parsed = MobileDevices.from_api(raw)

    assert not parsed.complete
    assert list(parsed.phones) == readable


def test_running_times() -> None:
    """Running time per room id, summed over the day's intervals."""
    raw = load_fixture("running_times.json")
    times = RunningTimes.from_api(raw, day=TODAY)
    assert times.day == TODAY
    assert times.by_room[1] == timedelta(hours=1)
    assert times.by_room[2] == timedelta(minutes=45)
    assert times.by_room[3] == timedelta(0)

    raw["runningTimes"].append(
        {"zones": [{"id": 1, "runningTimeInSeconds": 600}, {"id": "bad"}]}
    )
    assert RunningTimes.from_api(raw, day=TODAY).by_room[1] == timedelta(minutes=70)
    assert RunningTimes.from_api({}, day=TODAY).by_room == {}


def test_flow_temperature() -> None:
    """Maximum, constraints and auto-adaptation; constraints default to 20-75."""
    flow = FlowTemperature.from_api(load_fixture("flow_temperature.json"))
    assert flow == FlowTemperature(
        max_flow_temperature=50, minimum=30, maximum=80, auto_adaptation=False
    )
    bare = FlowTemperature.from_api({"autoAdaptation": {"enabled": True}})
    assert (bare.minimum, bare.maximum, bare.auto_adaptation) == (20, 75, True)
    assert bare.max_flow_temperature is None


def test_holiday_mode() -> None:
    """The raw payload is kept for read-modify-write."""
    raw = load_fixture("holiday_mode.json")
    holiday = HolidayMode.from_api(raw)
    assert holiday.enabled is False
    assert holiday.start is None
    assert holiday.raw == raw

    raw |= {
        "enabled": True,
        "startDate": "2026-12-24",
        "endDate": "2027-01-02T10:00:00Z",
    }
    holiday = HolidayMode.from_api(raw)
    assert holiday.start == datetime(2026, 12, 24, tzinfo=UTC)
    assert holiday.end == datetime(2027, 1, 2, 10, tzinfo=UTC)


def test_day_report() -> None:
    """The day report is kept raw."""
    raw = load_fixture("day_report.json")
    report = DayReport.from_api(raw, day=TODAY)
    assert report.day == TODAY
    assert report.raw == raw
    with pytest.raises(TadoXParseError):
        DayReport.from_api([], day=TODAY)


def test_schedule() -> None:
    """Weekly schedule with 24:00 as the end of the day."""
    raw = load_fixture("schedule.json")
    schedule = RoomSchedule.from_api(raw, room_id=1)
    assert schedule.room_id == 1
    assert len(schedule.blocks) == 21
    monday = schedule.day(DayType.MONDAY)
    assert [(block.start_minute, block.end_minute) for block in monday] == [
        (0, 390),
        (390, 1320),
        (1320, 1440),
    ]
    assert monday[1].setting == Setting(power=Power.ON, temperature=21.0)
    assert [block.to_api() for block in monday] == raw["schedule"][:3]


def test_schedule_skips_unknown_blocks(caplog: pytest.LogCaptureFixture) -> None:
    """Aggregate day types and bad times are skipped."""
    raw = load_fixture("schedule.json")
    raw["schedule"][0]["dayType"] = "MONDAY_TO_FRIDAY"
    raw["schedule"][1]["start"] = "25:00"
    raw["schedule"][2]["end"] = "6.30"
    raw["schedule"][3]["start"] = "06:75"
    raw["schedule"][4]["end"] = 1320
    schedule = RoomSchedule.from_api(raw, room_id=1)
    assert len(schedule.blocks) == 16
    assert schedule.day(DayType.MONDAY) == ()
    with pytest.raises(TadoXParseError):
        RoomSchedule.from_api({"room": {"id": 1}}, room_id=1)


def test_schedule_block_off_to_api() -> None:
    """An OFF block is sent with a null temperature."""
    block = ScheduleBlock(
        day=DayType.TUESDAY,
        start_minute=1320,
        end_minute=1440,
        setting=Setting(power=Power.OFF, temperature=None),
    )
    assert block.to_api() == {
        "dayType": "TUESDAY",
        "start": "22:00",
        "end": "24:00",
        "setting": {"power": "OFF", "temperature": None},
    }


def test_utcnow_is_aware() -> None:
    """The default clock of the client is aware UTC."""
    assert utcnow().tzinfo is UTC


def test_format_clock() -> None:
    """Minutes after midnight as HH:MM."""
    assert format_clock(0) == "00:00"
    assert format_clock(390) == "06:30"
    assert format_clock(1440) == "24:00"


def test_token_set_from_response() -> None:
    """The expiry counts from the time the request was sent."""
    tokens = TokenSet.from_token_response(
        load_fixture("token.json"), sent_at=NOW, previous_refresh="old"
    )
    assert tokens.access_token == "fake.jwt.access-token"
    assert tokens.refresh_token == "fake-refresh-token-0001"
    assert tokens.expires_at == NOW + timedelta(seconds=599)


def test_token_set_without_refresh_token() -> None:
    """A response without a refresh token keeps the previous one."""
    body = load_fixture("token.json")
    del body["refresh_token"]
    tokens = TokenSet.from_token_response(
        body, sent_at=NOW, previous_refresh="fake-refresh-token-previous"
    )
    assert tokens.refresh_token == "fake-refresh-token-previous"
    with pytest.raises(TadoXParseError):
        TokenSet.from_token_response(body, sent_at=NOW, previous_refresh=None)


def test_token_set_without_access_token_or_lifetime() -> None:
    """No access token is a parse error; no lifetime means 600 seconds."""
    body = load_fixture("token.json")
    del body["expires_in"]
    tokens = TokenSet.from_token_response(body, sent_at=NOW, previous_refresh=None)
    assert tokens.expires_at == NOW + timedelta(seconds=600)
    del body["access_token"]
    with pytest.raises(TadoXParseError):
        TokenSet.from_token_response(body, sent_at=NOW, previous_refresh=None)


def test_token_set_freshness_and_repr() -> None:
    """Fresh means valid for more than the margin; secrets stay out of repr."""
    tokens = TokenSet(
        access_token="fake-secret-access",
        refresh_token="fake-secret-refresh",
        expires_at=NOW + timedelta(seconds=120),
    )
    assert tokens.is_fresh(NOW)
    assert not tokens.is_fresh(NOW + timedelta(seconds=60))
    assert tokens.is_fresh(NOW + timedelta(seconds=60), margin=timedelta(0))
    assert "fake-secret" not in repr(tokens)


def test_device_authorization() -> None:
    """The verification URL always carries the user code and the client id (#76)."""
    auth = DeviceAuthorization.from_api(
        load_fixture("device_authorize.json"), received_at=NOW
    )
    url = URL(auth.verification_url)
    assert url.with_query(None) == URL("https://login.tado.com/oauth2/device")
    assert url.query["user_code"] == "ABC123"
    assert url.query["client_id"] == CLIENT_ID
    assert auth.device_code == "fake-device-code-0000000000000000"
    assert auth.interval == timedelta(seconds=5)
    assert auth.expires_at == NOW + timedelta(seconds=300)
    assert "ABC123" not in repr(auth)
    assert "fake-device-code" not in repr(auth)


def test_device_authorization_limits() -> None:
    """The interval is at least 5 seconds; a relative URI is rejected."""
    raw = load_fixture("device_authorize.json")
    raw["interval"] = 1
    raw["verification_uri"] = "https://login.tado.com/oauth2/device?user_code=OLD"
    auth = DeviceAuthorization.from_api(raw, received_at=NOW)
    assert auth.interval == timedelta(seconds=5)
    assert URL(auth.verification_url).query.getall("user_code") == ["ABC123"]

    raw["verification_uri"] = "/oauth2/device"
    with pytest.raises(TadoXParseError):
        DeviceAuthorization.from_api(raw, received_at=NOW)


def test_rate_limit_reset_at() -> None:
    """The reset time is known only when tado sent t=."""
    info = RateLimitInfo(
        limit=20000,
        window=timedelta(days=1),
        remaining=0,
        reset_in=timedelta(seconds=7082),
        observed_at=NOW,
    )
    assert info.reset_at == NOW + timedelta(seconds=7082)
    assert copy.replace(info, reset_in=None).reset_at is None

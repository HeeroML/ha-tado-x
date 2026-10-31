"""Typed models of the tado X API and their boundary parsers.

``from_api`` is the only code that reads raw payload keys. A bad required key
raises TadoXParseError, an optional key that is missing or has the wrong type
becomes None, and a malformed item of a list is skipped, with one warning per
kind, so that one bad room does not fail a whole poll. Datetimes are aware UTC.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
import logging
import re
from typing import Any, Final, Self

from yarl import URL

from .const import (
    CLIENT_ID,
    DEFAULT_DEVICE_CODE_LIFETIME,
    DEFAULT_TOKEN_LIFETIME,
    DEVICE_FLOW_MIN_INTERVAL,
    MAX_TIMER_DURATION,
    MIN_TIMER_DURATION,
    REFRESH_MARGIN,
)
from .enums import (
    BatteryState,
    ConnectionState,
    ControlMode,
    DayType,
    DeviceType,
    HomeGeneration,
    HumidityLevel,
    Power,
    Presence,
    PresenceSetting,
    TemperatureLevel,
    TerminationType,
    WeatherState,
    parse_enum,
)
from .errors import TadoXParseError
from .telemetry import WARN_ONCE, describe_value

_LOGGER = logging.getLogger(__package__)

_INDEX: Final = re.compile(r"\[\d+\]")

_MINUTES_PER_DAY = 1440
_DEFAULT_FLOW_MINIMUM = 20
_DEFAULT_FLOW_MAXIMUM = 75


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def _parse_datetime(raw: object) -> datetime | None:
    """Parse an ISO 8601 timestamp; a naive value is taken as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_clock(raw: object) -> int | None:
    """Parse "HH:MM" into minutes after midnight; "24:00" is 1440."""
    if not isinstance(raw, str):
        return None
    hours, sep, minutes = raw.partition(":")
    if not sep or not (hours.isdigit() and minutes.isdigit()) or len(minutes) != 2:
        return None
    value = int(hours) * 60 + int(minutes)
    if int(minutes) >= 60 or value > _MINUTES_PER_DAY:
        return None
    return value


def format_clock(minute: int) -> str:
    """Format minutes after midnight as "HH:MM"; 1440 is "24:00"."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


class _Reader:
    """Typed, path-aware access to one JSON object of a payload."""

    __slots__ = ("_data", "_endpoint", "_path")

    def __init__(self, raw: object, endpoint: str, path: str = "$") -> None:
        if not isinstance(raw, Mapping):
            raise TadoXParseError(endpoint, path, found=describe_value(raw))
        self._data: Mapping[str, Any] = raw
        self._endpoint = endpoint
        self._path = path

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    @property
    def path(self) -> str:
        return self._path

    def describe(self, key: str) -> str:
        """Describe the value of a key without revealing it; "missing" if absent."""
        if key not in self._data:
            return "missing"
        return describe_value(self._data[key])

    def _error(self, key: str) -> TadoXParseError:
        return TadoXParseError(
            self._endpoint, f"{self._path}.{key}", found=self.describe(key)
        )

    def child(self, key: str) -> _Reader:
        return _Reader(self._data.get(key), self._endpoint, f"{self._path}.{key}")

    def opt_child(self, key: str) -> _Reader | None:
        value = self._data.get(key)
        if not isinstance(value, Mapping):
            return None
        return _Reader(value, self._endpoint, f"{self._path}.{key}")

    def items(self, key: str) -> list[object]:
        value = self._data.get(key)
        if not isinstance(value, list):
            raise self._error(key)
        return value

    def opt_items(self, key: str) -> list[object]:
        value = self._data.get(key)
        return value if isinstance(value, list) else []

    def req_int(self, key: str) -> int:
        if (value := self.opt_int(key)) is None:
            raise self._error(key)
        return value

    def req_str(self, key: str) -> str:
        value = self._data.get(key)
        if not isinstance(value, str):
            raise self._error(key)
        return value

    def opt_int(self, key: str) -> int | None:
        value = self._data.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None

    def opt_float(self, key: str) -> float | None:
        value = self._data.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    def opt_str(self, key: str) -> str | None:
        value = self._data.get(key)
        return value if isinstance(value, str) else None

    def opt_bool(self, key: str) -> bool | None:
        value = self._data.get(key)
        return value if isinstance(value, bool) else None

    def opt_datetime(self, key: str) -> datetime | None:
        return _parse_datetime(self._data.get(key))

    def req_clock(self, key: str) -> int:
        if (value := parse_clock(self._data.get(key))) is None:
            raise self._error(key)
        return value

    def opt_seconds(self, key: str) -> timedelta | None:
        value = self.opt_int(key)
        if value is None:
            return None
        try:
            return timedelta(seconds=value)
        except OverflowError:
            return None

    def enum[E: StrEnum](self, cls: type[E], key: str, *, field: str) -> E | None:
        return parse_enum(cls, self._data.get(key), field=field)

    def req_enum[E: StrEnum](self, cls: type[E], key: str, *, field: str) -> E:
        if (value := self.enum(cls, key, field=field)) is None:
            raise self._error(key)
        return value


def _require_list(raw: object, endpoint: str) -> list[object]:
    if not isinstance(raw, list):
        raise TadoXParseError(endpoint, "$", found=describe_value(raw))
    return raw


def _log_skip(err: TadoXParseError) -> None:
    """Log a skipped item by its path and what was found there, never the value.

    The first skip of each endpoint and path is a warning, repeats are debug
    messages.
    """
    kind = ("skip", err.endpoint, _INDEX.sub("[]", err.path))
    _LOGGER.log(
        logging.WARNING if WARN_ONCE.should_warn(kind) else logging.DEBUG,
        "Skipping a malformed %s item: %s is %s",
        err.endpoint,
        err.path,
        err.found or "invalid",
    )


def _parse_items[T](
    items: Iterable[object],
    parse: Callable[[_Reader], T],
    endpoint: str,
    path: str,
    skipped: list[str] | None = None,
) -> list[T]:
    """Parse list items, skipping the malformed ones.

    The path of every skipped item is appended to skipped, if given.
    """
    parsed: list[T] = []
    for index, item in enumerate(items):
        try:
            parsed.append(parse(_Reader(item, endpoint, f"{path}[{index}]")))
        except TadoXParseError as err:
            _log_skip(err)
            if skipped is not None:
                skipped.append(err.path)
    return parsed


@dataclass(frozen=True, slots=True, kw_only=True)
class Termination:
    """How a manual control ends: what is sent, and a room's app default."""

    type: TerminationType
    duration: timedelta | None = None

    def __post_init__(self) -> None:
        """Validate that a duration is given for, and only for, TIMER."""
        if self.type is not TerminationType.TIMER:
            if self.duration is not None:
                raise ValueError(f"{self.type} takes no duration")
            return
        if self.duration is None:
            raise ValueError("a TIMER termination needs a duration")
        if not MIN_TIMER_DURATION <= self.duration <= MAX_TIMER_DURATION:
            raise ValueError("the timer must be between 60 seconds and 24 hours")

    @classmethod
    def manual(cls) -> Self:
        """Return a termination that lasts until the user ends it."""
        return cls(type=TerminationType.MANUAL)

    @classmethod
    def next_time_block(cls) -> Self:
        """Return a termination that ends at the next schedule block."""
        return cls(type=TerminationType.NEXT_TIME_BLOCK)

    @classmethod
    def timer(cls, duration: timedelta) -> Self:
        """Return a termination that ends after the given duration."""
        return cls(type=TerminationType.TIMER, duration=duration)

    def to_api(self) -> dict[str, Any]:
        """Return the request body; only TIMER carries a duration."""
        body: dict[str, Any] = {"type": self.type.value}
        if self.duration is not None:
            body["durationInSeconds"] = int(self.duration.total_seconds())
        return body

    @classmethod
    def from_api(cls, raw: object) -> Self | None:
        """Parse a room's deviceManualControlTermination.

        Return None when the value is missing or unknown, or when it is a
        TIMER without a usable duration.
        """
        if not isinstance(raw, Mapping):
            return None
        reader = _Reader(raw, "topology")
        kind = reader.enum(
            TerminationType, "type", field="deviceManualControlTermination.type"
        )
        if kind is None:
            return None
        if kind is not TerminationType.TIMER:
            return cls(type=kind)
        if (duration := reader.opt_seconds("durationInSeconds")) is None:
            return None
        try:
            return cls.timer(duration)
        except ValueError:
            _LOGGER.debug("Ignoring a room timer default of %s", duration)
            return None


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveOverride:
    """A running manual control or boost of a room."""

    type: TerminationType | None = None
    remaining: timedelta | None = None
    projected_expiry: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Setting:
    """Target of a room; temperature is None while the power is off."""

    power: Power
    temperature: float | None = None

    def to_api(self) -> dict[str, Any]:
        """Return the manual control setting body."""
        if self.power is Power.OFF:
            return {"power": Power.OFF.value}
        if self.temperature is None:
            raise ValueError("an ON setting needs a temperature")
        return {
            "power": Power.ON.value,
            "isBoost": False,
            "temperature": {"value": self.temperature},
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NextScheduleChange:
    """The next change of the schedule; it is kept even when in the past."""

    start: datetime
    setting: Setting


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenWindow:
    """Open-window mode of a room."""

    activated: bool
    expires_in: timedelta | None = None


def _setting(reader: _Reader) -> Setting:
    power = reader.req_enum(Power, "power", field="setting.power")
    temperature = reader.opt_child("temperature")
    return Setting(
        power=power,
        temperature=temperature.opt_float("value") if temperature else None,
    )


def _active_override(reader: _Reader | None, field: str) -> ActiveOverride | None:
    if reader is None:
        return None
    return ActiveOverride(
        type=reader.enum(TerminationType, "type", field=field),
        remaining=reader.opt_seconds("remainingTimeInSeconds"),
        projected_expiry=reader.opt_datetime("projectedExpiry"),
    )


def _next_schedule_change(reader: _Reader | None) -> NextScheduleChange | None:
    if reader is None or (start := reader.opt_datetime("start")) is None:
        return None
    try:
        setting = _setting(reader.child("setting"))
    except TadoXParseError:
        return None
    return NextScheduleChange(start=start, setting=setting)


def _open_window(reader: _Reader | None) -> OpenWindow | None:
    if reader is None:
        return None
    activated = reader.opt_bool("activated")
    return OpenWindow(
        activated=True if activated is None else activated,
        expires_in=reader.opt_seconds("expiryInSeconds"),
    )


def _connection(reader: _Reader, field: str) -> ConnectionState | None:
    connection = reader.opt_child("connection")
    if connection is None:
        return None
    return connection.enum(ConnectionState, "state", field=field)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomState:
    """Live state of one room (hops GET rooms)."""

    id: int
    name: str
    setting: Setting
    current_temperature: float | None = None
    humidity: float | None = None
    manual_control: ActiveOverride | None = None
    boost: ActiveOverride | None = None
    heating_power: int | None = None
    connection: ConnectionState | None = None
    open_window: OpenWindow | None = None
    next_schedule_change: NextScheduleChange | None = None
    next_time_block: datetime | None = None

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse one room of GET rooms or GET rooms/{id}."""
        return cls._parse(_Reader(raw, "rooms"))

    @classmethod
    def list_from_api(cls, raw: object) -> dict[int, Self]:
        """Parse GET rooms into rooms by id, skipping malformed rooms."""
        rooms = _parse_items(_require_list(raw, "rooms"), cls._parse, "rooms", "$")
        return {room.id: room for room in rooms}

    @classmethod
    def _parse(cls, reader: _Reader) -> Self:
        sensors = reader.opt_child("sensorDataPoints")
        inside = sensors.opt_child("insideTemperature") if sensors else None
        humidity = sensors.opt_child("humidity") if sensors else None
        heating = reader.opt_child("heatingPower")
        next_block = reader.opt_child("nextTimeBlock")
        return cls(
            id=reader.req_int("id"),
            name=reader.req_str("name"),
            setting=_setting(reader.child("setting")),
            current_temperature=inside.opt_float("value") if inside else None,
            humidity=humidity.opt_float("percentage") if humidity else None,
            manual_control=_active_override(
                reader.opt_child("manualControlTermination"),
                "manualControlTermination.type",
            ),
            boost=_active_override(reader.opt_child("boostMode"), "boostMode.type"),
            heating_power=heating.opt_int("percentage") if heating else None,
            connection=_connection(reader, "rooms.connection.state"),
            open_window=_open_window(reader.opt_child("openWindow")),
            next_schedule_change=_next_schedule_change(
                reader.opt_child("nextScheduleChange")
            ),
            next_time_block=next_block.opt_datetime("start") if next_block else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Device:
    """A physical device of roomsAndDevices (rooms[].devices[] or otherDevices[])."""

    serial: str
    type: DeviceType | None
    type_raw: str
    firmware: str | None = None
    connection: ConnectionState | None = None
    has_battery: bool = False
    battery: BatteryState | None = None
    mounting_state: str | None = None
    child_lock: bool | None = None
    temperature_measured: float | None = None
    temperature_offset: float | None = None
    control_mode: ControlMode | None = None
    room_id: int | None = None


def _device(reader: _Reader, room_id: int | None) -> Device:
    type_raw = reader.req_str("type")
    firmware = reader.data.get("firmwareVersion")
    return Device(
        serial=reader.req_str("serialNumber"),
        type=parse_enum(DeviceType, type_raw, field="device.type"),
        type_raw=type_raw,
        firmware=None
        if firmware is None or isinstance(firmware, bool)
        else str(firmware),
        connection=_connection(reader, "device.connection.state"),
        has_battery="batteryState" in reader.data,
        battery=reader.enum(BatteryState, "batteryState", field="device.batteryState"),
        mounting_state=reader.opt_str("mountingState"),
        child_lock=reader.opt_bool("childLockEnabled"),
        temperature_measured=reader.opt_float("temperatureAsMeasured"),
        temperature_offset=reader.opt_float("temperatureOffset"),
        control_mode=reader.enum(
            ControlMode, "controlMode", field="device.controlMode"
        ),
        room_id=room_id if room_id is not None else reader.opt_int("roomId"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomTopology:
    """Static data of one room: its devices and the app's termination default."""

    id: int
    name: str
    manual_termination_default: Termination | None = None
    devices: tuple[Device, ...] = ()


def _devices(
    reader: _Reader, key: str, room_id: int | None, skipped: list[str]
) -> tuple[Device, ...]:
    """Parse a device list of roomsAndDevices; a missing list counts as skipped."""
    path = f"{reader.path}.{key}"
    items = reader.data.get(key)
    if not isinstance(items, list):
        _log_skip(TadoXParseError("topology", path, found=reader.describe(key)))
        skipped.append(path)
        return ()
    return tuple(
        _parse_items(
            items, lambda item: _device(item, room_id), "topology", path, skipped
        )
    )


def _room_topology(reader: _Reader, skipped: list[str]) -> RoomTopology:
    room_id = reader.req_int("roomId")
    return RoomTopology(
        id=room_id,
        name=reader.req_str("roomName"),
        manual_termination_default=Termination.from_api(
            reader.data.get("deviceManualControlTermination")
        ),
        devices=_devices(reader, "devices", room_id, skipped),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Topology:
    """Rooms and devices of a home (hops GET roomsAndDevices).

    complete is False when a room or device was skipped as malformed or a
    device list was missing: such a topology cannot tell a removed device
    from one that could not be read.
    """

    rooms: Mapping[int, RoomTopology]
    other_devices: tuple[Device, ...] = ()
    complete: bool = True

    @property
    def devices(self) -> Mapping[str, Device]:
        """Return every device of the home by serial."""
        devices = {
            device.serial: device
            for room in self.rooms.values()
            for device in room.devices
        }
        devices.update((device.serial, device) for device in self.other_devices)
        return devices

    @property
    def bridge(self) -> Device | None:
        """Return the first Bridge X of the home."""
        return next(
            (
                device
                for device in self.devices.values()
                if device.type is DeviceType.IB02
            ),
            None,
        )

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET roomsAndDevices."""
        reader = _Reader(raw, "topology")
        skipped: list[str] = []
        rooms = _parse_items(
            reader.items("rooms"),
            lambda item: _room_topology(item, skipped),
            "topology",
            "$.rooms",
            skipped,
        )
        other_devices = _devices(reader, "otherDevices", None, skipped)
        return cls(
            rooms={room.id: room for room in rooms},
            other_devices=other_devices,
            complete=not skipped,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeRef:
    """A home listed by /me."""

    id: int
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Account:
    """The tado account (my GET /me); the email address is never parsed."""

    user_id: str
    homes: tuple[HomeRef, ...]

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET /me."""
        reader = _Reader(raw, "me")
        homes = _parse_items(
            reader.items("homes"),
            lambda item: HomeRef(id=item.req_int("id"), name=item.req_str("name")),
            "me",
            "$.homes",
        )
        return cls(user_id=reader.req_str("id"), homes=tuple(homes))


# The keys HomeInfo.from_api reads; only these of a home's details are cached.
_HOME_INFO_KEYS: Final = (
    "id",
    "name",
    "generation",
    "dateTimeZone",
    "temperatureUnit",
    "supportsFlowTemperatureOptimization",
    "isAirComfortEligible",
    "isEnergyIqEligible",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeInfo:
    """Details of a home (my GET homes/{id})."""

    id: int
    name: str
    generation: HomeGeneration
    time_zone: str | None = None
    temperature_unit: str | None = None
    supports_flow_temperature: bool | None = None
    air_comfort_eligible: bool | None = None
    energy_iq_eligible: bool | None = None

    @property
    def is_tado_x(self) -> bool:
        """Return True for a tado X (LINE_X) home."""
        return self.generation is HomeGeneration.LINE_X

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET homes/{id}."""
        reader = _Reader(raw, "home_info")
        generation = reader.enum(HomeGeneration, "generation", field="home.generation")
        return cls(
            id=reader.req_int("id"),
            name=reader.req_str("name"),
            generation=generation or HomeGeneration.UNKNOWN,
            time_zone=reader.opt_str("dateTimeZone"),
            temperature_unit=reader.opt_str("temperatureUnit"),
            supports_flow_temperature=reader.opt_bool(
                "supportsFlowTemperatureOptimization"
            ),
            air_comfort_eligible=reader.opt_bool("isAirComfortEligible"),
            energy_iq_eligible=reader.opt_bool("isEnergyIqEligible"),
        )

    @staticmethod
    def cache_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return the part of GET homes/{id} that from_api reads.

        The owner's contact details, postal address and location are left out,
        so they never reach the cache on disk.
        """
        return {key: raw[key] for key in _HOME_INFO_KEYS if key in raw}


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeState:
    """Presence of the home (my GET homes/{id}/state)."""

    presence: Presence
    presence_locked: bool = False

    @property
    def setting(self) -> PresenceSetting:
        """Return the presence setting; AUTO while geofencing decides."""
        if not self.presence_locked:
            return PresenceSetting.AUTO
        return PresenceSetting(self.presence.value)

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET homes/{id}/state; a missing presenceLocked means unlocked."""
        reader = _Reader(raw, "home_state")
        return cls(
            presence=reader.req_enum(Presence, "presence", field="state.presence"),
            presence_locked=reader.opt_bool("presenceLocked") is True,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Weather:
    """Weather at the home (my GET homes/{id}/weather)."""

    outdoor_temperature: float | None = None
    solar_intensity: float | None = None
    state: WeatherState | None = None

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET homes/{id}/weather."""
        reader = _Reader(raw, "weather")
        outside = reader.opt_child("outsideTemperature")
        solar = reader.opt_child("solarIntensity")
        state = reader.opt_child("weatherState")
        return cls(
            outdoor_temperature=outside.opt_float("celsius") if outside else None,
            solar_intensity=solar.opt_float("percentage") if solar else None,
            state=state.enum(WeatherState, "value", field="weatherState.value")
            if state
            else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MobileDevice:
    """A phone of the home (my GET homes/{id}/mobileDevices)."""

    id: int
    name: str
    geo_tracking: bool = False
    at_home: bool | None = None
    stale: bool | None = None
    platform: str | None = None
    os_version: str | None = None
    model: str | None = None


def _mobile_device(reader: _Reader) -> MobileDevice:
    settings = reader.opt_child("settings")
    location = reader.opt_child("location")
    metadata = reader.opt_child("deviceMetadata")
    return MobileDevice(
        id=reader.req_int("id"),
        name=reader.req_str("name"),
        geo_tracking=settings is not None
        and settings.opt_bool("geoTrackingEnabled") is True,
        at_home=location.opt_bool("atHome") if location else None,
        stale=location.opt_bool("stale") if location else None,
        platform=metadata.opt_str("platform") if metadata else None,
        os_version=metadata.opt_str("osVersion") if metadata else None,
        model=metadata.opt_str("model") if metadata else None,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MobileDevices:
    """The phones of a home by id (my GET homes/{id}/mobileDevices).

    complete is False when a phone was skipped as malformed: such a list
    cannot tell a removed phone from one that could not be read.
    """

    phones: Mapping[int, MobileDevice]
    complete: bool = True

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET mobileDevices."""
        skipped: list[str] = []
        phones = _parse_items(
            _require_list(raw, "mobile_devices"),
            _mobile_device,
            "mobile_devices",
            "$",
            skipped,
        )
        return cls(phones={phone.id: phone for phone in phones}, complete=not skipped)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomComfort:
    """Air comfort levels of one room."""

    room_id: int
    temperature_level: TemperatureLevel | None = None
    humidity_level: HumidityLevel | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AirComfort:
    """Air comfort of the home (hops GET airComfort)."""

    freshness: str | None
    rooms: Mapping[int, RoomComfort]

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET airComfort; humidityLevel is the room humidity level."""
        reader = _Reader(raw, "air_comfort")
        freshness = reader.opt_child("freshness")
        rooms = _parse_items(
            reader.opt_items("comfort"),
            lambda item: RoomComfort(
                room_id=item.req_int("roomId"),
                temperature_level=item.enum(
                    TemperatureLevel,
                    "temperatureLevel",
                    field="airComfort.temperatureLevel",
                ),
                humidity_level=item.enum(
                    HumidityLevel, "humidityLevel", field="airComfort.humidityLevel"
                ),
            ),
            "air_comfort",
            "$.comfort",
        )
        return cls(
            freshness=freshness.opt_str("value") if freshness else None,
            rooms={room.room_id: room for room in rooms},
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FlowTemperature:
    """Flow temperature optimization of an OpenTherm boiler."""

    max_flow_temperature: int | None
    minimum: int
    maximum: int
    auto_adaptation: bool

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET settings/flowTemperatureOptimization."""
        reader = _Reader(raw, "flow_temperature")
        constraints = reader.opt_child("maxFlowTemperatureConstraints")
        adaptation = reader.opt_child("autoAdaptation")
        minimum = constraints.opt_int("min") if constraints else None
        maximum = constraints.opt_int("max") if constraints else None
        return cls(
            max_flow_temperature=reader.opt_int("maxFlowTemperature"),
            minimum=_DEFAULT_FLOW_MINIMUM if minimum is None else minimum,
            maximum=_DEFAULT_FLOW_MAXIMUM if maximum is None else maximum,
            auto_adaptation=adaptation is not None
            and adaptation.opt_bool("enabled") is True,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunningTimes:
    """Heating running time per room for one day (minder runningTimes)."""

    day: date
    by_room: Mapping[int, timedelta]

    @classmethod
    def from_api(cls, raw: object, *, day: date) -> Self:
        """Parse GET runningTimes for a single day, summing every interval."""
        reader = _Reader(raw, "running_times")
        totals: dict[int, int] = {}
        intervals = _parse_items(
            reader.opt_items("runningTimes"),
            lambda item: item,
            "running_times",
            "$.runningTimes",
        )
        for interval in intervals:
            zones = _parse_items(
                interval.opt_items("zones"),
                lambda item: (item.req_int("id"), item.req_int("runningTimeInSeconds")),
                "running_times",
                "$.runningTimes[].zones",
            )
            for zone_id, seconds in zones:
                totals[zone_id] = totals.get(zone_id, 0) + seconds
        return cls(
            day=day,
            by_room={
                room_id: timedelta(seconds=seconds)
                for room_id, seconds in totals.items()
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleBlock:
    """One block of a room schedule; minutes run from 0 to 1440."""

    day: DayType
    start_minute: int
    end_minute: int
    setting: Setting

    def to_api(self) -> dict[str, Any]:
        """Return the block as sent in a day schedule POST."""
        temperature = (
            {"value": self.setting.temperature}
            if self.setting.power is Power.ON and self.setting.temperature is not None
            else None
        )
        return {
            "dayType": self.day.value,
            "start": format_clock(self.start_minute),
            "end": format_clock(self.end_minute),
            "setting": {"power": self.setting.power.value, "temperature": temperature},
        }


def _schedule_block(reader: _Reader) -> ScheduleBlock:
    return ScheduleBlock(
        day=reader.req_enum(DayType, "dayType", field="schedule.dayType"),
        start_minute=reader.req_clock("start"),
        end_minute=reader.req_clock("end"),
        setting=_setting(reader.child("setting")),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomSchedule:
    """Weekly schedule of a room (hops GET rooms/{id}/schedule)."""

    room_id: int
    blocks: tuple[ScheduleBlock, ...]

    def day(self, day: DayType) -> tuple[ScheduleBlock, ...]:
        """Return the blocks of one weekday in time order."""
        return tuple(
            sorted(
                (block for block in self.blocks if block.day is day),
                key=lambda block: block.start_minute,
            )
        )

    @classmethod
    def from_api(cls, raw: object, *, room_id: int) -> Self:
        """Parse GET rooms/{id}/schedule; blocks of unknown day types are skipped."""
        reader = _Reader(raw, "schedule")
        blocks = _parse_items(
            reader.items("schedule"), _schedule_block, "schedule", "$.schedule"
        )
        return cls(room_id=room_id, blocks=tuple(blocks))


@dataclass(frozen=True, slots=True, kw_only=True)
class DayReport:
    """Historic report of one room and day (my GET zones/{id}/dayReport)."""

    day: date
    raw: Mapping[str, Any]

    @classmethod
    def from_api(cls, raw: object, *, day: date) -> Self:
        """Wrap a day report; it is summarized on demand."""
        return cls(day=day, raw=dict(_Reader(raw, "day_report").data))


@dataclass(frozen=True, slots=True, kw_only=True)
class HolidayMode:
    """Holiday mode of the home; raw is the base for read-modify-write."""

    enabled: bool | None
    active: bool | None
    start: datetime | None
    end: datetime | None
    raw: Mapping[str, Any]

    @classmethod
    def from_api(cls, raw: object) -> Self:
        """Parse GET settings/holidayMode."""
        reader = _Reader(raw, "holiday_mode")
        return cls(
            enabled=reader.opt_bool("enabled"),
            active=reader.opt_bool("active"),
            start=reader.opt_datetime("startDate"),
            end=reader.opt_datetime("endDate"),
            raw=dict(reader.data),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenSet:
    """OAuth tokens of one grant; expires_at is absolute UTC."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime

    def is_fresh(self, now: datetime, margin: timedelta = REFRESH_MARGIN) -> bool:
        """Return True while the access token is valid for at least the margin."""
        return now + margin < self.expires_at

    @classmethod
    def from_token_response(
        cls, body: object, *, sent_at: datetime, previous_refresh: str | None
    ) -> Self:
        """Parse a 200 token response.

        The expiry counts from the time the request was sent. A response
        without a refresh token keeps the previous one.
        """
        reader = _Reader(body, "token")
        lifetime = reader.opt_seconds("expires_in")
        if lifetime is None:
            lifetime = DEFAULT_TOKEN_LIFETIME
        refresh = reader.opt_str("refresh_token") or previous_refresh
        if not refresh:
            raise TadoXParseError("token", "$.refresh_token")
        return cls(
            access_token=reader.req_str("access_token"),
            refresh_token=refresh,
            expires_at=sent_at + lifetime,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceAuthorization:
    """A started device-code login."""

    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_url: str = field(repr=False)
    interval: timedelta
    expires_at: datetime

    @classmethod
    def from_api(cls, raw: object, *, received_at: datetime) -> Self:
        """Parse a device_authorize response.

        The verification URL is always rebuilt from verification_uri with the
        user code and the client id, because tado's complete URL may lack the
        client id (#76).
        """
        reader = _Reader(raw, "device_authorize")
        user_code = reader.req_str("user_code")
        base = URL(reader.req_str("verification_uri"))
        if not base.absolute:
            raise TadoXParseError("device_authorize", "$.verification_uri")
        interval = reader.opt_seconds("interval") or DEVICE_FLOW_MIN_INTERVAL
        lifetime = reader.opt_seconds("expires_in")
        if lifetime is None:
            lifetime = DEFAULT_DEVICE_CODE_LIFETIME
        return cls(
            device_code=reader.req_str("device_code"),
            user_code=user_code,
            verification_url=str(
                base.update_query(user_code=user_code, client_id=CLIENT_ID)
            ),
            interval=max(interval, DEVICE_FLOW_MIN_INTERVAL),
            expires_at=received_at + lifetime,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimitInfo:
    """The account-wide quota from the ratelimit headers of one response."""

    limit: int | None
    window: timedelta | None
    remaining: int | None
    reset_in: timedelta | None
    observed_at: datetime

    @property
    def reset_at(self) -> datetime | None:
        """Return when the quota refills; known only when tado sent t=."""
        if self.reset_in is None:
            return None
        return self.observed_at + self.reset_in

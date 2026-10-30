"""Sensor platform of the tado X integration."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .client import (
    BatteryState,
    ControlMode,
    Device,
    HumidityLevel,
    NextScheduleChange,
    Power,
    RateLimitInfo,
    RoomComfort,
    RoomState,
    TemperatureLevel,
    TerminationType,
    WeatherState,
)
from .coordinator import (
    DevicesCoordinator,
    HomeCoordinator,
    RoomsCoordinator,
    TadoXCoordinator,
    home_today,
)
from .devices import device_identifier, home_identifier, room_identifier
from .entity import TadoXEntity, TadoXStatusEntity, async_add_dynamic_entities
from .quota import ApiStatus
from .runtime import HomeSnapshot, TadoXConfigEntry, TadoXRuntimeData

PARALLEL_UPDATES = 0

type SensorValue = StateType | datetime


def _state(value: StrEnum | None) -> str | None:
    """Return the ENUM state of an API value; None while it is unknown."""
    return None if value is None else value.value.lower()


def _options(enum: type[StrEnum]) -> list[str]:
    """Return the ENUM options of an API enum."""
    return [member.value.lower() for member in enum]


def _upcoming(moment: datetime | None) -> datetime | None:
    """Return a time that still lies ahead; tado keeps reporting past ones."""
    return moment if moment is not None and moment > dt_util.utcnow() else None


def _calls_used(info: RateLimitInfo | None) -> int | None:
    """Return the calls the whole account used since the quota reset (q - r)."""
    if info is None or info.limit is None or info.remaining is None:
        return None
    return max(info.limit - info.remaining, 0)


def _usage_percentage(info: RateLimitInfo | None) -> float | None:
    """Return the share of the account quota used since the reset."""
    used = _calls_used(info)
    if used is None or info is None or not info.limit:
        return None
    return round(min(used / info.limit, 1.0) * 100, 1)


def _next_change(room: RoomState) -> NextScheduleChange | None:
    """Return the next schedule change of a room unless it already passed."""
    change = room.next_schedule_change
    if change is None or _upcoming(change.start) is None:
        return None
    return change


def _next_temperature(room: RoomState) -> float | None:
    """Return the target of the next schedule change; None when it turns off."""
    change = _next_change(room)
    if change is None or change.setting.power is not Power.ON:
        return None
    return change.setting.temperature


def _manual_control_end(room: RoomState) -> datetime | None:
    """Return when the manual control of a room ends; None if it has no planned end."""
    override = room.manual_control
    if override is None or override.type is TerminationType.MANUAL:
        return None
    return _upcoming(override.projected_expiry)


@dataclass(frozen=True, kw_only=True)
class TadoXStatusSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor of the API quota or of the data freshness."""

    value_fn: Callable[[TadoXRuntimeData], SensorValue]


STATUS_SENSORS: tuple[TadoXStatusSensorEntityDescription, ...] = (
    TadoXStatusSensorEntityDescription(
        key="api_calls_today",
        translation_key="api_calls_today",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _calls_used(data.monitor.info),
    ),
    TadoXStatusSensorEntityDescription(
        key="api_calls_24h",
        translation_key="api_calls_24h",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.monitor.calls_24h(),
    ),
    TadoXStatusSensorEntityDescription(
        key="api_quota_remaining",
        translation_key="api_quota_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: info.remaining if (info := data.monitor.info) else None,
    ),
    TadoXStatusSensorEntityDescription(
        key="api_quota_limit",
        translation_key="api_quota_limit",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: info.limit if (info := data.monitor.info) else None,
    ),
    TadoXStatusSensorEntityDescription(
        key="api_usage_percentage",
        translation_key="api_usage_percentage",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _usage_percentage(data.monitor.info),
    ),
    TadoXStatusSensorEntityDescription(
        key="api_reset_time",
        translation_key="api_reset_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.monitor.reset_time,
    ),
    TadoXStatusSensorEntityDescription(
        key="api_status",
        translation_key="api_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in ApiStatus],
        value_fn=lambda data: data.monitor.status.value,
    ),
    TadoXStatusSensorEntityDescription(
        key="data_updated",
        translation_key="data_updated",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.rooms.last_live_update,
    ),
)


@dataclass(frozen=True, kw_only=True)
class TadoXHomeSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor of the home; feature names the endpoint it needs."""

    value_fn: Callable[[HomeSnapshot], StateType]
    feature: str | None = None


HOME_SENSORS: tuple[TadoXHomeSensorEntityDescription, ...] = (
    TadoXHomeSensorEntityDescription(
        key="presence_state",
        translation_key="presence_state",
        device_class=SensorDeviceClass.ENUM,
        options=["home", "away"],
        value_fn=lambda home: home.state.presence.value.lower(),
    ),
    TadoXHomeSensorEntityDescription(
        key="presence_mode",
        translation_key="presence_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.ENUM,
        options=["auto", "manual"],
        value_fn=lambda home: "manual" if home.state.presence_locked else "auto",
    ),
    TadoXHomeSensorEntityDescription(
        key="weather_outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        feature="weather",
        value_fn=lambda home: (
            home.weather.outdoor_temperature if home.weather else None
        ),
    ),
    TadoXHomeSensorEntityDescription(
        key="weather_solar_intensity",
        translation_key="solar_intensity",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        feature="weather",
        value_fn=lambda home: home.weather.solar_intensity if home.weather else None,
    ),
    TadoXHomeSensorEntityDescription(
        key="weather_weather_state",
        translation_key="weather_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(WeatherState),
        feature="weather",
        value_fn=lambda home: _state(home.weather.state) if home.weather else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class TadoXRoomSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor of the live state of a room."""

    value_fn: Callable[[RoomState], SensorValue]


ROOM_SENSORS: tuple[TadoXRoomSensorEntityDescription, ...] = (
    TadoXRoomSensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda room: room.current_temperature,
    ),
    TadoXRoomSensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda room: room.humidity,
    ),
    TadoXRoomSensorEntityDescription(
        key="heating_power",
        translation_key="heating_power",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda room: room.heating_power,
    ),
    TadoXRoomSensorEntityDescription(
        key="next_schedule_change",
        translation_key="next_schedule_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda room: change.start if (change := _next_change(room)) else None,
    ),
    TadoXRoomSensorEntityDescription(
        key="next_schedule_temperature",
        translation_key="next_schedule_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_next_temperature,
    ),
    TadoXRoomSensorEntityDescription(
        key="manual_control_end",
        translation_key="manual_control_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_manual_control_end,
    ),
    TadoXRoomSensorEntityDescription(
        key="next_time_block",
        translation_key="next_time_block",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda room: _upcoming(room.next_time_block),
    ),
)


@dataclass(frozen=True, kw_only=True)
class TadoXComfortSensorEntityDescription(SensorEntityDescription):
    """Describes an air comfort level of a room."""

    value_fn: Callable[[RoomComfort], str | None]


COMFORT_SENSORS: tuple[TadoXComfortSensorEntityDescription, ...] = (
    TadoXComfortSensorEntityDescription(
        key="air_freshness",
        translation_key="air_freshness",
        device_class=SensorDeviceClass.ENUM,
        options=_options(HumidityLevel),
        value_fn=lambda comfort: _state(comfort.humidity_level),
    ),
    TadoXComfortSensorEntityDescription(
        key="comfort_level",
        translation_key="comfort_level",
        device_class=SensorDeviceClass.ENUM,
        options=_options(TemperatureLevel),
        value_fn=lambda comfort: _state(comfort.temperature_level),
    ),
)

HEATING_TIME = SensorEntityDescription(
    key="heating_time_today",
    translation_key="heating_time_today",
    device_class=SensorDeviceClass.DURATION,
    state_class=SensorStateClass.TOTAL_INCREASING,
    native_unit_of_measurement=UnitOfTime.HOURS,
    suggested_display_precision=1,
)


@dataclass(frozen=True, kw_only=True)
class TadoXDeviceSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor of a physical device that reports the field it reads."""

    value_fn: Callable[[Device], StateType]
    exists_fn: Callable[[Device], bool]


DEVICE_SENSORS: tuple[TadoXDeviceSensorEntityDescription, ...] = (
    TadoXDeviceSensorEntityDescription(
        key="battery",
        translation_key="battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=_options(BatteryState),
        exists_fn=lambda device: device.has_battery,
        value_fn=lambda device: _state(device.battery),
    ),
    TadoXDeviceSensorEntityDescription(
        key="device_temperature",
        translation_key="device_temperature",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        exists_fn=lambda device: device.temperature_measured is not None,
        value_fn=lambda device: device.temperature_measured,
    ),
    TadoXDeviceSensorEntityDescription(
        key="temperature_offset",
        translation_key="temperature_offset",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        exists_fn=lambda device: device.temperature_offset is not None,
        value_fn=lambda device: device.temperature_offset,
    ),
    TadoXDeviceSensorEntityDescription(
        key="control_mode",
        translation_key="control_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=_options(ControlMode),
        exists_fn=lambda device: device.control_mode is not None,
        value_fn=lambda device: _state(device.control_mode),
    ),
)


class TadoXStatusSensor(TadoXStatusEntity, SensorEntity):
    """Sensor of the API quota or of the data freshness, fed by the quota monitor."""

    entity_description: TadoXStatusSensorEntityDescription

    def __init__(
        self, data: TadoXRuntimeData, description: TadoXStatusSensorEntityDescription
    ) -> None:
        """Initialize a sensor of the home device."""
        self.entity_description = description
        self._data = data
        home_id = data.api.home_id
        self._attr_unique_id = f"{home_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={home_identifier(home_id)})

    @property
    @override
    def native_value(self) -> SensorValue:
        """Return the value."""
        return self.entity_description.value_fn(self._data)


class TadoXHomeSensor(TadoXEntity[HomeCoordinator], SensorEntity):
    """Sensor of the presence or the weather of the home."""

    entity_description: TadoXHomeSensorEntityDescription

    def __init__(
        self,
        coordinator: HomeCoordinator,
        description: TadoXHomeSensorEntityDescription,
    ) -> None:
        """Initialize a sensor of the home device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            description,
            unique_id=f"{home_id}_{description.key}",
            device=home_identifier(home_id),
        )

    @property
    @override
    def native_value(self) -> StateType:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.data)


class TadoXRoomBoundSensor[C: TadoXCoordinator[Any]](TadoXEntity[C], SensorEntity):
    """Sensor of a room device, fed by any of the coordinators."""

    def __init__(
        self, coordinator: C, description: SensorEntityDescription, room_id: int
    ) -> None:
        """Initialize a sensor of a room device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            description,
            unique_id=f"{home_id}_{room_id}_{description.key}",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id


class TadoXRoomSensor(TadoXRoomBoundSensor[RoomsCoordinator]):
    """Sensor of the live state of a room."""

    entity_description: TadoXRoomSensorEntityDescription

    @override
    def _present(self) -> bool:
        return self._room_id in self.coordinator.data.rooms

    @property
    @override
    def native_value(self) -> SensorValue:
        """Return the value."""
        return self.entity_description.value_fn(
            self.coordinator.data.rooms[self._room_id]
        )


class TadoXComfortSensor(TadoXRoomBoundSensor[HomeCoordinator]):
    """Air comfort level of a room."""

    entity_description: TadoXComfortSensorEntityDescription

    @property
    @override
    def native_value(self) -> str | None:
        """Return the level; None while tado rates no level for the room."""
        comfort = self.coordinator.data.air_comfort
        room = None if comfort is None else comfort.rooms.get(self._room_id)
        return None if room is None else self.entity_description.value_fn(room)


class TadoXHeatingTimeSensor(TadoXRoomBoundSensor[DevicesCoordinator]):
    """Heating time of a room today, in the home's time zone."""

    @override
    def _present(self) -> bool:
        return self._room_id in self.coordinator.data.topology.rooms

    @property
    @override
    def native_value(self) -> float | None:
        """Return the hours; running times of an earlier day count as none today."""
        data = self.coordinator.data
        if (times := data.running_times) is None:
            return None
        if times.day != home_today(data.home_info):
            return 0.0
        if (spent := times.by_room.get(self._room_id)) is None:
            return None
        return round(spent.total_seconds() / 3600, 2)


class TadoXDeviceSensor(TadoXEntity[DevicesCoordinator], SensorEntity):
    """Sensor of a physical device."""

    entity_description: TadoXDeviceSensorEntityDescription

    def __init__(
        self,
        coordinator: DevicesCoordinator,
        description: TadoXDeviceSensorEntityDescription,
        serial: str,
    ) -> None:
        """Initialize a sensor of a physical device."""
        super().__init__(
            coordinator,
            description,
            unique_id=f"{serial}_{description.key}",
            device=device_identifier(serial),
        )
        self._serial = serial

    @override
    def _present(self) -> bool:
        return self._serial in self.coordinator.data.topology.devices

    @property
    @override
    def native_value(self) -> StateType:
        """Return the value."""
        return self.entity_description.value_fn(
            self.coordinator.data.topology.devices[self._serial]
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of a tado X home."""
    data = entry.runtime_data
    monitor = data.monitor
    async_add_entities(
        TadoXStatusSensor(data, description) for description in STATUS_SENSORS
    )
    async_add_entities(
        TadoXHomeSensor(data.home, description)
        for description in HOME_SENSORS
        if description.feature is None or monitor.enabled(description.feature)
    )
    air_comfort = monitor.enabled("air_comfort")
    running_times = monitor.enabled("running_times")

    def build() -> Iterator[TadoXEntity[Any]]:
        topology = data.devices.data.topology
        for room_id in topology.rooms:
            for description in ROOM_SENSORS:
                yield TadoXRoomSensor(data.rooms, description, room_id)
            if air_comfort:
                for comfort in COMFORT_SENSORS:
                    yield TadoXComfortSensor(data.home, comfort, room_id)
            if running_times:
                yield TadoXHeatingTimeSensor(data.devices, HEATING_TIME, room_id)
        for device in topology.devices.values():
            for sensor in DEVICE_SENSORS:
                if sensor.exists_fn(device):
                    yield TadoXDeviceSensor(data.devices, sensor, device.serial)

    async_add_dynamic_entities(entry, data.devices, async_add_entities, build)

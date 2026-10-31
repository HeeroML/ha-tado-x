"""Number platform: offsets, termination timers and flow temperature."""

from collections.abc import Iterator
from datetime import timedelta
from typing import override

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import Device, FlowTemperature, TadoXError, Termination, TerminationType
from .const import (
    MAX_TERMINATION_TIMER_MINUTES,
    MIN_TERMINATION_TIMER_MINUTES,
    OFFSET_MAX,
    OFFSET_MIN,
    OFFSET_STEP,
)
from .coordinator import DevicesCoordinator
from .devices import device_identifier, home_identifier, room_identifier
from .entity import (
    TadoXEntity,
    async_add_dynamic_entities,
    handle_tado_errors,
    raise_for_tado_error,
)
from .runtime import TadoXConfigEntry
from .select import async_set_room_default

PARALLEL_UPDATES = 1

OFFSET_DESCRIPTION = NumberEntityDescription(
    key="offset",
    translation_key="temperature_offset",
    device_class=NumberDeviceClass.TEMPERATURE_DELTA,
    entity_category=EntityCategory.CONFIG,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_min_value=OFFSET_MIN,
    native_max_value=OFFSET_MAX,
    native_step=OFFSET_STEP,
    mode=NumberMode.BOX,
)
TIMER_DESCRIPTION = NumberEntityDescription(
    key="manual_termination_timer",
    translation_key="manual_termination_timer",
    device_class=NumberDeviceClass.DURATION,
    entity_category=EntityCategory.CONFIG,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    native_min_value=MIN_TERMINATION_TIMER_MINUTES,
    native_max_value=MAX_TERMINATION_TIMER_MINUTES,
    native_step=1,
    mode=NumberMode.BOX,
)
FLOW_DESCRIPTION = NumberEntityDescription(
    key="max_flow_temperature",
    translation_key="max_flow_temperature",
    device_class=NumberDeviceClass.TEMPERATURE,
    entity_category=EntityCategory.CONFIG,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_step=1,
    mode=NumberMode.BOX,
)


async def async_set_offset(entry: TadoXConfigEntry, serial: str, offset: float) -> None:
    """Write the temperature offset of a device and show it without a read (#51)."""
    data = entry.runtime_data
    offset = round(offset, 1)
    try:
        await data.api.async_update_device(serial, temperature_offset=offset)
    except TadoXError as err:
        raise_for_tado_error(err, entry, context="command temperature_offset")
    data.devices.async_apply_optimistic(
        {
            ("temperature_offset", serial): lambda snapshot: snapshot.with_device(
                serial, temperature_offset=offset
            )
        }
    )


def _entities(
    coordinator: DevicesCoordinator,
) -> Iterator[TadoXEntity[DevicesCoordinator]]:
    snapshot = coordinator.data
    for serial, device in snapshot.topology.devices.items():
        if device.temperature_offset is not None:
            yield TadoXOffsetNumber(coordinator, serial)
    for room_id in snapshot.topology.rooms:
        yield TadoXTerminationTimerNumber(coordinator, room_id)
    if snapshot.flow_temperature is not None:
        yield TadoXMaxFlowTemperatureNumber(coordinator)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number entities of a tado X home."""
    devices = entry.runtime_data.devices
    async_add_dynamic_entities(
        entry, devices, async_add_entities, lambda: list(_entities(devices))
    )


class TadoXOffsetNumber(TadoXEntity[DevicesCoordinator], NumberEntity):
    """Temperature offset of a valve, sensor or thermostat."""

    def __init__(self, coordinator: DevicesCoordinator, serial: str) -> None:
        """Initialize the number of a device."""
        super().__init__(
            coordinator,
            OFFSET_DESCRIPTION,
            unique_id=f"{serial}_offset",
            device=device_identifier(serial),
        )
        self._serial = serial

    @property
    def _device(self) -> Device | None:
        return self.coordinator.data.topology.devices.get(self._serial)

    @override
    def _present(self) -> bool:
        return (device := self._device) is not None and (
            device.temperature_offset is not None
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the offset in °C."""
        return None if (device := self._device) is None else device.temperature_offset

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the offset."""
        await async_set_offset(self.coordinator.config_entry, self._serial, value)


class TadoXTerminationTimerNumber(TadoXEntity[DevicesCoordinator], NumberEntity):
    """Timer of the app's manual control default; only while that default is a timer."""

    def __init__(self, coordinator: DevicesCoordinator, room_id: int) -> None:
        """Initialize the number of a room."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            TIMER_DESCRIPTION,
            unique_id=f"{home_id}_{room_id}_manual_termination_timer",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id

    @property
    def _timer(self) -> timedelta | None:
        room = self.coordinator.data.topology.rooms.get(self._room_id)
        default = None if room is None else room.manual_termination_default
        if default is None or default.type is not TerminationType.TIMER:
            return None
        return default.duration

    @override
    def _present(self) -> bool:
        return self._timer is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the timer in minutes, as tado stores it."""
        timer = self._timer
        return None if timer is None else timer.total_seconds() / 60

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write a timer default with the new duration."""
        await async_set_room_default(
            self.coordinator.config_entry,
            self._room_id,
            Termination.timer(timedelta(minutes=value)),
        )


class TadoXMaxFlowTemperatureNumber(TadoXEntity[DevicesCoordinator], NumberEntity):
    """Maximum flow temperature of an OpenTherm boiler."""

    def __init__(self, coordinator: DevicesCoordinator) -> None:
        """Initialize the number of the home."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            FLOW_DESCRIPTION,
            unique_id=f"{home_id}_max_flow_temperature",
            device=home_identifier(home_id),
        )

    @property
    def _flow(self) -> FlowTemperature | None:
        return self.coordinator.data.flow_temperature

    @override
    def _present(self) -> bool:
        return self._flow is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the maximum flow temperature."""
        flow = self._flow
        return None if flow is None else flow.max_flow_temperature

    @property
    @override
    def native_min_value(self) -> float:
        """Return the lowest maximum tado accepts."""
        flow = self._flow
        return super().native_min_value if flow is None else flow.minimum

    @property
    @override
    def native_max_value(self) -> float:
        """Return the highest maximum tado accepts."""
        flow = self._flow
        return super().native_max_value if flow is None else flow.maximum

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the maximum flow temperature."""
        await self._async_set(round(value))

    @handle_tado_errors
    async def _async_set(self, maximum: int) -> None:
        await self.coordinator.api.async_set_flow_temperature(maximum=maximum)
        self.coordinator.async_apply_optimistic(
            {
                "max_flow_temperature": lambda snapshot: snapshot.with_flow_temperature(
                    max_flow_temperature=maximum
                )
            }
        )

"""Climate platform: one entity per tado X room."""

from collections.abc import Callable, Mapping
from dataclasses import asdict
import datetime as dt
from typing import Any, Final, override

from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    PRESET_AWAY,
    PRESET_HOME,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .client import (
    ActiveOverride,
    ConnectionState,
    DayType,
    Power,
    PresenceSetting,
    RoomState,
    Setting,
    TadoXError,
    TadoXForbiddenError,
    Termination,
    TerminationType,
    summarize_day_report,
)
from .const import (
    DEFAULT_OPTIONS,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    MAX_TEMP,
    MIN_TEMP,
    OPT_MANUAL_TERMINATION,
    OPT_OFF_FOLLOWS_POLICY,
    OPT_TIMER_MINUTES,
    TEMP_STEP,
)
from .coordinator import RoomsCoordinator, home_today
from .devices import room_identifier
from .entity import (
    TadoXEntity,
    async_add_dynamic_entities,
    handle_tado_errors,
    raise_for_tado_error,
)
from .runtime import RoomsSnapshot, TadoXConfigEntry
from .select import async_set_presence
from .services import schedule_blocks, schedule_response, termination_override
from .termination import TerminationPolicy, resolve_termination

PARALLEL_UPDATES = 1

PRESET_AUTO: Final = "auto"
PRESETS: Final[Mapping[str, PresenceSetting]] = {
    PRESET_HOME: PresenceSetting.HOME,
    PRESET_AWAY: PresenceSetting.AWAY,
    PRESET_AUTO: PresenceSetting.AUTO,
}
ATTR_MANUAL_CONTROL_TYPE: Final = "manual_control_type"
DESCRIPTION = ClimateEntityDescription(key="climate", translation_key=DOMAIN)

type RoomPatch = Callable[[RoomsSnapshot], RoomsSnapshot]


def control_target(room_id: int) -> tuple[str, int]:
    """Return the optimistic patch target of the setting and manual control of a room."""
    return ("control", room_id)


def _manual_patch(
    room_id: int, setting: Setting, termination: Termination
) -> RoomPatch:
    """Return a patch that shows a manual control until the server reports it."""
    now = dt_util.utcnow()
    duration = termination.duration

    def patch(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        room = snapshot.rooms.get(room_id)
        if room is None or (
            room.setting == setting
            and room.manual_control is not None
            and room.manual_control.type is termination.type
        ):
            return snapshot
        expiry = room.next_time_block
        if duration is not None:
            expiry = now + duration
        elif termination.type is TerminationType.MANUAL:
            expiry = None
        override = ActiveOverride(
            type=termination.type, remaining=duration, projected_expiry=expiry
        )
        return snapshot.with_room(room_id, setting=setting, manual_control=override)

    return patch


def resume_patch(room_id: int) -> RoomPatch:
    """Return a patch that shows the schedule until the server reports it."""

    def patch(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        room = snapshot.rooms.get(room_id)
        if room is None or room.manual_control is None:
            return snapshot
        return snapshot.with_room(room_id, manual_control=None)

    return patch


def _hvac_mode(room: RoomState) -> HVACMode:
    if room.manual_control is None:
        return HVACMode.AUTO
    return HVACMode.OFF if room.setting.power is Power.OFF else HVACMode.HEAT


def _hvac_action(room: RoomState) -> HVACAction:
    if room.setting.power is Power.OFF:
        return HVACAction.OFF
    return HVACAction.HEATING if room.heating_power else HVACAction.IDLE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room climate entities of a tado X home."""
    data = entry.runtime_data
    # Rooms follow the topology, which also registers their devices.
    async_add_dynamic_entities(
        entry,
        data.devices,
        async_add_entities,
        lambda: [
            TadoXClimate(data.rooms, room_id)
            for room_id in data.devices.data.topology.rooms
        ],
    )


class TadoXClimate(TadoXEntity[RoomsCoordinator], ClimateEntity):
    """Climate entity of one room; presets set the home-wide presence."""

    _attr_name = None
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = list(PRESETS)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, coordinator: RoomsCoordinator, room_id: int) -> None:
        """Initialize the climate entity of a room."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            DESCRIPTION,
            unique_id=f"{home_id}_{room_id}_climate",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id
        self._last_target: float | None = None
        self._remember_target()

    @property
    def _room(self) -> RoomState | None:
        return self.coordinator.data.rooms.get(self._room_id)

    @property
    def _entry(self) -> TadoXConfigEntry:
        return self.coordinator.config_entry

    @override
    def _present(self) -> bool:
        room = self._room
        return room is not None and room.connection is not ConnectionState.DISCONNECTED

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the presence of the home too, which drives the preset."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._entry.runtime_data.home.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._remember_target()
        super()._handle_coordinator_update()

    def _remember_target(self) -> None:
        room = self._room
        if room is not None and room.setting.temperature is not None:
            self._last_target = room.setting.temperature

    @property
    @override
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        return None if (room := self._room) is None else room.current_temperature

    @property
    @override
    def current_humidity(self) -> float | None:
        """Return the room humidity."""
        return None if (room := self._room) is None else room.humidity

    @property
    @override
    def target_temperature(self) -> float | None:
        """Return the target; None while the room is off."""
        return None if (room := self._room) is None else room.setting.temperature

    @property
    @override
    def hvac_mode(self) -> HVACMode | None:
        """Return OFF or HEAT under manual control, else AUTO for the schedule."""
        return None if (room := self._room) is None else _hvac_mode(room)

    @property
    @override
    def hvac_action(self) -> HVACAction | None:
        """Return whether the room is off, heating or idle."""
        return None if (room := self._room) is None else _hvac_action(room)

    @property
    @override
    def preset_mode(self) -> str | None:
        """Return the presence of the home."""
        return self._entry.runtime_data.home.data.state.setting.lower()

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how the manual control of the room ends."""
        room = self._room
        manual = None if room is None else room.manual_control
        kind = None if manual is None else manual.type
        return {ATTR_MANUAL_CONTROL_TYPE: None if kind is None else kind.lower()}

    def _termination(
        self, override: Termination | None = None, *, off: bool = False
    ) -> Termination:
        """Return the termination of a manual change per the options (#94)."""
        options = {**DEFAULT_OPTIONS, **self._entry.options}
        if off and not options[OPT_OFF_FOLLOWS_POLICY]:
            return Termination.manual()
        topology = self._entry.runtime_data.devices.data.topology
        room = topology.rooms.get(self._room_id)
        return resolve_termination(
            policy=TerminationPolicy(options[OPT_MANUAL_TERMINATION]),
            timer=dt.timedelta(minutes=int(options[OPT_TIMER_MINUTES])),
            room_default=None if room is None else room.manual_termination_default,
            override=override,
        )

    def _heat_temperature(self) -> float:
        """Return the target for turning on: current, last seen, next schedule, 21 °C."""
        room = self._room
        if room is not None and room.setting.temperature is not None:
            return room.setting.temperature
        if self._last_target is not None:
            return self._last_target
        change = None if room is None else room.next_schedule_change
        if change is not None and change.setting.temperature is not None:
            return change.setting.temperature
        return DEFAULT_TARGET_TEMP

    async def _async_set_manual(
        self, setting: Setting, termination: Termination
    ) -> None:
        await self.coordinator.api.async_set_manual_control(
            self._room_id, setting, termination
        )
        self.coordinator.async_apply_optimistic(
            {
                control_target(self._room_id): _manual_patch(
                    self._room_id, setting, termination
                )
            }
        )
        await self.coordinator.async_request_refresh()

    @handle_tado_errors
    async def _async_heat(
        self, temperature: float, override: Termination | None = None
    ) -> None:
        await self._async_set_manual(
            Setting(power=Power.ON, temperature=temperature),
            self._termination(override),
        )

    @handle_tado_errors
    async def _async_off(self) -> None:
        await self._async_set_manual(
            Setting(power=Power.OFF), self._termination(off=True)
        )

    @handle_tado_errors
    async def _async_resume(self) -> None:
        await self.coordinator.api.async_resume_schedule(self._room_id)
        self.coordinator.async_apply_optimistic(
            {control_target(self._room_id): resume_patch(self._room_id)}
        )
        await self.coordinator.async_request_refresh()

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a manual target; it ends as the termination options say."""
        if kwargs.get(ATTR_HVAC_MODE) == HVACMode.OFF:
            await self._async_off()
            return
        await self._async_heat(float(kwargs[ATTR_TEMPERATURE]))

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn the room off, heat it manually, or resume the schedule."""
        if hvac_mode == HVACMode.OFF:
            await self._async_off()
        elif hvac_mode == HVACMode.HEAT:
            await self._async_heat(self._heat_temperature())
        else:
            await self._async_resume()

    @override
    async def async_turn_on(self) -> None:
        """Heat the room manually."""
        await self._async_heat(self._heat_temperature())

    @override
    async def async_turn_off(self) -> None:
        """Turn the room off."""
        await self._async_off()

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the presence of the whole home."""
        await async_set_presence(self._entry, PRESETS[preset_mode])

    async def async_set_climate_timer(
        self,
        temperature: float,
        duration: int | None = None,
        termination: str | None = None,
    ) -> None:
        """Set a temperature; duration in minutes means a timer (v1 compatible)."""
        await self._async_heat(temperature, termination_override(duration, termination))

    async def async_get_schedule(self) -> ServiceResponse:
        """Return the weekly schedule of the room."""
        try:
            schedule = await self.coordinator.api.async_get_schedule(self._room_id)
        except TadoXError as err:
            raise_for_tado_error(err, self._entry, context="service get_schedule")
        return schedule_response(schedule)

    async def async_set_schedule(
        self, days: list[str], blocks: list[dict[str, Any]]
    ) -> None:
        """Replace the schedule of the given weekdays with the same blocks.

        Every day is validated before the first request; tado takes one
        request per day.
        """
        chosen = {DayType(day.upper()) for day in days}
        plan = {day: schedule_blocks(day, blocks) for day in DayType if day in chosen}
        done: list[str] = []
        for day, day_blocks in plan.items():
            try:
                await self.coordinator.api.async_set_day_schedule(
                    self._room_id, day, day_blocks
                )
            except TadoXError as err:
                if not done:
                    raise_for_tado_error(
                        err, self._entry, context="service set_schedule"
                    )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_partially_written",
                    translation_placeholders={"done_days": ", ".join(done)},
                ) from err
            done.append(day.lower())
        await self.coordinator.async_request_refresh()

    async def async_get_day_report(
        self, date: dt.date | None = None
    ) -> ServiceResponse:
        """Return the summary and the raw day report; today in the home's zone by default."""
        devices = self._entry.runtime_data.devices.data
        day = date or home_today(devices.home_info)
        try:
            report = await self.coordinator.api.async_get_day_report(self._room_id, day)
        except TadoXForbiddenError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="day_report_forbidden"
            ) from err
        except TadoXError as err:
            raise_for_tado_error(err, self._entry, context="service get_day_report")
        return {
            "summary": asdict(summarize_day_report(report)),
            "report": dict(report.raw),
        }

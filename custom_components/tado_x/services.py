"""Services of the tado X integration, registered once per Home Assistant start.

Room services are platform entity services on climate; home services resolve
their config entry per call, so they always reach the entry's current client.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
import datetime as dt
from typing import Any, Final, NoReturn

import probatio

from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DATE,
    ATTR_DEVICE_ID,
    ATTR_TEMPERATURE,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import VolDictType
from homeassistant.util import dt as dt_util

from .client import (
    DayType,
    Power,
    RoomSchedule,
    ScheduleBlock,
    Setting,
    TadoXError,
    TadoXForbiddenError,
    TadoXNotFoundError,
    TariffUnit,
    Termination,
    TerminationType,
    format_clock,
    parse_clock,
    tariff_cents,
    validate_day,
)
from .const import (
    DOMAIN,
    MAX_TEMP,
    MAX_TERMINATION_TIMER_MINUTES,
    MIN_TEMP,
    MIN_TERMINATION_TIMER_MINUTES,
    OFFSET_MAX,
    OFFSET_MIN,
)
from .coordinator import home_today
from .entity import raise_for_tado_error
from .number import async_set_offset
from .runtime import DevicesSnapshot, TadoXConfigEntry

SERVICE_SET_CLIMATE_TIMER: Final = "set_climate_timer"
SERVICE_GET_SCHEDULE: Final = "get_schedule"
SERVICE_SET_SCHEDULE: Final = "set_schedule"
SERVICE_GET_DAY_REPORT: Final = "get_day_report"
SERVICE_SET_TEMPERATURE_OFFSET: Final = "set_temperature_offset"
SERVICE_ADD_METER_READING: Final = "add_meter_reading"
SERVICE_SET_EIQ_TARIFF: Final = "set_eiq_tariff"
SERVICE_GET_HOLIDAY_MODE: Final = "get_holiday_mode"
SERVICE_SET_HOLIDAY_MODE: Final = "set_holiday_mode"

ATTR_DURATION: Final = "duration"
ATTR_TERMINATION: Final = "termination"
ATTR_DAYS: Final = "days"
ATTR_BLOCKS: Final = "blocks"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_POWER: Final = "power"
ATTR_OFFSET: Final = "offset"
ATTR_READING: Final = "reading"
ATTR_TARIFF: Final = "tariff"
ATTR_UNIT: Final = "unit"
ATTR_START_DATE: Final = "start_date"
ATTR_END_DATE: Final = "end_date"
ATTR_ENABLED: Final = "enabled"
ATTR_ACTIVE: Final = "active"

TARIFF_UNITS: Final = {"m3": TariffUnit.M3, "kwh": TariffUnit.KWH}
ENERGY_IQ: Final = "Energy IQ"


TEMPERATURE: Final = probatio.All(
    probatio.Coerce(float), probatio.Range(min=MIN_TEMP, max=MAX_TEMP)
)
BLOCK_SCHEMA: Final = probatio.Schema(
    {
        probatio.Required(ATTR_START): cv.string,
        probatio.Required(ATTR_END): cv.string,
        probatio.Optional(ATTR_POWER, default="on"): probatio.In(["on", "off"]),
        probatio.Optional(ATTR_TEMPERATURE): TEMPERATURE,
    }
)
SET_CLIMATE_TIMER_SCHEMA: Final[VolDictType] = {
    probatio.Required(ATTR_TEMPERATURE): TEMPERATURE,
    probatio.Optional(ATTR_DURATION): probatio.All(
        probatio.Coerce(int),
        probatio.Range(
            min=MIN_TERMINATION_TIMER_MINUTES, max=MAX_TERMINATION_TIMER_MINUTES
        ),
    ),
    probatio.Optional(ATTR_TERMINATION): probatio.In(
        [kind.lower() for kind in TerminationType]
    ),
}
SET_SCHEDULE_SCHEMA: Final[VolDictType] = {
    probatio.Required(ATTR_DAYS): probatio.All(
        probatio.EnsureList(),
        probatio.Length(min=1),
        [probatio.In([day.lower() for day in DayType])],
    ),
    probatio.Required(ATTR_BLOCKS): probatio.All(
        probatio.EnsureList(), probatio.Length(min=1), [BLOCK_SCHEMA]
    ),
}
GET_DAY_REPORT_SCHEMA: Final[VolDictType] = {probatio.Optional(ATTR_DATE): cv.date}
SET_TEMPERATURE_OFFSET_SCHEMA: Final = probatio.Schema(
    {
        probatio.Required(ATTR_DEVICE_ID): str,
        probatio.Required(ATTR_OFFSET): probatio.All(
            probatio.Coerce(float), probatio.Range(min=OFFSET_MIN, max=OFFSET_MAX)
        ),
    }
)
HOME_SCHEMA: Final = probatio.Schema({probatio.Optional(ATTR_CONFIG_ENTRY_ID): str})
ADD_METER_READING_SCHEMA: Final = HOME_SCHEMA.extend(
    {
        probatio.Required(ATTR_READING): probatio.All(
            probatio.Coerce(int), probatio.Range(min=0)
        ),
        probatio.Optional(ATTR_DATE): cv.date,
    }
)
SET_EIQ_TARIFF_SCHEMA: Final = HOME_SCHEMA.extend(
    {
        probatio.Required(ATTR_TARIFF): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0)
        ),
        probatio.Required(ATTR_UNIT): probatio.All(
            str, probatio.Lower, probatio.In(TARIFF_UNITS)
        ),
        probatio.Required(ATTR_START_DATE): cv.date,
        probatio.Optional(ATTR_END_DATE): cv.date,
    }
)
SET_HOLIDAY_MODE_SCHEMA: Final = HOME_SCHEMA.extend(
    {
        probatio.Required(ATTR_ENABLED): cv.boolean,
        probatio.Optional(ATTR_START): cv.datetime,
        probatio.Optional(ATTR_END): cv.datetime,
    }
)


def termination_override(
    duration: int | None, termination: str | None
) -> Termination | None:
    """Return the termination of a set_climate_timer call; None means the policy."""
    if termination is None:
        if duration is None:
            return None
        return Termination.timer(dt.timedelta(minutes=duration))
    kind = TerminationType(termination.upper())
    if kind is TerminationType.TIMER:
        if duration is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="timer_requires_duration"
            )
        return Termination.timer(dt.timedelta(minutes=duration))
    if duration is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="duration_requires_timer"
        )
    return Termination(type=kind)


def _minute(value: str) -> int:
    if (minute := parse_clock(value)) is None:
        raise ValueError(f"{value} is not a time between 00:00 and 24:00")
    return minute


def schedule_blocks(
    day: DayType, blocks: Sequence[Mapping[str, Any]]
) -> tuple[ScheduleBlock, ...]:
    """Return the validated blocks of one weekday, or raise ServiceValidationError.

    Times arrive as HH:MM text, so a bad time gets the same translated error
    as a gap in the day.
    """
    try:
        return validate_day(
            [
                ScheduleBlock(
                    day=day,
                    start_minute=_minute(block[ATTR_START]),
                    end_minute=_minute(block[ATTR_END]),
                    setting=Setting(
                        power=Power(str(block.get(ATTR_POWER, "on")).upper()),
                        temperature=block.get(ATTR_TEMPERATURE),
                    ),
                )
                for block in blocks
            ]
        )
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_schedule",
            translation_placeholders={"reason": str(err)},
        ) from err


def _block_response(block: ScheduleBlock) -> dict[str, Any]:
    response: dict[str, Any] = {
        ATTR_START: format_clock(block.start_minute),
        ATTR_END: format_clock(block.end_minute),
        ATTR_POWER: block.setting.power.lower(),
    }
    if block.setting.temperature is not None:
        response[ATTR_TEMPERATURE] = block.setting.temperature
    return response


def schedule_response(schedule: RoomSchedule) -> dict[str, Any]:
    """Return a weekly schedule as get_schedule answers it, one list per weekday."""
    return {
        "days": {
            day.lower(): [_block_response(block) for block in schedule.day(day)]
            for day in DayType
        }
    }


def _entry(call: ServiceCall) -> TadoXConfigEntry:
    """Return the loaded entry named by the call, or the only loaded one."""
    entry: TadoXConfigEntry = service.async_get_config_entry(
        call.hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)
    )
    return entry


def _energy_iq_entry(call: ServiceCall) -> TadoXConfigEntry:
    """Return the entry of an Energy IQ call; a home without Energy IQ is refused."""
    entry = _entry(call)
    info = entry.runtime_data.devices.data.home_info
    if info is not None and info.energy_iq_eligible is False:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="feature_unavailable",
            translation_placeholders={"feature": ENERGY_IQ},
        )
    return entry


def _offset_serial(entry: TadoXConfigEntry, device: DeviceEntry) -> str | None:
    """Return the serial of a device tado reports with a temperature offset."""
    devices = entry.runtime_data.devices.data.topology.devices
    for domain, serial in device.identifiers:
        if (
            domain == DOMAIN
            and (reported := devices.get(serial)) is not None
            and reported.temperature_offset is not None
        ):
            return serial
    return None


async def _async_set_temperature_offset(call: ServiceCall) -> None:
    """Write the offset of a valve, sensor or thermostat (#51)."""
    device, config_entry = service.async_get_device_and_config_entry(
        call.hass, DOMAIN, call.data[ATTR_DEVICE_ID], include_child_devices=False
    )
    entry: TadoXConfigEntry = config_entry
    if (serial := _offset_serial(entry, device)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_offset_capable",
            translation_placeholders={
                "device": device.name_by_user or device.name or device.id
            },
        )
    await async_set_offset(entry, serial, call.data[ATTR_OFFSET])


async def _async_add_meter_reading(call: ServiceCall) -> None:
    """Add an Energy IQ meter reading; the date defaults to today at the home."""
    entry = _energy_iq_entry(call)
    data = entry.runtime_data
    day = call.data.get(ATTR_DATE) or home_today(data.devices.data.home_info)
    try:
        await data.api.async_add_meter_reading(call.data[ATTR_READING], day)
    except TadoXError as err:
        raise_for_tado_error(err, entry)


async def _async_set_eiq_tariff(call: ServiceCall) -> None:
    """Add an Energy IQ tariff in whole cents, rounded half up."""
    entry = _energy_iq_entry(call)
    try:
        await entry.runtime_data.api.async_add_tariff(
            cents=tariff_cents(call.data[ATTR_TARIFF]),
            unit=TARIFF_UNITS[call.data[ATTR_UNIT]],
            start=call.data[ATTR_START_DATE],
            end=call.data.get(ATTR_END_DATE),
        )
    except TadoXError as err:
        raise_for_tado_error(err, entry)


def _raise_holiday_error(err: TadoXError, entry: TadoXConfigEntry) -> NoReturn:
    """Raise the translated error of a holiday mode request."""
    if isinstance(err, TadoXForbiddenError | TadoXNotFoundError):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="holiday_mode_unavailable"
        ) from err
    raise_for_tado_error(err, entry)


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.isoformat()


async def _async_get_holiday_mode(call: ServiceCall) -> ServiceResponse:
    """Return the holiday mode of a home."""
    entry = _entry(call)
    try:
        holiday = await entry.runtime_data.api.async_read_holiday_mode()
    except TadoXError as err:
        _raise_holiday_error(err, entry)
    return {
        ATTR_ENABLED: holiday.enabled,
        ATTR_ACTIVE: holiday.active,
        ATTR_START: _iso(holiday.start),
        ATTR_END: _iso(holiday.end),
    }


def _holiday_patch(
    enabled: bool, start: dt.datetime | None, end: dt.datetime | None
) -> Callable[[DevicesSnapshot], DevicesSnapshot]:
    """Return a patch that shows the written holiday mode until tado reports it."""

    def patch(snapshot: DevicesSnapshot) -> DevicesSnapshot:
        if (current := snapshot.holiday_mode) is None:
            return snapshot
        changed = replace(
            current,
            enabled=enabled,
            start=start or current.start,
            end=end or current.end,
        )
        return (
            snapshot if changed == current else replace(snapshot, holiday_mode=changed)
        )

    return patch


async def _async_set_holiday_mode(call: ServiceCall) -> None:
    """Turn holiday mode on or off; tado keeps the settings it is not given."""
    entry = _entry(call)
    enabled: bool = call.data[ATTR_ENABLED]
    start: dt.datetime | None = call.data.get(ATTR_START)
    end: dt.datetime | None = call.data.get(ATTR_END)
    start = None if start is None else dt_util.as_utc(start)
    end = None if end is None else dt_util.as_utc(end)
    data = entry.runtime_data
    try:
        await data.api.async_set_holiday_mode(enabled=enabled, start=start, end=end)
    except TadoXError as err:
        _raise_holiday_error(err, entry)
    data.devices.async_apply_optimistic(_holiday_patch(enabled, start, end))


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register every tado X service."""
    for name, method, entity_schema, response in (
        (
            SERVICE_SET_CLIMATE_TIMER,
            "async_set_climate_timer",
            SET_CLIMATE_TIMER_SCHEMA,
            SupportsResponse.NONE,
        ),
        (SERVICE_GET_SCHEDULE, "async_get_schedule", None, SupportsResponse.ONLY),
        (
            SERVICE_SET_SCHEDULE,
            "async_set_schedule",
            SET_SCHEDULE_SCHEMA,
            SupportsResponse.NONE,
        ),
        (
            SERVICE_GET_DAY_REPORT,
            "async_get_day_report",
            GET_DAY_REPORT_SCHEMA,
            SupportsResponse.ONLY,
        ),
    ):
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=CLIMATE_DOMAIN,
            func=method,
            schema=entity_schema,
            supports_response=response,
        )
    for name, handler, schema, response in (
        (
            SERVICE_SET_TEMPERATURE_OFFSET,
            _async_set_temperature_offset,
            SET_TEMPERATURE_OFFSET_SCHEMA,
            SupportsResponse.NONE,
        ),
        (
            SERVICE_ADD_METER_READING,
            _async_add_meter_reading,
            ADD_METER_READING_SCHEMA,
            SupportsResponse.NONE,
        ),
        (
            SERVICE_SET_EIQ_TARIFF,
            _async_set_eiq_tariff,
            SET_EIQ_TARIFF_SCHEMA,
            SupportsResponse.NONE,
        ),
        (
            SERVICE_GET_HOLIDAY_MODE,
            _async_get_holiday_mode,
            HOME_SCHEMA,
            SupportsResponse.ONLY,
        ),
        (
            SERVICE_SET_HOLIDAY_MODE,
            _async_set_holiday_mode,
            SET_HOLIDAY_MODE_SCHEMA,
            SupportsResponse.NONE,
        ),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=response
        )

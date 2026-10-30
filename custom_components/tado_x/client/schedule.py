"""Schedule validation and day report summaries."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from .enums import Power
from .models import DayReport, ScheduleBlock, format_clock

_DAY_END: Final = 1440
_MIN_TEMPERATURE: Final = 5.0
_MAX_TEMPERATURE: Final = 30.0
_NO_HEAT_CALL: Final = "NONE"


@dataclass(frozen=True, slots=True, kw_only=True)
class DaySummary:
    """Summary of one room day report."""

    temperature_avg: float | None
    temperature_min: float | None
    temperature_max: float | None
    humidity_avg_percent: float | None
    heating_hours: float | None


def _check_setting(block: ScheduleBlock) -> None:
    at = format_clock(block.start_minute)
    temperature = block.setting.temperature
    if block.setting.power is Power.OFF:
        if temperature is not None:
            raise ValueError(f"the OFF block at {at} must not have a temperature")
        return
    if temperature is None or not _MIN_TEMPERATURE <= temperature <= _MAX_TEMPERATURE:
        raise ValueError(f"the block at {at} needs a temperature between 5 and 30 °C")


def validate_day(blocks: Sequence[ScheduleBlock]) -> tuple[ScheduleBlock, ...]:
    """Return the blocks of one weekday in order, or raise ValueError(reason).

    The day must start at 00:00, be contiguous and end at 24:00; every block
    must end after it starts; ON blocks need 5-30 °C and OFF blocks no
    temperature.
    """
    if not blocks:
        raise ValueError("the day has no blocks")
    if len({block.day for block in blocks}) > 1:
        raise ValueError("all blocks must be on the same weekday")
    ordered = tuple(sorted(blocks, key=lambda block: block.start_minute))
    end = 0
    for block in ordered:
        if block.start_minute != end:
            raise ValueError(
                "the day must start at 00:00"
                if end == 0
                else f"the block at {format_clock(block.start_minute)} must start "
                f"at {format_clock(end)}, where the previous block ends"
            )
        if block.end_minute <= block.start_minute:
            raise ValueError(
                f"the block at {format_clock(block.start_minute)} must end after it starts"
            )
        _check_setting(block)
        end = block.end_minute
    if end != _DAY_END:
        raise ValueError("the day must end at 24:00")
    return ordered


def _intervals(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """Return the dataIntervals or dataPoints of a series as mappings."""
    series = raw.get(key)
    if not isinstance(series, Mapping):
        return []
    items = series.get("dataIntervals") or series.get("dataPoints")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _numbers(items: Iterable[Mapping[str, Any]], unit: str | None) -> list[float]:
    values: list[float] = []
    for item in items:
        value = item.get("value")
        if unit is not None and isinstance(value, Mapping):
            value = value.get(unit)
        if isinstance(value, int | float) and not isinstance(value, bool):
            values.append(float(value))
    return values


def _hours(interval: Mapping[str, Any]) -> float:
    try:
        span = datetime.fromisoformat(interval["to"]) - datetime.fromisoformat(
            interval["from"]
        )
    except KeyError, TypeError, ValueError:
        return 0.0
    return span.total_seconds() / 3600


def _average(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _heating_hours(raw: Mapping[str, Any]) -> float | None:
    """Hours with a call for heat, else hours with the setting powered on."""
    if calls := _intervals(raw, "callForHeat"):
        heating = [item for item in calls if item.get("value") != _NO_HEAT_CALL]
    elif settings := _intervals(raw, "settings"):
        heating = [
            item
            for item in settings
            if isinstance(value := item.get("value"), Mapping)
            and value.get("power") == Power.ON
        ]
    else:
        return None
    return round(sum(_hours(item) for item in heating), 1)


def summarize_day_report(report: DayReport) -> DaySummary:
    """Summarize temperature, humidity in percent and heating hours of a day.

    Humidity arrives as a fraction although tado calls it a percentage.
    """
    measured = report.raw.get("measuredData")
    if not isinstance(measured, Mapping):
        measured = {}
    temperatures = _numbers(_intervals(measured, "insideTemperature"), "celsius")
    humidity = [
        round(value * 100, 1)
        for value in _numbers(_intervals(measured, "humidity"), None)
    ]
    return DaySummary(
        temperature_avg=_average(temperatures),
        temperature_min=round(min(temperatures), 1) if temperatures else None,
        temperature_max=round(max(temperatures), 1) if temperatures else None,
        humidity_avg_percent=_average(humidity),
        heating_hours=_heating_hours(report.raw),
    )

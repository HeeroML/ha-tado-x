"""Schedule validation and day report summaries."""

from collections.abc import Sequence
from dataclasses import dataclass

from .models import DayReport, ScheduleBlock


@dataclass(frozen=True, slots=True, kw_only=True)
class DaySummary:
    """Summary of one room day report."""

    temperature_avg: float | None
    temperature_min: float | None
    temperature_max: float | None
    humidity_avg_percent: float | None
    heating_hours: float | None


def validate_day(blocks: Sequence[ScheduleBlock]) -> tuple[ScheduleBlock, ...]:
    """Return the blocks of one weekday in order, or raise ValueError(reason).

    The day must start at 00:00, be contiguous and end at 24:00; every block
    must end after it starts; ON blocks need 5-30 °C and OFF blocks no
    temperature.
    """
    raise NotImplementedError


def summarize_day_report(report: DayReport) -> DaySummary:
    """Summarize temperature, humidity in percent and heating hours of a day."""
    raise NotImplementedError

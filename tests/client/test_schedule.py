"""Tests for schedule validation and day report summaries."""

from datetime import date
from typing import Any

import pytest

from custom_components.tado_x.client import (
    DayReport,
    DaySummary,
    DayType,
    Power,
    RoomSchedule,
    ScheduleBlock,
    Setting,
    summarize_day_report,
    validate_day,
)
from tests.tado_mock import load_fixture

DAY = date(2026, 10, 30)


def _block(
    start: int,
    end: int,
    temperature: float | None = 20.0,
    *,
    power: Power = Power.ON,
    day: DayType = DayType.MONDAY,
) -> ScheduleBlock:
    return ScheduleBlock(
        day=day,
        start_minute=start,
        end_minute=end,
        setting=Setting(power=power, temperature=temperature),
    )


def test_validate_fixture_days() -> None:
    """Every weekday of the fixture schedule is valid."""
    schedule = RoomSchedule.from_api(load_fixture("schedule.json"), room_id=1)
    for day in DayType:
        assert validate_day(schedule.day(day)) == schedule.day(day)


def test_validate_sorts_and_accepts_off_blocks() -> None:
    """Blocks come back in time order; OFF blocks need no temperature."""
    blocks = [
        _block(1320, 1440, None, power=Power.OFF),
        _block(0, 390, 5.0),
        _block(390, 1320, 30.0),
    ]
    assert [block.start_minute for block in validate_day(blocks)] == [0, 390, 1320]


@pytest.mark.parametrize(
    ("blocks", "reason"),
    [
        ([], "the day has no blocks"),
        (
            [_block(0, 720), _block(720, 1440, day=DayType.TUESDAY)],
            "all blocks must be on the same weekday",
        ),
        ([_block(30, 1440)], "the day must start at 00:00"),
        (
            [_block(0, 600), _block(660, 1440)],
            "the block at 11:00 must start at 10:00, where the previous block ends",
        ),
        (
            [_block(0, 720), _block(600, 1440)],
            "the block at 10:00 must start at 12:00, where the previous block ends",
        ),
        ([_block(0, 1320)], "the day must end at 24:00"),
        (
            [_block(0, 0), _block(0, 1440)],
            "the block at 00:00 must end after it starts",
        ),
        (
            [_block(0, 720, None), _block(720, 1440)],
            "the block at 00:00 needs a temperature between 5 and 30 °C",
        ),
        (
            [_block(0, 720, 4.5), _block(720, 1440)],
            "the block at 00:00 needs a temperature between 5 and 30 °C",
        ),
        (
            [_block(0, 720), _block(720, 1440, 30.5)],
            "the block at 12:00 needs a temperature between 5 and 30 °C",
        ),
        (
            [_block(0, 720), _block(720, 1440, 18.0, power=Power.OFF)],
            "the OFF block at 12:00 must not have a temperature",
        ),
    ],
)
def test_validate_rejects(blocks: list[ScheduleBlock], reason: str) -> None:
    """Gaps, overlaps, open ends, empty blocks and bad settings are rejected."""
    with pytest.raises(ValueError, match=f"^{reason}$"):
        validate_day(blocks)


def test_summarize_fixture() -> None:
    """Averages, extremes, humidity in percent and heating from callForHeat."""
    report = DayReport.from_api(load_fixture("day_report.json"), day=DAY)
    assert summarize_day_report(report) == DaySummary(
        temperature_avg=20.5,
        temperature_min=19.5,
        temperature_max=21.5,
        humidity_avg_percent=52.0,
        heating_hours=3.5,
    )


def test_summarize_falls_back_to_settings() -> None:
    """Without callForHeat the hours with the setting powered on count."""
    raw = load_fixture("day_report.json")
    del raw["callForHeat"]
    raw["settings"]["dataIntervals"][0]["value"]["power"] = "OFF"
    raw["settings"]["dataIntervals"].append({"from": "x", "to": "y", "value": "ON"})
    summary = summarize_day_report(DayReport.from_api(raw, day=DAY))
    assert summary.heating_hours == 3.5


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"measuredData": "nope", "callForHeat": [], "settings": {"dataIntervals": {}}},
        {"measuredData": {"insideTemperature": {"dataPoints": []}}},
    ],
)
def test_summarize_empty_report(raw: dict[str, Any]) -> None:
    """A report without data summarizes to unknown values."""
    assert summarize_day_report(DayReport(day=DAY, raw=raw)) == DaySummary(
        temperature_avg=None,
        temperature_min=None,
        temperature_max=None,
        humidity_avg_percent=None,
        heating_hours=None,
    )


def test_summarize_skips_bad_values() -> None:
    """Values that are not numbers and intervals that cannot be read are skipped."""
    raw = {
        "measuredData": {
            "insideTemperature": {
                "dataPoints": [
                    {"value": {"celsius": 20.0}},
                    {"value": {"celsius": True}},
                    {"value": "warm"},
                    "garbage",
                ]
            },
            "humidity": {"dataPoints": [{"value": 0.555}, {"value": None}]},
        },
        "callForHeat": {
            "dataIntervals": [
                {
                    "from": "2026-10-30T05:00:00Z",
                    "to": "2026-10-30T06:00:00Z",
                    "value": "HIGH",
                },
                {
                    "from": "2026-10-30T06:00:00Z",
                    "to": "2026-10-30T07:00:00",
                    "value": "LOW",
                },
                {"to": "2026-10-30T08:00:00Z", "value": "LOW"},
                {"from": 5, "to": "2026-10-30T09:00:00Z", "value": "LOW"},
                {
                    "from": "2026-10-30T09:00:00Z",
                    "to": "2026-10-30T10:00:00Z",
                    "value": "NONE",
                },
            ]
        },
    }
    summary = summarize_day_report(DayReport(day=DAY, raw=raw))
    assert (summary.temperature_avg, summary.temperature_min) == (20.0, 20.0)
    assert summary.humidity_avg_percent == 55.5
    assert summary.heating_hours == 1.0

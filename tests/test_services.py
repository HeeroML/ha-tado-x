"""Tests for the tado X services (#51, #60, #64, #69, #92)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import yaml

from custom_components.tado_x import services as services_module
from custom_components.tado_x.client import tariff_cents
from custom_components.tado_x.const import DOMAIN, OPT_HOLIDAY_MODE
from custom_components.tado_x.devices import (
    device_identifier,
    home_identifier,
    mobile_identifier,
    room_identifier,
)
from custom_components.tado_x.services import (
    ADD_METER_READING_SCHEMA,
    GET_DAY_REPORT_SCHEMA,
    HOME_SCHEMA,
    SET_CLIMATE_TIMER_SCHEMA,
    SET_EIQ_TARIFF_SCHEMA,
    SET_HOLIDAY_MODE_SCHEMA,
    SET_SCHEDULE_SCHEMA,
    SET_TEMPERATURE_OFFSET_SCHEMA,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_climate import (
    ROOMS,
    bodies,
    climate_id,
    entity_id,
    manual_path,
    setup,
    state_of,
    tick,
)
from tests.test_number import offset_id

NIGHT = "2026-10-30T23:30:00+00:00"
VALVE = "VA0000000101"
DEVICE = f"/homes/{HOME_B}/roomsAndDevices/devices/{VALVE}"
HOLIDAY = f"/homes/{HOME_B}/settings/holidayMode"
METER = f"/api/homes/{HOME_B}/meterReadings"
TARIFFS = f"/api/homes/{HOME_B}/tariffs"
DAY_REPORT = f"/api/v2/homes/{HOME_B}/zones/1/dayReport"
TIMER_30 = {"type": "TIMER", "durationInSeconds": 1800}
BLOCKS = [
    {"start": "00:00", "end": "06:30", "temperature": 17},
    {"start": "06:30", "end": "22:00", "temperature": 21},
    {"start": "22:00", "end": "24:00", "power": "off"},
]


def schedule_path(room_id: int) -> str:
    """Return the schedule path of a room."""
    return f"/homes/{HOME_B}/rooms/{room_id}/schedule"


async def call(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any] | None = None,
    *,
    target: dict[str, Any] | None = None,
    response: bool = False,
) -> dict[str, Any]:
    """Call a tado X service, wait for it and return its response, if any."""
    result = await hass.services.async_call(
        DOMAIN,
        service,
        data or {},
        blocking=True,
        target=target,
        return_response=response,
    )
    if not response:
        return {}
    assert isinstance(result, dict)
    return result


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(
        (Path(services_module.__file__).parent / "services.yaml").read_text("utf-8")
    )
    return services


def device_id(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: tuple[str, str]
) -> str:
    """Return the registry id of a device of the entry."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        identifier, entry.entry_id
    )
    assert device is not None
    return device.id


async def test_set_climate_timer_accepts_every_target(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Entity lists, devices, areas and labels all reach the right rooms (#64)."""
    entry = await setup(hass, freezer)
    area = area_registry.async_create("Kinderzimmer")
    dr.async_get(hass).async_update_device(
        device_id(hass, entry, room_identifier(HOME_B, 4)), area_id=area.id
    )
    label = label_registry.async_create("Heizung")
    entity_registry.async_update_entity(climate_id(hass, 5), labels={label.label_id})

    for target in (
        {ATTR_ENTITY_ID: [climate_id(hass, 1), climate_id(hass, 2)]},
        {ATTR_DEVICE_ID: device_id(hass, entry, room_identifier(HOME_B, 3))},
        {"area_id": area.id},
        {"label_id": label.label_id},
    ):
        await call(
            hass,
            "set_climate_timer",
            {"temperature": 22, "duration": 30},
            target=target,
        )

    for room_id in range(1, 6):
        assert bodies(mock_tado, "POST", manual_path(room_id)) == [
            {
                "setting": {
                    "power": "ON",
                    "isBoost": False,
                    "temperature": {"value": 22.0},
                },
                "termination": TIMER_30,
            }
        ]
    assert mock_tado.calls("POST", manual_path(6)) == []


@pytest.mark.parametrize(
    ("data", "termination"),
    [
        ({"duration": 30}, TIMER_30),
        ({"duration": 30, "termination": "timer"}, TIMER_30),
        ({"termination": "manual"}, {"type": "MANUAL"}),
        ({"termination": "next_time_block"}, {"type": "NEXT_TIME_BLOCK"}),
        ({}, {"type": "TIMER", "durationInSeconds": 3600}),
    ],
    ids=["duration", "timer", "manual", "next_time_block", "policy"],
)
async def test_set_climate_timer_terminations(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    termination: dict[str, Any],
) -> None:
    """The v1 data form works; termination overrides the policy per call (#94)."""
    await setup(hass, freezer)

    await call(
        hass,
        "set_climate_timer",
        {ATTR_ENTITY_ID: climate_id(hass, 2), "temperature": 19.5, **data},
    )

    assert bodies(mock_tado, "POST", manual_path(2))[0]["termination"] == termination
    assert state_of(hass, climate_id(hass, 2)).attributes["temperature"] == 19.5


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"termination": "timer"}, "timer_requires_duration"),
        ({"termination": "manual", "duration": 30}, "duration_requires_timer"),
    ],
)
async def test_set_climate_timer_conflicts(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    key: str,
) -> None:
    """A timer needs a duration, and only a timer takes one."""
    await setup(hass, freezer)

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "set_climate_timer",
            {"temperature": 21, **data},
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
        )

    assert raised.value.translation_key == key
    assert mock_tado.calls("POST") == []


async def test_set_climate_timer_checks_the_range(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The temperature range is 5 to 30 °C, as for the climate entity."""
    await setup(hass, freezer)

    with pytest.raises(probatio.Invalid):
        await call(
            hass,
            "set_climate_timer",
            {ATTR_ENTITY_ID: climate_id(hass, 1), "temperature": 31},
        )

    assert mock_tado.calls("POST") == []


async def test_get_schedule(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The schedule comes back per weekday; OFF blocks have no temperature."""
    schedule = mock_tado.home(HOME_B).schedules[1]["schedule"]
    schedule[-1]["setting"] = {"power": "OFF", "temperature": None}
    await setup(hass, freezer)
    living = climate_id(hass, 1)

    response = await call(
        hass, "get_schedule", target={ATTR_ENTITY_ID: living}, response=True
    )

    days = response[living]["days"]
    assert list(days) == [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    assert days["monday"] == [
        {"start": "00:00", "end": "06:30", "power": "on", "temperature": 18.0},
        {"start": "06:30", "end": "22:00", "power": "on", "temperature": 21.0},
        {"start": "22:00", "end": "24:00", "power": "on", "temperature": 18.0},
    ]
    assert days["sunday"][-1] == {"start": "22:00", "end": "24:00", "power": "off"}


async def test_get_schedule_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failed read raises a translated error."""
    await setup(hass, freezer)
    mock_tado.fail("GET", schedule_path(1), status=500)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "get_schedule",
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
            response=True,
        )

    assert raised.value.translation_key == "cannot_connect"


async def test_set_schedule_posts_one_day_at_a_time(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Each weekday is one POST, then the rooms are read back once."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    await call(
        hass,
        "set_schedule",
        {"days": ["tuesday", "monday"], "blocks": BLOCKS},
        target={ATTR_ENTITY_ID: climate_id(hass, 3)},
    )
    await tick(hass, freezer, 5)

    def day(name: str) -> dict[str, Any]:
        return {
            "dayType": name,
            "daySchedule": [
                {
                    "dayType": name,
                    "start": "00:00",
                    "end": "06:30",
                    "setting": {"power": "ON", "temperature": {"value": 17.0}},
                },
                {
                    "dayType": name,
                    "start": "06:30",
                    "end": "22:00",
                    "setting": {"power": "ON", "temperature": {"value": 21.0}},
                },
                {
                    "dayType": name,
                    "start": "22:00",
                    "end": "24:00",
                    "setting": {"power": "OFF", "temperature": None},
                },
            ],
        }

    assert bodies(mock_tado, "POST", schedule_path(3)) == [
        day("MONDAY"),
        day("TUESDAY"),
    ]
    assert len(mock_tado.calls("GET", ROOMS)) == 1


@pytest.mark.parametrize(
    ("blocks", "reason"),
    [
        (BLOCKS[1:], "the day must start at 00:00"),
        ([BLOCKS[0], BLOCKS[2]], "the block at 22:00 must start at 06:30"),
        (BLOCKS[:2], "the day must end at 24:00"),
        (
            [*BLOCKS[:2], {"start": "22:00", "end": "24:00"}],
            "the block at 22:00 needs a temperature between 5 and 30 °C",
        ),
        (
            [
                *BLOCKS[:2],
                {"start": "22:00", "end": "24:00", "power": "off", "temperature": 5},
            ],
            "the OFF block at 22:00 must not have a temperature",
        ),
    ],
    ids=["start", "gap", "end", "no_temperature", "off_with_temperature"],
)
async def test_invalid_schedule_sends_nothing(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    blocks: list[dict[str, Any]],
    reason: str,
) -> None:
    """Every day is validated before the first request."""
    await setup(hass, freezer)

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "set_schedule",
            {"days": "monday", "blocks": blocks},
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
        )

    assert raised.value.translation_key == "invalid_schedule"
    assert raised.value.translation_placeholders is not None
    assert raised.value.translation_placeholders["reason"].startswith(reason)
    assert mock_tado.calls("POST") == []


async def test_schedule_time_format(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Times are HH:MM up to 24:00, and the error names the bad one."""
    await setup(hass, freezer)

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "set_schedule",
            {
                "days": ["monday"],
                "blocks": [{"start": "0:00", "end": "25:00", "temperature": 20}],
            },
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
        )

    assert raised.value.translation_key == "invalid_schedule"
    assert raised.value.translation_placeholders == {
        "reason": "25:00 is not a time between 00:00 and 24:00"
    }
    assert mock_tado.calls("POST") == []


async def test_schedule_partially_written(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failure after the first day names the days tado already saved."""
    await setup(hass, freezer)
    mock_tado.set_quota(remaining=1)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "set_schedule",
            {"days": ["monday", "tuesday", "wednesday"], "blocks": BLOCKS},
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
        )

    assert raised.value.translation_key == "schedule_partially_written"
    assert raised.value.translation_placeholders == {"done_days": "monday"}
    assert len(mock_tado.calls("POST", schedule_path(1))) == 2


async def test_schedule_not_written(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failure on the first day is a plain request error."""
    await setup(hass, freezer)
    mock_tado.fail("POST", schedule_path(1), status=422)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "set_schedule",
            {"days": ["monday", "tuesday"], "blocks": BLOCKS},
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
        )

    assert raised.value.translation_key == "request_rejected"
    assert len(mock_tado.calls("POST", schedule_path(1))) == 1


@pytest.mark.parametrize(
    ("data", "day"),
    [({}, "2026-10-31"), ({"date": "2026-10-01"}, "2026-10-01")],
    ids=["today_at_the_home", "given"],
)
async def test_get_day_report(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    day: str,
) -> None:
    """The report comes with a summary; today is the date in Berlin (C18)."""
    await setup(hass, freezer)
    freezer.move_to(NIGHT)
    living = climate_id(hass, 1)

    response = await call(
        hass,
        "get_day_report",
        data,
        target={ATTR_ENTITY_ID: living},
        response=True,
    )

    report = response[living]
    assert report["summary"] == {
        "temperature_avg": 20.5,
        "temperature_min": 19.5,
        "temperature_max": 21.5,
        "humidity_avg_percent": 52.0,
        "heating_hours": 3.5,
    }
    assert report["report"] == mock_tado.home(HOME_B).day_report
    requests = mock_tado.calls("GET", DAY_REPORT)
    assert [request.url.query["date"] for request in requests] == [day]


@pytest.mark.parametrize(
    ("status", "error", "key"),
    [
        (403, ServiceValidationError, "day_report_forbidden"),
        (500, HomeAssistantError, "cannot_connect"),
    ],
)
async def test_get_day_report_errors(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    status: int,
    error: type[HomeAssistantError],
    key: str,
) -> None:
    """Past days need Auto-Assist; other failures are request errors."""
    await setup(hass, freezer)
    mock_tado.fail("GET", DAY_REPORT, status=status)

    with pytest.raises(error) as raised:
        await call(
            hass,
            "get_day_report",
            {"date": "2026-09-01"},
            target={ATTR_ENTITY_ID: climate_id(hass, 1)},
            response=True,
        )

    assert raised.value.translation_key == key


async def test_set_temperature_offset(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The offset is one PATCH to the device, with no read back (#51, #52)."""
    entry = await setup(hass, freezer)
    mock_tado.clear_calls()

    await call(
        hass,
        "set_temperature_offset",
        {"device_id": device_id(hass, entry, device_identifier(VALVE)), "offset": -2.5},
    )

    assert [(r.method, r.path, r.body) for r in mock_tado.calls()] == [
        ("PATCH", DEVICE, {"temperatureOffset": -2.5})
    ]
    assert state_of(hass, offset_id(hass)).state == "-2.5"


@pytest.mark.parametrize(
    ("identifier", "name"),
    [
        (room_identifier(HOME_B, 1), "Wohnzimmer"),
        (home_identifier(HOME_B), "Haus 1000002"),
        (device_identifier("IB0000000001"), "Bridge X"),
        (mobile_identifier(5000001), "Phone"),
        (device_identifier("VA0000000199"), "Old valve"),
    ],
    ids=["room", "home", "bridge", "phone", "not_reported"],
)
async def test_set_temperature_offset_needs_a_capable_device(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
    identifier: tuple[str, str],
    name: str,
) -> None:
    """Rooms, the home, the bridge and phones have no offset; it says so (#51)."""
    entry = await setup(hass, freezer, mobile_devices=True)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={device_identifier("VA0000000199")},
        name="Old valve",
    )
    mock_tado.clear_calls()

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "set_temperature_offset",
            {"device_id": device_id(hass, entry, identifier), "offset": 1},
        )

    assert raised.value.translation_key == "device_not_offset_capable"
    assert raised.value.translation_placeholders == {"device": name}
    assert mock_tado.calls() == []


async def test_set_temperature_offset_unknown_device(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A device id that does not exist is refused before any request."""
    await setup(hass, freezer)
    mock_tado.clear_calls()

    with pytest.raises(ServiceValidationError):
        await call(
            hass, "set_temperature_offset", {"device_id": "missing", "offset": 1}
        )

    assert mock_tado.calls() == []


async def test_set_temperature_offset_api_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected offset raises instead of only logging (#51)."""
    entry = await setup(hass, freezer)
    mock_tado.fail("PATCH", DEVICE, status=422)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "set_temperature_offset",
            {
                "device_id": device_id(hass, entry, device_identifier(VALVE)),
                "offset": 3,
            },
        )

    assert type(raised.value) is HomeAssistantError
    assert raised.value.translation_key == "request_rejected"
    assert state_of(hass, offset_id(hass)).state == "-1.5"


async def test_service_auth_error_starts_reauth(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A service never raises ConfigEntryAuthFailed; it starts reauth instead."""
    entry = await setup(hass, freezer)
    mock_tado.fail("PATCH", DEVICE, status=401)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "set_temperature_offset",
            {
                "device_id": device_id(hass, entry, device_identifier(VALVE)),
                "offset": 3,
            },
        )
    await hass.async_block_till_done()

    assert raised.value.translation_key == "auth_failed"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    ("data", "day"),
    [({}, "2026-10-31"), ({"date": "2026-10-15"}, "2026-10-15")],
    ids=["today_at_the_home", "given"],
)
async def test_add_meter_reading(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    day: str,
) -> None:
    """The reading date defaults to today in the home's time zone (C18)."""
    await setup(hass, freezer)
    freezer.move_to(NIGHT)

    await call(hass, "add_meter_reading", {"reading": 12345, **data})

    assert bodies(mock_tado, "POST", METER) == [{"date": day, "reading": 12345}]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("add_meter_reading", {"reading": 1}),
        ("set_eiq_tariff", {"tariff": 1, "unit": "m3", "start_date": "2026-11-01"}),
    ],
)
async def test_energy_iq_needs_an_eligible_home(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    service: str,
    data: dict[str, Any],
) -> None:
    """A home without Energy IQ is refused before any request."""
    mock_tado.home(HOME_B).info["isEnergyIqEligible"] = False
    await setup(hass, freezer)
    mock_tado.clear_calls()

    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, service, data)

    assert raised.value.translation_key == "feature_unavailable"
    assert raised.value.translation_placeholders == {"feature": "Energy IQ"}
    assert mock_tado.calls() == []


async def test_add_meter_reading_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected reading raises a translated error."""
    await setup(hass, freezer)
    mock_tado.fail("POST", METER, status=400)

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "add_meter_reading", {"reading": 1})

    assert raised.value.translation_key == "request_rejected"


@pytest.mark.parametrize(
    ("service", "data", "path"),
    [
        ("add_meter_reading", {"reading": 1}, METER),
        (
            "set_eiq_tariff",
            {"tariff": 1, "unit": "m3", "start_date": "2026-11-01"},
            TARIFFS,
        ),
    ],
)
async def test_energy_iq_refusal_in_a_2xx_is_an_error(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    service: str,
    data: dict[str, Any],
    path: str,
) -> None:
    """A 2xx that carries only a message is a refusal, not a success."""
    await setup(hass, freezer)
    reason = "Reading must be greater than previous reading"
    mock_tado.fail("POST", path, status=200, body={"message": reason})

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, service, data)

    assert raised.value.translation_key == "request_rejected"
    assert raised.value.translation_placeholders == {"status": "200", "title": reason}


@pytest.mark.parametrize(
    ("data", "body"),
    [
        (
            {"tariff": 0.285, "unit": "kwh", "start_date": "2026-11-01"},
            {"tariffInCents": 29, "unit": "kWh", "startDate": "2026-11-01"},
        ),
        (
            {
                "tariff": 0.29,
                "unit": "m3",
                "start_date": "2026-11-01",
                "end_date": "2027-10-31",
            },
            {
                "tariffInCents": 29,
                "unit": "m3",
                "startDate": "2026-11-01",
                "endDate": "2027-10-31",
            },
        ),
    ],
    ids=["half_up", "with_end"],
)
async def test_set_eiq_tariff(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    body: dict[str, Any],
) -> None:
    """Tariffs go out in whole cents, rounded half up (C19)."""
    await setup(hass, freezer)

    await call(hass, "set_eiq_tariff", data)

    assert bodies(mock_tado, "POST", TARIFFS) == [body]


async def test_set_eiq_tariff_starts_today_at_the_home(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without a start date the tariff starts today at the home, as in 1.8."""
    await setup(hass, freezer)
    freezer.move_to(NIGHT)

    await call(hass, "set_eiq_tariff", {"tariff": 0.12, "unit": "kWh"})

    assert bodies(mock_tado, "POST", TARIFFS) == [
        {"tariffInCents": 12, "unit": "kWh", "startDate": "2026-10-31"}
    ]


def test_tariff_selector_steps_in_cents() -> None:
    """The tariff field offers no finer step than the cent tado stores."""
    tariff = _services()["set_eiq_tariff"]["fields"]["tariff"]
    assert tariff_cents(tariff["selector"]["number"]["step"]) == 1


@pytest.mark.parametrize(
    ("service", "schema"),
    [
        ("set_climate_timer", SET_CLIMATE_TIMER_SCHEMA),
        ("set_schedule", SET_SCHEDULE_SCHEMA),
        ("get_day_report", GET_DAY_REPORT_SCHEMA),
        ("set_temperature_offset", SET_TEMPERATURE_OFFSET_SCHEMA),
        ("add_meter_reading", ADD_METER_READING_SCHEMA),
        ("set_eiq_tariff", SET_EIQ_TARIFF_SCHEMA),
        ("get_holiday_mode", HOME_SCHEMA),
        ("set_holiday_mode", SET_HOLIDAY_MODE_SCHEMA),
    ],
)
def test_services_yaml_marks_the_required_fields(
    service: str, schema: probatio.Schema | dict[Any, Any]
) -> None:
    """A field is required in the UI exactly when the schema requires it."""
    keys = schema.schema if isinstance(schema, probatio.Schema) else schema
    fields = _services()[service].get("fields", {})

    assert {name for name, field in fields.items() if field.get("required")} == {
        str(key) for key in keys if isinstance(key, probatio.Required)
    }


async def test_set_eiq_tariff_error(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected tariff raises a translated error."""
    await setup(hass, freezer)
    mock_tado.fail("POST", TARIFFS, status=500)

    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass,
            "set_eiq_tariff",
            {"tariff": 1, "unit": "kWh", "start_date": "2026-11-01"},
        )

    assert raised.value.translation_key == "cannot_connect"


async def test_get_holiday_mode(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The holiday mode of the only home comes back as a response."""
    mock_tado.home(HOME_B).holiday_mode.update(
        enabled=True, startDate="2026-12-20T09:00:00Z", endDate=None
    )
    await setup(hass, freezer)

    response = await call(hass, "get_holiday_mode", response=True)

    assert response == {
        "enabled": True,
        "active": False,
        "start": "2026-12-20T09:00:00+00:00",
        "end": None,
    }


async def test_get_holiday_mode_passes_the_poll_floor(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A user's holiday read is a command, so the poll floor does not refuse it."""
    await setup(hass, freezer)
    mock_tado.set_quota(remaining=10, reset_in=timedelta(hours=1))

    await call(hass, "get_holiday_mode", response=True)
    response = await call(hass, "get_holiday_mode", response=True)

    assert response["enabled"] is False
    assert len(mock_tado.calls("GET", HOLIDAY)) == 2


async def test_set_holiday_mode_keeps_unknown_settings(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The PATCH carries what tado sent back, with the new period in UTC."""
    entry = await setup(hass, freezer, **{OPT_HOLIDAY_MODE: True})
    mock_tado.home(HOME_B).holiday_mode["roomSettings"] = [{"roomId": 1, "x": 1}]
    mock_tado.clear_calls()

    await call(
        hass,
        "set_holiday_mode",
        {
            "enabled": True,
            "start": "2026-12-20T10:00:00+01:00",
            "end": "2027-01-02T18:00:00+01:00",
        },
    )

    assert bodies(mock_tado, "PATCH", HOLIDAY) == [
        {
            "enabled": True,
            "active": False,
            "startDate": "2026-12-20T09:00:00Z",
            "endDate": "2027-01-02T17:00:00Z",
            "roomSettings": [{"roomId": 1, "x": 1}],
            "hotWaterSettings": None,
        }
    ]
    holiday = entry.runtime_data.devices.data.holiday_mode
    assert holiday is not None
    assert holiday.enabled is True
    assert holiday.end is not None
    assert holiday.end.isoformat() == "2027-01-02T17:00:00+00:00"


async def test_turn_off_holiday_mode_keeps_the_period(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Turning holiday mode off sends only the switch; the shown state follows."""
    mock_tado.home(HOME_B).holiday_mode.update(
        enabled=True, startDate="2026-12-20T09:00:00Z"
    )
    entry = await setup(hass, freezer, **{OPT_HOLIDAY_MODE: True})

    await call(hass, "set_holiday_mode", {"enabled": False})

    assert bodies(mock_tado, "PATCH", HOLIDAY)[0]["startDate"] == (
        "2026-12-20T09:00:00Z"
    )
    holiday = entry.runtime_data.devices.data.holiday_mode
    assert holiday is not None
    assert holiday.enabled is False

    await call(hass, "set_holiday_mode", {"enabled": False})

    assert entry.runtime_data.devices.data.holiday_mode == holiday


@pytest.mark.parametrize(
    ("data", "shown"),
    [
        (
            {
                "enabled": True,
                "start": "2026-10-30T09:00:00+00:00",
                "end": "2026-11-02T18:00:00+00:00",
            },
            "on",
        ),
        ({"enabled": True}, "on"),
        (
            {
                "enabled": True,
                "start": "2026-12-20T09:00:00+00:00",
                "end": "2027-01-02T18:00:00+00:00",
            },
            "off",
        ),
        ({"enabled": True, "end": "2026-10-30T09:00:00+00:00"}, "off"),
    ],
    ids=["period_now", "no_period", "period_later", "period_over"],
)
async def test_set_holiday_mode_shows_at_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    data: dict[str, Any],
    shown: str,
) -> None:
    """The holiday sensor shows the written mode at once, without a holiday read."""
    await setup(hass, freezer, **{OPT_HOLIDAY_MODE: True})
    sensor = entity_id(hass, "binary_sensor", f"{HOME_B}_holiday_mode")
    assert state_of(hass, sensor).state == "off"
    mock_tado.clear_calls()

    await call(hass, "set_holiday_mode", data)

    assert state_of(hass, sensor).state == shown
    assert len(mock_tado.calls("GET", HOLIDAY)) == 1

    await call(hass, "set_holiday_mode", {"enabled": False})

    assert state_of(hass, sensor).state == "off"


async def test_set_holiday_mode_without_the_feature(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without the holiday option nothing is polled, and the service still works."""
    entry = await setup(hass, freezer)

    await call(hass, "set_holiday_mode", {"enabled": True})

    assert bodies(mock_tado, "PATCH", HOLIDAY)[0]["enabled"] is True
    assert entry.runtime_data.devices.data.holiday_mode is None


@pytest.mark.parametrize(
    ("status", "error", "key"),
    [
        (500, HomeAssistantError, "cannot_connect"),
        (403, ServiceValidationError, "holiday_mode_unavailable"),
        (404, ServiceValidationError, "holiday_mode_unavailable"),
    ],
)
async def test_holiday_mode_read_failure_aborts(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    status: int,
    error: type[HomeAssistantError],
    key: str,
) -> None:
    """A failed read sends no PATCH that could wipe the room settings."""
    await setup(hass, freezer)
    mock_tado.fail("GET", HOLIDAY, status=status)

    with pytest.raises(error) as raised:
        await call(hass, "set_holiday_mode", {"enabled": True})
    with pytest.raises(error):
        await call(hass, "get_holiday_mode", response=True)

    assert raised.value.translation_key == key
    assert mock_tado.calls("PATCH", HOLIDAY) == []

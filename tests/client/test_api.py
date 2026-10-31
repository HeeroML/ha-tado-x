"""Tests for the endpoints of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientSession
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tado_x.client import (
    DayType,
    HomeGeneration,
    HomeInfo,
    MemoryTokenStore,
    Power,
    PresenceSetting,
    QuickAction,
    QuotaTracker,
    RateLimitInfo,
    ScheduleBlock,
    Setting,
    TadoXClient,
    TadoXConnectionError,
    TadoXHome,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    TariffUnit,
    Telemetry,
    Termination,
    TokenManager,
    TokenSet,
)
from custom_components.tado_x.client.api import tariff_cents
from custom_components.tado_x.client.const import MY_API_URL
from tests.tado_mock import (
    HOME_A,
    HOME_B,
    HOME_C,
    TadoCloudMock,
    entry_tokens,
    load_fixture,
)

DAY = date(2026, 10, 30)
CLIENT_LOGGER = "custom_components.tado_x.client"
HOPS_B = f"https://hops.tado.com/homes/{HOME_B}"
MY_B = f"{MY_API_URL}/homes/{HOME_B}"
EIQ_B = f"https://energy-insights.tado.com/api/homes/{HOME_B}"

type Observed = list[tuple[int, str, Any, datetime]]


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def observed() -> Observed:
    """Return the payloads the observer received."""
    return []


@pytest.fixture
def client(session: ClientSession, observed: Observed) -> TadoXClient:
    """Return a client with a fresh token whose observer records into observed."""
    access, refresh = entry_tokens(HOME_B)
    tokens = TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    client = TadoXClient(
        session, TokenManager(session, MemoryTokenStore(tokens)), QuotaTracker()
    )
    client.set_payload_observer(lambda *args: observed.append(args))
    return client


@pytest.fixture
def home(client: TadoXClient) -> TadoXHome:
    """Return the API of home 1000002."""
    return client.home(HOME_B)


def _only_call(mock_tado: TadoCloudMock, method: str) -> tuple[str, Any]:
    [call] = mock_tado.calls(method)
    return str(call.url.with_query(None)), call.body


async def test_polled_reads(
    mock_tado: TadoCloudMock, home: TadoXHome, observed: Observed
) -> None:
    """Every polled read parses its fixture and feeds the cache, except phones."""
    mock_tado.home(HOME_B).flow_temperature = load_fixture("flow_temperature.json")

    rooms = await home.async_get_rooms()
    topology = await home.async_get_topology()
    state = await home.async_get_home_state()
    weather = await home.async_get_weather()
    comfort = await home.async_get_air_comfort()
    phones = await home.async_get_mobile_devices()
    running = await home.async_get_running_times(DAY)
    flow = await home.async_get_flow_temperature()
    holiday = await home.async_get_holiday_mode()

    assert list(rooms) == [1, 2, 3, 4, 5, 6]
    assert topology.bridge is not None
    assert topology.bridge.serial == "IB0000000001"
    assert state.setting is PresenceSetting.AUTO
    assert weather.outdoor_temperature is not None
    assert set(comfort.rooms) == set(rooms)
    assert list(phones.phones) == [5000001, 5000002, 5000003]
    assert running.by_room[1] == timedelta(hours=1)
    assert flow.max_flow_temperature == 50
    assert holiday.enabled is False

    assert [(home_id, key) for home_id, key, _, _ in observed] == [
        (HOME_B, "rooms"),
        (HOME_B, "topology"),
        (HOME_B, "home_state"),
        (HOME_B, "weather"),
        (HOME_B, "air_comfort"),
        (HOME_B, "running_times"),
        (HOME_B, "flow_temperature"),
        (HOME_B, "holiday_mode"),
    ]
    assert [str(call.url) for call in mock_tado.calls("GET")] == [
        f"{HOPS_B}/rooms",
        f"{HOPS_B}/roomsAndDevices",
        f"{MY_B}/state",
        f"{MY_B}/weather",
        f"{HOPS_B}/airComfort",
        f"{MY_B}/mobileDevices",
        f"https://minder.tado.com/v1/homes/{HOME_B}/runningTimes?from=2026-10-30&to=2026-10-30",
        f"{HOPS_B}/settings/flowTemperatureOptimization",
        f"{HOPS_B}/settings/holidayMode",
    ]


async def test_missing_flow_temperature(
    mock_tado: TadoCloudMock, home: TadoXHome, observed: Observed
) -> None:
    """A home without OpenTherm answers the flow temperature read with 404."""
    with pytest.raises(TadoXNotFoundError):
        await home.async_get_flow_temperature()
    assert observed == []


async def test_on_demand_reads(mock_tado: TadoCloudMock, home: TadoXHome) -> None:
    """Single room, schedule and day report reads."""
    room = await home.async_get_room(2)
    schedule = await home.async_get_schedule(3)
    report = await home.async_get_day_report(4, DAY)

    assert room.id == 2
    assert schedule.room_id == 3
    assert len(schedule.day(DayType.MONDAY)) == 3
    assert report.day == DAY
    assert report.raw["hoursInDay"] == 24
    assert [str(call.url) for call in mock_tado.calls("GET")] == [
        f"{HOPS_B}/rooms/2",
        f"{HOPS_B}/rooms/3/schedule",
        f"{MY_B}/zones/4/dayReport?date=2026-10-30",
    ]


async def test_commands_pass_the_poll_floor(
    mock_tado: TadoCloudMock, client: TadoXClient, home: TadoXHome
) -> None:
    """Below the poll floor polls are refused locally while user actions pass."""
    client.quota.observe(
        RateLimitInfo(
            limit=20000,
            window=None,
            remaining=5,
            reset_in=timedelta(hours=1),
            observed_at=datetime.now(UTC),
        ),
        status=200,
    )
    with pytest.raises(TadoXRateLimitError):
        await home.async_get_rooms()
    with pytest.raises(TadoXRateLimitError):
        await client.async_get_home_info(HOME_B)
    await home.async_get_room(1)
    await home.async_read_holiday_mode()
    await home.async_resume_schedule(1)
    await client.async_get_account()
    assert len(mock_tado.calls()) == 4


@pytest.mark.parametrize(
    ("setting", "termination", "body"),
    [
        (
            Setting(power=Power.ON, temperature=22.5),
            Termination.timer(timedelta(hours=1)),
            {
                "setting": {
                    "power": "ON",
                    "isBoost": False,
                    "temperature": {"value": 22.5},
                },
                "termination": {"type": "TIMER", "durationInSeconds": 3600},
            },
        ),
        (
            Setting(power=Power.ON, temperature=21.0),
            Termination.next_time_block(),
            {
                "setting": {
                    "power": "ON",
                    "isBoost": False,
                    "temperature": {"value": 21.0},
                },
                "termination": {"type": "NEXT_TIME_BLOCK"},
            },
        ),
        (
            Setting(power=Power.ON, temperature=19.5),
            Termination.manual(),
            {
                "setting": {
                    "power": "ON",
                    "isBoost": False,
                    "temperature": {"value": 19.5},
                },
                "termination": {"type": "MANUAL"},
            },
        ),
        (
            Setting(power=Power.OFF),
            Termination.manual(),
            {"setting": {"power": "OFF"}, "termination": {"type": "MANUAL"}},
        ),
    ],
)
async def test_manual_control(
    mock_tado: TadoCloudMock,
    home: TadoXHome,
    setting: Setting,
    termination: Termination,
    body: dict[str, Any],
) -> None:
    """Manual control always sends an explicit termination."""
    await home.async_set_manual_control(2, setting, termination)
    assert _only_call(mock_tado, "POST") == (f"{HOPS_B}/rooms/2/manualControl", body)
    [call] = mock_tado.calls("POST")
    assert call.headers["Content-Type"] == "application/json"


type Command = Callable[[TadoXHome], Awaitable[None]]


@pytest.mark.parametrize(
    ("command", "method", "url", "body"),
    [
        (
            lambda home: home.async_resume_schedule(1),
            "DELETE",
            f"{HOPS_B}/rooms/1/manualControl",
            None,
        ),
        (
            lambda home: home.async_set_open_window(5, active=True),
            "POST",
            f"{HOPS_B}/rooms/5/openWindow",
            None,
        ),
        (
            lambda home: home.async_set_open_window(5, active=False),
            "DELETE",
            f"{HOPS_B}/rooms/5/openWindow",
            None,
        ),
        (
            lambda home: home.async_quick_action(QuickAction.BOOST),
            "POST",
            f"{HOPS_B}/quickActions/boost",
            None,
        ),
        (
            lambda home: home.async_quick_action(QuickAction.ALL_OFF),
            "POST",
            f"{HOPS_B}/quickActions/allOff",
            None,
        ),
        (
            lambda home: home.async_quick_action(QuickAction.RESUME_SCHEDULE),
            "POST",
            f"{HOPS_B}/quickActions/resumeSchedule",
            None,
        ),
        (
            lambda home: home.async_set_presence(PresenceSetting.AWAY),
            "PUT",
            f"{MY_B}/presenceLock",
            {"homePresence": "AWAY"},
        ),
        (
            lambda home: home.async_set_presence(PresenceSetting.HOME),
            "PUT",
            f"{MY_B}/presenceLock",
            {"homePresence": "HOME"},
        ),
        (
            lambda home: home.async_set_presence(PresenceSetting.AUTO),
            "DELETE",
            f"{MY_B}/presenceLock",
            None,
        ),
        (
            lambda home: home.async_update_device(
                "VA0000000101", temperature_offset=-1.5
            ),
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/devices/VA0000000101",
            {"temperatureOffset": -1.5},
        ),
        (
            lambda home: home.async_update_device("VA0000000101", child_lock=True),
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/devices/VA0000000101",
            {"childLockEnabled": True},
        ),
        (
            lambda home: home.async_update_device(
                "VA0000000101", temperature_offset=0.0, child_lock=False
            ),
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/devices/VA0000000101",
            {"temperatureOffset": 0.0, "childLockEnabled": False},
        ),
        (
            lambda home: home.async_identify_device("SU0000000102"),
            "POST",
            f"{HOPS_B}/roomsAndDevices/devices/SU0000000102/identify",
            None,
        ),
        (
            lambda home: home.async_set_room_termination_default(
                6, Termination.timer(timedelta(minutes=30))
            ),
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/rooms/6",
            {
                "deviceManualControlTermination": {
                    "type": "TIMER",
                    "durationInSeconds": 1800,
                }
            },
        ),
        (
            lambda home: home.async_set_room_termination_default(
                1, Termination.next_time_block()
            ),
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/rooms/1",
            {"deviceManualControlTermination": {"type": "NEXT_TIME_BLOCK"}},
        ),
        (
            lambda home: home.async_set_flow_temperature(maximum=45),
            "PATCH",
            f"{HOPS_B}/settings/flowTemperatureOptimization",
            {"maxFlowTemperature": 45},
        ),
        (
            lambda home: home.async_set_flow_temperature(auto_adaptation=True),
            "PATCH",
            f"{HOPS_B}/settings/flowTemperatureOptimization",
            {"autoAdaptation": {"enabled": True}},
        ),
        (
            lambda home: home.async_set_flow_temperature(
                maximum=55, auto_adaptation=False
            ),
            "PATCH",
            f"{HOPS_B}/settings/flowTemperatureOptimization",
            {"maxFlowTemperature": 55, "autoAdaptation": {"enabled": False}},
        ),
        (
            lambda home: home.async_add_meter_reading(1234, DAY),
            "POST",
            f"{EIQ_B}/meterReadings",
            {"date": "2026-10-30", "reading": 1234},
        ),
        (
            lambda home: home.async_add_tariff(
                cents=29, unit=TariffUnit.KWH, start=DAY, end=date(2026, 12, 31)
            ),
            "POST",
            f"{EIQ_B}/tariffs",
            {
                "tariffInCents": 29,
                "unit": "kWh",
                "startDate": "2026-10-30",
                "endDate": "2026-12-31",
            },
        ),
        (
            lambda home: home.async_add_tariff(
                cents=85, unit=TariffUnit.M3, start=DAY, end=None
            ),
            "POST",
            f"{EIQ_B}/tariffs",
            {"tariffInCents": 85, "unit": "m3", "startDate": "2026-10-30"},
        ),
    ],
)
async def test_command_bodies(
    mock_tado: TadoCloudMock,
    home: TadoXHome,
    command: Command,
    method: str,
    url: str,
    body: dict[str, Any] | None,
) -> None:
    """Every command sends exactly one request with the expected body."""
    mock_tado.home(HOME_B).flow_temperature = load_fixture("flow_temperature.json")
    await command(home)
    assert _only_call(mock_tado, method) == (url, body)
    assert len(mock_tado.calls()) == 1


@pytest.mark.parametrize(
    "command",
    [
        lambda home: home.async_update_device("VA0000000101"),
        lambda home: home.async_set_flow_temperature(),
    ],
)
async def test_empty_patches_are_refused(
    mock_tado: TadoCloudMock, home: TadoXHome, command: Command
) -> None:
    """A PATCH without any setting is a programming error and sends nothing."""
    with pytest.raises(ValueError):
        await command(home)
    assert mock_tado.calls() == []


def _monday(*spans: tuple[int, int, float]) -> list[ScheduleBlock]:
    return [
        ScheduleBlock(
            day=DayType.MONDAY,
            start_minute=start,
            end_minute=end,
            setting=Setting(power=Power.ON, temperature=temperature),
        )
        for start, end, temperature in spans
    ]


async def test_set_day_schedule(mock_tado: TadoCloudMock, home: TadoXHome) -> None:
    """One POST per weekday with the blocks in time order."""
    blocks = _monday((390, 1440, 21.0), (0, 390, 18.0))
    await home.async_set_day_schedule(1, DayType.MONDAY, blocks)
    assert _only_call(mock_tado, "POST") == (
        f"{HOPS_B}/rooms/1/schedule",
        {
            "dayType": "MONDAY",
            "daySchedule": [
                {
                    "dayType": "MONDAY",
                    "start": "00:00",
                    "end": "06:30",
                    "setting": {"power": "ON", "temperature": {"value": 18.0}},
                },
                {
                    "dayType": "MONDAY",
                    "start": "06:30",
                    "end": "24:00",
                    "setting": {"power": "ON", "temperature": {"value": 21.0}},
                },
            ],
        },
    )


@pytest.mark.parametrize(
    ("day", "blocks", "reason"),
    [
        (
            DayType.MONDAY,
            _monday((0, 600, 20.0), (660, 1440, 20.0)),
            "must start at 10:00",
        ),
        (DayType.TUESDAY, _monday((0, 1440, 20.0)), "every block must be on tuesday"),
    ],
)
async def test_set_day_schedule_validates_first(
    mock_tado: TadoCloudMock,
    home: TadoXHome,
    day: DayType,
    blocks: list[ScheduleBlock],
    reason: str,
) -> None:
    """An invalid day is refused before any request."""
    with pytest.raises(ValueError, match=reason):
        await home.async_set_day_schedule(1, day, blocks)
    assert mock_tado.calls() == []


async def test_holiday_mode_read_modify_write(
    mock_tado: TadoCloudMock, home: TadoXHome
) -> None:
    """The PATCH keeps every key of the read and sends UTC timestamps."""
    server = mock_tado.home(HOME_B)
    server.holiday_mode = {
        **server.holiday_mode,
        "roomSettings": [{"roomId": 1, "temperature": 12.0}],
        "futureKey": {"kept": True},
    }
    start = datetime(2026, 12, 24, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    end = datetime(2027, 1, 2, 18, 0)

    await home.async_set_holiday_mode(enabled=True, start=start, end=end)

    assert _only_call(mock_tado, "PATCH") == (
        f"{HOPS_B}/settings/holidayMode",
        {
            "enabled": True,
            "active": False,
            "startDate": "2026-12-24T07:00:00Z",
            "endDate": "2027-01-02T18:00:00Z",
            "roomSettings": [{"roomId": 1, "temperature": 12.0}],
            "hotWaterSettings": None,
            "futureKey": {"kept": True},
        },
    )


async def test_holiday_mode_keeps_dates_when_not_given(
    mock_tado: TadoCloudMock, home: TadoXHome
) -> None:
    """Disabling without dates sends the stored dates back unchanged."""
    server = mock_tado.home(HOME_B)
    server.holiday_mode = {**server.holiday_mode, "startDate": "2026-12-24T07:00:00Z"}
    await home.async_set_holiday_mode(enabled=False, start=None, end=None)
    _, body = _only_call(mock_tado, "PATCH")
    assert (body["enabled"], body["startDate"], body["endDate"]) == (
        False,
        "2026-12-24T07:00:00Z",
        None,
    )


async def test_holiday_mode_failed_read_aborts(
    mock_tado: TadoCloudMock, home: TadoXHome
) -> None:
    """Without the current state nothing is written."""
    mock_tado.fail("GET", "/settings/holidayMode", status=503)
    with pytest.raises(TadoXServerError):
        await home.async_set_holiday_mode(enabled=True, start=None, end=None)
    assert mock_tado.calls("PATCH") == []


@pytest.mark.parametrize(
    ("tariff", "cents"), [(0.29, 29), (0.285, 29), (0.284, 28), (1.005, 101), (0, 0)]
)
def test_tariff_cents(tariff: float, cents: int) -> None:
    """Tariffs round half up to whole cents."""
    assert tariff_cents(tariff) == cents


async def test_account_and_home_info(
    mock_tado: TadoCloudMock, client: TadoXClient, observed: Observed
) -> None:
    """/me lists the homes; home details are cached under home_info."""
    account = await client.async_get_account()
    info = await client.async_get_home_info(HOME_B)

    assert account.user_id == mock_tado.user_id
    assert [home.id for home in account.homes] == [HOME_A, HOME_B]
    assert (info.id, info.is_tado_x, info.time_zone) == (HOME_B, True, "Europe/Berlin")
    assert [(home_id, key) for home_id, key, _, _ in observed] == [
        (HOME_B, "home_info")
    ]


async def test_home_info_cache_leaves_out_personal_data(
    mock_tado: TadoCloudMock, client: TadoXClient, observed: Observed
) -> None:
    """Only the keys HomeInfo reads are cached; contact details and address are not."""
    info = await client.async_get_home_info(HOME_B)

    [(_, _, raw, _)] = observed
    assert set(raw) == {
        "id",
        "name",
        "generation",
        "dateTimeZone",
        "temperatureUnit",
        "supportsFlowTemperatureOptimization",
        "isAirComfortEligible",
        "isEnergyIqEligible",
    }
    assert HomeInfo.from_api(raw) == info


@pytest.mark.parametrize(
    ("read", "path"),
    [
        (lambda client: client.home(HOME_B).async_get_rooms(), "/homes/1000002/rooms"),
        (
            lambda client: client.home(HOME_B).async_get_topology(),
            "/homes/1000002/roomsAndDevices",
        ),
        (
            lambda client: client.home(HOME_B).async_get_home_state(),
            "/api/v2/homes/1000002/state",
        ),
        (lambda client: client.async_get_home_info(HOME_B), "/api/v2/homes/1000002"),
    ],
    ids=["rooms", "topology", "home_state", "home_info"],
)
async def test_unparsable_payload_is_not_cached(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    observed: Observed,
    read: Callable[[TadoXClient], Awaitable[object]],
    path: str,
) -> None:
    """A 2xx the parser refuses never reaches the cache observer."""
    mock_tado.fail("GET", path, status=200, body={"message": "try again later"})

    with pytest.raises(TadoXParseError):
        await read(client)

    assert observed == []


async def test_list_homes(mock_tado: TadoCloudMock, client: TadoXClient) -> None:
    """Listing homes costs /me plus one call per home, or one per home with an account."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    homes = await client.async_list_homes()
    assert [(home.id, home.generation) for home in homes] == [
        (HOME_A, HomeGeneration.PRE_LINE_X),
        (HOME_B, HomeGeneration.LINE_X),
        (HOME_C, HomeGeneration.LINE_X),
    ]
    assert len(mock_tado.calls()) == 4

    mock_tado.clear_calls()
    account = await client.async_get_account()
    mock_tado.clear_calls()
    assert await client.async_list_homes(account) == homes
    assert [call.path for call in mock_tado.calls()] == [
        f"/api/v2/homes/{HOME_A}",
        f"/api/v2/homes/{HOME_B}",
        f"/api/v2/homes/{HOME_C}",
    ]


async def test_list_homes_stops_at_the_first_failure(
    mock_tado: TadoCloudMock, client: TadoXClient
) -> None:
    """A 429 on one home stops the listing; the gate keeps the rest unsent."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    mock_tado.fail("GET", f"/api/v2/homes/{HOME_A}", status=429)
    with pytest.raises(TadoXRateLimitError):
        await client.async_list_homes()
    assert len(mock_tado.calls()) == 2


async def test_homes_are_isolated(
    mock_tado: TadoCloudMock, client: TadoXClient
) -> None:
    """Two homes with the same room ids reach their own URLs (#92)."""
    await client.home(HOME_B).async_resume_schedule(1)
    await client.home(HOME_C).async_resume_schedule(1)
    assert [call.path for call in mock_tado.calls("DELETE")] == [
        f"/homes/{HOME_B}/rooms/1/manualControl",
        f"/homes/{HOME_C}/rooms/1/manualControl",
    ]
    assert client.home(HOME_C).home_id == HOME_C


async def test_non_x_home(mock_tado: TadoCloudMock, client: TadoXClient) -> None:
    """Hops refuses a home that is no tado X home (#40)."""
    with pytest.raises(TadoXHomeNotXError) as err:
        await client.home(HOME_A).async_get_rooms()
    assert err.value.home_id == HOME_A


async def test_close(
    mock_tado: TadoCloudMock, client: TadoXClient, freezer: FrozenDateTimeFactory
) -> None:
    """A closed client refuses requests that need a token refresh."""
    await client.async_close()
    freezer.tick(timedelta(minutes=10))
    with pytest.raises(TadoXConnectionError):
        await client.async_get_account()
    assert mock_tado.calls() == []


async def test_running_times_use_the_given_day(
    mock_tado: TadoCloudMock, home: TadoXHome, freezer: FrozenDateTimeFactory
) -> None:
    """The caller decides which day is today in the home's time zone."""
    freezer.move_to(datetime(2026, 10, 30, 23, 30, tzinfo=UTC))
    running = await home.async_get_running_times(date(2026, 10, 31))
    assert running.day == date(2026, 10, 31)
    [call] = mock_tado.calls("GET")
    assert dict(call.url.query) == {"from": "2026-10-31", "to": "2026-10-31"}


def _client_log(
    caplog: pytest.LogCaptureFixture, minimum: int = logging.DEBUG
) -> list[tuple[int, str]]:
    """Return level and message of the client's records from minimum up."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == CLIENT_LOGGER and record.levelno >= minimum
    ]


async def test_client_owns_one_telemetry(
    mock_tado: TadoCloudMock, session: ClientSession, client: TadoXClient
) -> None:
    """The client records into its own telemetry, or into the one it was given."""
    await client.home(HOME_B).async_get_home_state()
    assert [e.endpoint for e in client.telemetry.exchanges()] == [
        "GET my:/homes/{home}/state"
    ]

    telemetry = Telemetry()
    access, refresh = entry_tokens(HOME_B)
    tokens = TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    shared = TadoXClient(
        session,
        TokenManager(session, MemoryTokenStore(tokens), telemetry=telemetry),
        QuotaTracker(telemetry=telemetry),
        telemetry=telemetry,
    )
    assert shared.telemetry is telemetry
    await shared.async_get_account()
    assert [e.endpoint for e in telemetry.exchanges()] == ["GET my:/me"]


async def test_reads_log_nothing_above_debug(
    mock_tado: TadoCloudMock, home: TadoXHome, caplog: pytest.LogCaptureFixture
) -> None:
    """Polls and on-demand reads are debug lines only, none per field or value."""
    caplog.set_level(logging.DEBUG)
    await home.async_get_rooms()
    await home.async_get_topology()
    await home.async_get_schedule(1)
    await home.async_get_day_report(1, DAY)

    log = _client_log(caplog)
    assert len(log) == 4
    assert {level for level, _ in log} == {logging.DEBUG}
    for secret in ("Wohnzimmer", "VA0000000101", str(HOME_B)):
        assert secret not in caplog.text


async def test_failed_device_write_is_audited_without_the_serial(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    home: TadoXHome,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 404 for a device names it by its masked serial, at info level."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TadoXNotFoundError):
        await home.async_identify_device("VA0000009999")

    assert _client_log(caplog, logging.INFO) == [
        (
            logging.INFO,
            "Write failed: identify VA…9999 -> HTTP 404 "
            "code=not.found title=<str len=16>",
        )
    ]
    record = client.telemetry.last_error
    assert record is not None
    assert (record.context, record.endpoint, record.status) == (
        "command identify",
        "POST hops:/homes/{home}/roomsAndDevices/devices/{serial}/identify",
        404,
    )
    assert "VA0000009999" not in caplog.text


async def test_free_text_title_never_shows(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    home: TadoXHome,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tado error title is free text; it is described by its length only."""
    title = "Wohnzimmer Musterstrasse 1 ABCD-EFGH"
    mock_tado.fail(
        "DELETE",
        f"{HOPS_B}/rooms/1/manualControl",
        status=422,
        body={"errors": [{"code": "not.allowed", "title": title}]},
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TadoXRequestError) as err:
        await home.async_resume_schedule(1)

    details = "code=not.allowed title=<str len=36>"
    assert _client_log(caplog, logging.INFO) == [
        (logging.INFO, f"Write failed: resume schedule room 1 -> HTTP 422 {details}")
    ]
    assert str(err.value) == f"tado answered HTTP 422: {details}"
    assert err.value.title == title
    [exchange] = client.telemetry.exchanges()
    assert exchange.error == details
    assert client.telemetry.last_error is not None
    assert client.telemetry.last_error.message == details
    for word in ("Wohnzimmer", "Musterstrasse", "ABCD"):
        assert word not in caplog.text
        assert word not in json.dumps(client.telemetry.as_diagnostics())


async def test_write_refused_locally_is_audited(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    home: TadoXHome,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write the closed quota gate refuses sends nothing and says why."""
    client.quota.observe(None, status=429)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TadoXRateLimitError):
        await home.async_set_presence(PresenceSetting.AWAY)

    assert mock_tado.calls() == []
    assert _client_log(caplog, logging.INFO) == [
        (
            logging.INFO,
            "Write failed: presence AWAY -> TadoXRateLimitError: "
            "rate limited by the local quota gate",
        )
    ]
    record = client.telemetry.last_error
    assert record is not None
    assert (record.kind, record.context, record.endpoint) == (
        "TadoXRateLimitError",
        "command presenceLock",
        "PUT my:/homes/{home}/presenceLock",
    )


async def test_refused_write_without_a_request_is_not_audited(
    mock_tado: TadoCloudMock, home: TadoXHome, caplog: pytest.LogCaptureFixture
) -> None:
    """A write the client refuses before sending is no audited write."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        await home.async_update_device("VA0000000101")
    assert _client_log(caplog) == []

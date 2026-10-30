"""Endpoints of the tado X API: the account client and the home-bound API."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Any

from aiohttp import ClientSession
from yarl import URL

from .auth import TokenManager
from .const import (
    ENERGY_IQ_API_URL,
    HOPS_API_URL,
    MINDER_API_URL,
    MY_API_URL,
    REQUEST_TIMEOUT,
)
from .enums import DayType, PresenceSetting, QuickAction, TariffUnit
from .http import Transport
from .models import (
    Account,
    AirComfort,
    DayReport,
    FlowTemperature,
    HolidayMode,
    HomeInfo,
    HomeState,
    MobileDevices,
    RoomSchedule,
    RoomState,
    RunningTimes,
    ScheduleBlock,
    Setting,
    Termination,
    Topology,
    Weather,
    utcnow,
)
from .ratelimit import Priority, QuotaTracker
from .schedule import validate_day


def tariff_cents(tariff: float) -> int:
    """Convert a tariff in currency units to whole cents, rounding half up.

    round() would round half to even, so 0.285 would become 28 cents.
    """
    cents = (Decimal(str(tariff)) * 100).quantize(Decimal(1), ROUND_HALF_UP)
    return int(cents)


def _utc_timestamp(value: datetime) -> str:
    """Format a datetime as UTC ISO 8601; a naive value is taken as UTC."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TadoXHome:
    """API of one home; every call is bound to its immutable home id."""

    def __init__(self, transport: Transport, home_id: int) -> None:
        """Initialize for one home."""
        self._transport = transport
        self._home_id = home_id
        self._my = URL(MY_API_URL) / "homes" / str(home_id)
        self._hops = URL(HOPS_API_URL) / "homes" / str(home_id)
        self._minder = URL(MINDER_API_URL) / "homes" / str(home_id)
        self._energy_iq = URL(ENERGY_IQ_API_URL) / "homes" / str(home_id)

    @property
    def home_id(self) -> int:
        """Return the id of the home every call goes to."""
        return self._home_id

    async def _poll[T](
        self,
        url: URL,
        key: str,
        parse: Callable[[Any], T],
        params: Mapping[str, str] | None = None,
        *,
        cached: bool = True,
    ) -> T:
        """Read and parse a polled endpoint; only a parsed payload reaches the cache."""
        raw = await self._transport.request("GET", url, params=params, endpoint=key)
        value = parse(raw)
        if cached:
            self._transport.publish(self._home_id, key, raw)
        return value

    async def _read(self, url: URL, params: Mapping[str, str] | None = None) -> Any:
        """Read on demand, with command priority at the quota gate."""
        return await self._transport.request(
            "GET", url, params=params, priority=Priority.COMMAND
        )

    async def _command(self, method: str, url: URL, body: Any = None) -> None:
        await self._transport.request(method, url, json=body, priority=Priority.COMMAND)

    def _room(self, room_id: int) -> URL:
        return self._hops / "rooms" / str(room_id)

    async def async_get_rooms(self) -> dict[int, RoomState]:
        """Poll the live state of every room (hops rooms)."""
        return await self._poll(self._hops / "rooms", "rooms", RoomState.list_from_api)

    async def async_get_topology(self) -> Topology:
        """Poll rooms and devices (hops roomsAndDevices)."""
        return await self._poll(
            self._hops / "roomsAndDevices", "topology", Topology.from_api
        )

    async def async_get_home_state(self) -> HomeState:
        """Poll the presence of the home (my state)."""
        return await self._poll(self._my / "state", "home_state", HomeState.from_api)

    async def async_get_weather(self) -> Weather:
        """Poll the weather at the home (my weather)."""
        return await self._poll(self._my / "weather", "weather", Weather.from_api)

    async def async_get_air_comfort(self) -> AirComfort:
        """Poll the air comfort of the rooms (hops airComfort)."""
        return await self._poll(
            self._hops / "airComfort", "air_comfort", AirComfort.from_api
        )

    async def async_get_mobile_devices(self) -> MobileDevices:
        """Poll the phones of the home (my mobileDevices); never cached."""
        return await self._poll(
            self._my / "mobileDevices",
            "mobile_devices",
            MobileDevices.from_api,
            cached=False,
        )

    async def async_get_running_times(self, day: date) -> RunningTimes:
        """Poll the heating running times of one day (minder runningTimes)."""
        return await self._poll(
            self._minder / "runningTimes",
            "running_times",
            partial(RunningTimes.from_api, day=day),
            {"from": day.isoformat(), "to": day.isoformat()},
        )

    async def async_get_flow_temperature(self) -> FlowTemperature:
        """Poll the flow temperature settings; 404 raises TadoXNotFoundError."""
        return await self._poll(
            self._hops / "settings" / "flowTemperatureOptimization",
            "flow_temperature",
            FlowTemperature.from_api,
        )

    async def async_get_holiday_mode(self) -> HolidayMode:
        """Poll the holiday mode (hops settings/holidayMode)."""
        return await self._poll(
            self._hops / "settings" / "holidayMode",
            "holiday_mode",
            HolidayMode.from_api,
        )

    async def async_get_room(self, room_id: int) -> RoomState:
        """Read the live state of one room."""
        return RoomState.from_api(await self._read(self._room(room_id)))

    async def async_get_schedule(self, room_id: int) -> RoomSchedule:
        """Read the weekly schedule of a room."""
        raw = await self._read(self._room(room_id) / "schedule")
        return RoomSchedule.from_api(raw, room_id=room_id)

    async def async_get_day_report(self, room_id: int, day: date) -> DayReport:
        """Read the historic report of a room for one day (my dayReport)."""
        raw = await self._read(
            self._my / "zones" / str(room_id) / "dayReport",
            {"date": day.isoformat()},
        )
        return DayReport.from_api(raw, day=day)

    async def async_read_holiday_mode(self) -> HolidayMode:
        """Read the holiday mode on demand, with command priority."""
        return HolidayMode.from_api(
            await self._read(self._hops / "settings" / "holidayMode")
        )

    async def async_set_manual_control(
        self, room_id: int, setting: Setting, termination: Termination
    ) -> None:
        """Start a manual control of a room with an explicit termination."""
        await self._command(
            "POST",
            self._room(room_id) / "manualControl",
            {"setting": setting.to_api(), "termination": termination.to_api()},
        )

    async def async_resume_schedule(self, room_id: int) -> None:
        """End the manual control of a room (DELETE rooms/{id}/manualControl)."""
        await self._command("DELETE", self._room(room_id) / "manualControl")

    async def async_set_open_window(self, room_id: int, *, active: bool) -> None:
        """Activate (POST) or end (DELETE) open-window mode of a room."""
        method = "POST" if active else "DELETE"
        await self._command(method, self._room(room_id) / "openWindow")

    async def async_quick_action(self, action: QuickAction) -> None:
        """Run a home-wide quick action."""
        await self._command("POST", self._hops / "quickActions" / action.value)

    async def async_set_presence(self, setting: PresenceSetting) -> None:
        """Lock the presence (PUT presenceLock), or return to AUTO (DELETE)."""
        url = self._my / "presenceLock"
        if setting is PresenceSetting.AUTO:
            await self._command("DELETE", url)
        else:
            await self._command("PUT", url, {"homePresence": setting.value})

    async def async_update_device(
        self,
        serial: str,
        *,
        temperature_offset: float | None = None,
        child_lock: bool | None = None,
    ) -> None:
        """Change device settings with one PATCH that carries the given keys only."""
        body: dict[str, Any] = {}
        if temperature_offset is not None:
            body["temperatureOffset"] = temperature_offset
        if child_lock is not None:
            body["childLockEnabled"] = child_lock
        if not body:
            raise ValueError("no device setting to change")
        await self._command(
            "PATCH", self._hops / "roomsAndDevices" / "devices" / serial, body
        )

    async def async_identify_device(self, serial: str) -> None:
        """Make a device flash its display."""
        await self._command(
            "POST", self._hops / "roomsAndDevices" / "devices" / serial / "identify"
        )

    async def async_set_room_termination_default(
        self, room_id: int, termination: Termination
    ) -> None:
        """Write the app's manual control default of a room (PATCH roomsAndDevices/rooms)."""
        await self._command(
            "PATCH",
            self._hops / "roomsAndDevices" / "rooms" / str(room_id),
            {"deviceManualControlTermination": termination.to_api()},
        )

    async def async_set_day_schedule(
        self, room_id: int, day: DayType, blocks: Sequence[ScheduleBlock]
    ) -> None:
        """Validate and replace the schedule of one weekday with one POST."""
        if any(block.day is not day for block in blocks):
            raise ValueError(f"every block must be on {day.value.lower()}")
        ordered = validate_day(blocks)
        await self._command(
            "POST",
            self._room(room_id) / "schedule",
            {
                "dayType": day.value,
                "daySchedule": [block.to_api() for block in ordered],
            },
        )

    async def async_set_holiday_mode(
        self, *, enabled: bool, start: datetime | None, end: datetime | None
    ) -> None:
        """Read, merge and PATCH the holiday mode; a failed read aborts.

        Keys this client does not know, such as room and hot water settings,
        are sent back unchanged.
        """
        current = await self.async_read_holiday_mode()
        body = {**current.raw, "enabled": enabled}
        if start is not None:
            body["startDate"] = _utc_timestamp(start)
        if end is not None:
            body["endDate"] = _utc_timestamp(end)
        await self._command("PATCH", self._hops / "settings" / "holidayMode", body)

    async def async_set_flow_temperature(
        self, *, maximum: int | None = None, auto_adaptation: bool | None = None
    ) -> None:
        """Change the flow temperature settings."""
        body: dict[str, Any] = {}
        if maximum is not None:
            body["maxFlowTemperature"] = maximum
        if auto_adaptation is not None:
            body["autoAdaptation"] = {"enabled": auto_adaptation}
        if not body:
            raise ValueError("no flow temperature setting to change")
        await self._command(
            "PATCH", self._hops / "settings" / "flowTemperatureOptimization", body
        )

    async def async_add_meter_reading(self, reading: int, day: date) -> None:
        """Add an Energy IQ meter reading."""
        await self._command(
            "POST",
            self._energy_iq / "meterReadings",
            {"date": day.isoformat(), "reading": reading},
        )

    async def async_add_tariff(
        self, *, cents: int, unit: TariffUnit, start: date, end: date | None
    ) -> None:
        """Add an Energy IQ tariff."""
        body: dict[str, Any] = {
            "tariffInCents": cents,
            "unit": unit.value,
            "startDate": start.isoformat(),
        }
        if end is not None:
            body["endDate"] = end.isoformat()
        await self._command("POST", self._energy_iq / "tariffs", body)


class TadoXClient:
    """Account-level client: transport, tokens and quota of one grant."""

    def __init__(
        self,
        session: ClientSession,
        tokens: TokenManager,
        quota: QuotaTracker,
        *,
        clock: Callable[[], datetime] = utcnow,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Initialize with the shared session, the grant and the quota tracker."""
        self._tokens = tokens
        self._quota = quota
        self._transport = Transport(
            session, tokens, quota, clock=clock, timeout=timeout
        )

    @property
    def quota(self) -> QuotaTracker:
        """Return the quota tracker of this grant."""
        return self._quota

    def set_payload_observer(
        self, cb: Callable[[int, str, Any, datetime], None] | None
    ) -> None:
        """Set the callback that receives the raw payloads of cached reads."""
        self._transport.set_payload_observer(cb)

    async def async_get_account(self) -> Account:
        """Read the account (my /me)."""
        raw = await self._transport.request(
            "GET", URL(MY_API_URL) / "me", priority=Priority.COMMAND
        )
        return Account.from_api(raw)

    async def async_get_home_info(self, home_id: int) -> HomeInfo:
        """Read the details of a home (my homes/{id})."""
        raw = await self._transport.request(
            "GET", URL(MY_API_URL) / "homes" / str(home_id), endpoint="home_info"
        )
        info = HomeInfo.from_api(raw)
        self._transport.publish(home_id, "home_info", HomeInfo.cache_payload(raw))
        return info

    async def async_list_homes(self, account: Account | None = None) -> list[HomeInfo]:
        """Read the details of every home of the account, one call per home.

        /me is read first unless the caller already has the account.
        """
        if account is None:
            account = await self.async_get_account()
        return [await self.async_get_home_info(home.id) for home in account.homes]

    def home(self, home_id: int) -> TadoXHome:
        """Return the API bound to one home."""
        return TadoXHome(self._transport, home_id)

    async def async_close(self) -> None:
        """Close the grant; waits for an in-flight token refresh."""
        await self._tokens.async_close()

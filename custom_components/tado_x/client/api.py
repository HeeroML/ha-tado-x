"""Endpoints of the tado X API: the account client and the home-bound API."""

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from aiohttp import ClientSession

from .auth import TokenManager
from .const import REQUEST_TIMEOUT
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
    MobileDevice,
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
from .ratelimit import QuotaTracker


class TadoXHome:
    """API of one home; every call is bound to its immutable home id."""

    home_id: int

    def __init__(self, transport: Transport, home_id: int) -> None:
        """Initialize for one home."""
        raise NotImplementedError

    async def async_get_rooms(self) -> dict[int, RoomState]:
        """Poll the live state of every room (hops rooms)."""
        raise NotImplementedError

    async def async_get_topology(self) -> Topology:
        """Poll rooms and devices (hops roomsAndDevices)."""
        raise NotImplementedError

    async def async_get_home_state(self) -> HomeState:
        """Poll the presence of the home (my state)."""
        raise NotImplementedError

    async def async_get_weather(self) -> Weather:
        """Poll the weather at the home (my weather)."""
        raise NotImplementedError

    async def async_get_air_comfort(self) -> AirComfort:
        """Poll the air comfort of the rooms (hops airComfort)."""
        raise NotImplementedError

    async def async_get_mobile_devices(self) -> dict[int, MobileDevice]:
        """Poll the phones of the home (my mobileDevices); never cached."""
        raise NotImplementedError

    async def async_get_running_times(self, day: date) -> RunningTimes:
        """Poll the heating running times of one day (minder runningTimes)."""
        raise NotImplementedError

    async def async_get_flow_temperature(self) -> FlowTemperature:
        """Poll the flow temperature settings; 404 raises TadoXNotFoundError."""
        raise NotImplementedError

    async def async_get_holiday_mode(self) -> HolidayMode:
        """Poll the holiday mode (hops settings/holidayMode)."""
        raise NotImplementedError

    async def async_get_room(self, room_id: int) -> RoomState:
        """Read the live state of one room."""
        raise NotImplementedError

    async def async_get_schedule(self, room_id: int) -> RoomSchedule:
        """Read the weekly schedule of a room."""
        raise NotImplementedError

    async def async_get_day_report(self, room_id: int, day: date) -> DayReport:
        """Read the historic report of a room for one day (my dayReport)."""
        raise NotImplementedError

    async def async_set_manual_control(
        self, room_id: int, setting: Setting, termination: Termination
    ) -> None:
        """Start a manual control of a room with an explicit termination."""
        raise NotImplementedError

    async def async_resume_schedule(self, room_id: int) -> None:
        """End the manual control of a room (DELETE rooms/{id}/manualControl)."""
        raise NotImplementedError

    async def async_set_open_window(self, room_id: int, *, active: bool) -> None:
        """Activate (POST) or end (DELETE) open-window mode of a room."""
        raise NotImplementedError

    async def async_quick_action(self, action: QuickAction) -> None:
        """Run a home-wide quick action."""
        raise NotImplementedError

    async def async_set_presence(self, setting: PresenceSetting) -> None:
        """Lock the presence (PUT presenceLock), or return to AUTO (DELETE)."""
        raise NotImplementedError

    async def async_update_device(
        self,
        serial: str,
        *,
        temperature_offset: float | None = None,
        child_lock: bool | None = None,
    ) -> None:
        """Change device settings with one PATCH that carries the given keys only."""
        raise NotImplementedError

    async def async_identify_device(self, serial: str) -> None:
        """Make a device flash its display."""
        raise NotImplementedError

    async def async_set_room_termination_default(
        self, room_id: int, termination: Termination
    ) -> None:
        """Write the app's manual control default of a room (PATCH roomsAndDevices/rooms)."""
        raise NotImplementedError

    async def async_set_day_schedule(
        self, room_id: int, day: DayType, blocks: Sequence[ScheduleBlock]
    ) -> None:
        """Validate and replace the schedule of one weekday with one POST."""
        raise NotImplementedError

    async def async_set_holiday_mode(
        self, *, enabled: bool, start: datetime | None, end: datetime | None
    ) -> None:
        """Read, merge and PATCH the holiday mode; a failed read aborts."""
        raise NotImplementedError

    async def async_set_flow_temperature(
        self, *, maximum: int | None = None, auto_adaptation: bool | None = None
    ) -> None:
        """Change the flow temperature settings."""
        raise NotImplementedError

    async def async_add_meter_reading(self, reading: int, day: date) -> None:
        """Add an Energy IQ meter reading."""
        raise NotImplementedError

    async def async_add_tariff(
        self, *, cents: int, unit: TariffUnit, start: date, end: date | None
    ) -> None:
        """Add an Energy IQ tariff."""
        raise NotImplementedError


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
        raise NotImplementedError

    @property
    def quota(self) -> QuotaTracker:
        """Return the quota tracker of this grant."""
        raise NotImplementedError

    def set_payload_observer(
        self, cb: Callable[[int, str, Any, datetime], None] | None
    ) -> None:
        """Set the callback that receives the raw payloads of cached reads."""
        raise NotImplementedError

    async def async_get_account(self) -> Account:
        """Read the account (my /me)."""
        raise NotImplementedError

    async def async_get_home_info(self, home_id: int) -> HomeInfo:
        """Read the details of a home (my homes/{id})."""
        raise NotImplementedError

    async def async_list_homes(self) -> list[HomeInfo]:
        """Read /me and the details of every home of the account."""
        raise NotImplementedError

    def home(self, home_id: int) -> TadoXHome:
        """Return the API bound to one home."""
        raise NotImplementedError

    async def async_close(self) -> None:
        """Close the grant; waits for an in-flight token refresh."""
        raise NotImplementedError

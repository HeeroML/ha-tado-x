"""Quota monitoring: polling plan, throttle, API status and quota issues."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .budget import (
    ENDPOINT_RATES,
    FLOW_REPROBE,
    OPTIONAL_ENDPOINTS,
    PlanMode,
    PollPlan,
    coordinator_interval,
    endpoints_due,
    plan_polling,
    throttle_factor,
)
from .cache import SnapshotCache
from .client import HomeInfo, QuotaTracker, RateLimitInfo
from .const import (
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    OPT_AIR_COMFORT,
    OPT_DAILY_CALL_BUDGET,
    OPT_HOLIDAY_MODE,
    OPT_MOBILE_DEVICES,
    OPT_QUOTA_RESERVE,
    OPT_RUNNING_TIMES,
    OPT_WEATHER,
)
from .issues import async_sync_quota_issues
from .runtime import DevicesSnapshot, FeatureStatus, TadoXConfigEntry, entry_label

if TYPE_CHECKING:
    from .coordinator import TadoXCoordinator


FEATURE_OPTIONS: Final[Mapping[str, str]] = {
    "weather": OPT_WEATHER,
    "air_comfort": OPT_AIR_COMFORT,
    "mobile_devices": OPT_MOBILE_DEVICES,
    "running_times": OPT_RUNNING_TIMES,
    "holiday_mode": OPT_HOLIDAY_MODE,
}
THROTTLE_RECHECK: Final = timedelta(minutes=1)


class ApiStatus(StrEnum):
    """State of the api_status sensor."""

    OK = "ok"
    THROTTLED = "throttled"
    CONSERVING = "conserving"
    RATE_LIMITED = "rate_limited"


class QuotaMonitor:
    """Plan and throttle the coordinators of one entry from the shared quota.

    Each change of the plan mode and each start and end of a quota hold is
    logged once: a used-up quota as a warning, because nothing is sent until
    the reset, everything else at info level.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TadoXConfigEntry,
        quota: QuotaTracker,
        cache: SnapshotCache,
    ) -> None:
        """Initialize from the entry options and the quota tracker."""
        self._hass = hass
        self._entry = entry
        self._label = entry_label(entry)
        self._quota = quota
        self._cache = cache
        options = {**DEFAULT_OPTIONS, **entry.options}
        self._options = options
        self._budget = int(options[OPT_DAILY_CALL_BUDGET])
        self._reserve = int(options[OPT_QUOTA_RESERVE])
        self._home_info: HomeInfo | None = None
        self._flow_status = FeatureStatus.UNKNOWN
        self._coordinators: tuple[TadoXCoordinator[Any], ...] = ()
        self._listeners: list[CALLBACK_TYPE] = []
        self._plan = self._make_plan()
        self._factor = 1.0
        self._mode = PlanMode.OK
        self._checked_at: datetime | None = None
        self._conserving_since: datetime | None = None
        self._holding = False
        self._unsub_reset: CALLBACK_TYPE | None = None
        entry.async_on_unload(quota.add_listener(self._async_on_quota))
        entry.async_on_unload(self._async_unwatch_reset)

    @property
    def status(self) -> ApiStatus:
        """Return the API status."""
        if self._mode is PlanMode.BLOCKED or any(
            coordinator.stale_since is not None for coordinator in self._coordinators
        ):
            return ApiStatus.RATE_LIMITED
        if self._mode is PlanMode.CONSERVING:
            return ApiStatus.CONSERVING
        if self._mode is PlanMode.THROTTLED:
            return ApiStatus.THROTTLED
        return ApiStatus.OK

    @property
    def plan(self) -> PollPlan:
        """Return the current polling plan."""
        return self._plan

    @property
    def info(self) -> RateLimitInfo | None:
        """Return the latest rate limit information."""
        return self._quota.latest

    @property
    def factor(self) -> float:
        """Return the current throttle factor."""
        return self._factor

    @property
    def mode(self) -> PlanMode:
        """Return the current plan mode."""
        return self._mode

    @property
    def blocked_until(self) -> datetime | None:
        """Return until when the quota gate stays closed."""
        return self._quota.blocked_until

    @property
    def reset_time(self) -> datetime | None:
        """Return when tado accepts requests again or refills the quota.

        Known only while the gate is closed or when tado sent t=.
        """
        if (blocked := self._blocked_until(dt_util.utcnow())) is not None:
            return blocked
        return None if self.info is None else self.info.reset_at

    def known_hold_end(self, now: datetime) -> datetime | None:
        """Return when a quota hold ends, if tado sent that time."""
        return self._quota.known_hold_end(now)

    def known_hold_began(self, now: datetime) -> datetime | None:
        """Return when a quota hold with a known end began.

        The quota tracker keeps the start of the hold in its persisted state,
        so the start outlasts a reload or a restart.
        """
        return self._quota.known_hold_began(now)

    def calls_24h(self, now: datetime | None = None) -> int:
        """Return the requests this entry sent in the last 24 hours."""
        return self._quota.calls.total(now or dt_util.utcnow())

    def enabled(self, key: str) -> bool:
        """Return whether an endpoint is polled with the current options and home."""
        if (option := FEATURE_OPTIONS.get(key)) is not None and not self._options.get(
            option
        ):
            return False
        home_info = self._home_info
        if key == "air_comfort":
            return home_info is None or home_info.air_comfort_eligible is not False
        if key == "flow_temperature":
            return home_info is None or home_info.supports_flow_temperature is not False
        return key in ENDPOINT_RATES

    def endpoints(self, coordinator: TadoXCoordinator[Any]) -> tuple[str, ...]:
        """Return the enabled endpoints of a coordinator."""
        return tuple(key for key in coordinator.ENDPOINTS if self.enabled(key))

    def due(self, coordinator: TadoXCoordinator[Any], now: datetime) -> frozenset[str]:
        """Return the endpoints of a coordinator that are due now."""
        return endpoints_due(
            plan=self._plan,
            keys=self.endpoints(coordinator),
            optional=OPTIONAL_ENDPOINTS,
            last=coordinator.last_fetched,
            now=now,
            factor=self._factor,
            mode=self._mode,
        )

    def interval(self, coordinator: TadoXCoordinator[Any]) -> timedelta:
        """Return the update interval of a coordinator: its shortest active period."""
        return coordinator_interval(
            plan=self._plan,
            keys=self.endpoints(coordinator),
            optional=OPTIONAL_ENDPOINTS,
            factor=self._factor,
            mode=self._mode,
        )

    @callback
    def async_note_devices(self, snapshot: DevicesSnapshot) -> None:
        """Take the home details that decide which endpoints exist."""
        self._home_info = snapshot.home_info or self._home_info
        self._flow_status = snapshot.flow_status
        self._cache.set_flow_status(snapshot.flow_status)

    @callback
    def async_start(self, coordinators: Iterable[TadoXCoordinator[Any]]) -> None:
        """Plan, apply the intervals and start listening to the quota tracker."""
        self._coordinators = tuple(coordinators)
        self._async_update(dt_util.utcnow(), force=True)

    @callback
    def async_on_poll(self, coordinator: TadoXCoordinator[Any]) -> None:
        """Re-plan, update the status listeners and the quota issues after a poll."""
        self._async_update(dt_util.utcnow(), force=True)

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for status changes; return a function that removes the listener."""
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    @callback
    def _async_on_quota(self) -> None:
        """Handle a response observed by the quota tracker."""
        self._async_update(dt_util.utcnow(), force=False)

    @callback
    def _async_update(self, now: datetime, *, force: bool) -> None:
        self._cache.set_quota(self._quota.as_dict(), self._quota.calls.dump())
        if (
            force
            or self._checked_at is None
            or now - self._checked_at >= THROTTLE_RECHECK
        ):
            self._async_replan(now)
            async_sync_quota_issues(
                self._hass,
                self._entry,
                info=self.info,
                blocked_until=self._quota.blocked_until,
                conserving_since=self._conserving_since,
                reserve=self._reserve,
                now=now,
                rooms_period=self._plan.rooms,
            )
        for listener in list(self._listeners):
            listener()

    @callback
    def _async_replan(self, now: datetime) -> None:
        self._checked_at = now
        self._plan = self._make_plan()
        blocked = self._blocked_until(now) is not None
        self._factor, mode = throttle_factor(
            plan=self._plan,
            info=self.info,
            reserve=self._reserve,
            entries_on_account=self._entries_on_account(),
            blocked=blocked,
            now=now,
        )
        self._log_transition(mode)
        self._log_hold(self._hold_end(now), mode)
        self._mode = mode
        if mode is PlanMode.CONSERVING:
            self._conserving_since = self._conserving_since or now
        else:
            self._conserving_since = None
        for coordinator in self._coordinators:
            coordinator.async_set_interval(self.interval(coordinator))
        self._async_watch_reset(now)

    @callback
    def _async_watch_reset(self, now: datetime) -> None:
        """Re-plan at the reset tado sent while it slows the polling down."""
        self._async_unwatch_reset()
        info = self.info
        if (
            self._mode in (PlanMode.THROTTLED, PlanMode.CONSERVING)
            and info is not None
            and (reset := info.reset_at) is not None
            and reset > now
        ):
            self._unsub_reset = async_track_point_in_utc_time(
                self._hass, self._async_on_reset, reset
            )

    @callback
    def _async_unwatch_reset(self) -> None:
        if self._unsub_reset is not None:
            self._unsub_reset()
            self._unsub_reset = None

    @callback
    def _async_on_reset(self, now: datetime) -> None:
        self._unsub_reset = None
        self._async_update(now, force=True)

    def _log_transition(self, mode: PlanMode) -> None:
        """Log a change of the plan mode; the end of a block is a hold's end."""
        if mode is self._mode:
            return
        if mode is PlanMode.BLOCKED:
            LOGGER.warning(
                "The tado API quota of %s is used up; polling pauses until %s",
                self._label,
                self._quota.blocked_until,
            )
        elif mode is PlanMode.CONSERVING:
            LOGGER.info(
                "Only %s tado API calls are left for %s; polling slows down until the quota resets",
                None if self.info is None else self.info.remaining,
                self._label,
            )
        elif self._mode is PlanMode.CONSERVING:
            LOGGER.info("Polling of %s is back to its plan", self._label)

    def _hold_end(self, now: datetime) -> datetime | None:
        """Return until when the quota gate refuses polls, if it does."""
        return self._quota.known_hold_end(now) or self._blocked_until(now)

    def _log_hold(self, end: datetime | None, mode: PlanMode) -> None:
        """Log the start and the end of a quota hold.

        A hold that starts with a used-up quota has its warning already.
        """
        holding = end is not None
        if holding is self._holding:
            return
        self._holding = holding
        if end is None:
            LOGGER.info("Polling of %s resumes after the quota hold", self._label)
        elif mode is not PlanMode.BLOCKED:
            LOGGER.info(
                "Polling of %s holds until %s: only %s tado API calls are left",
                self._label,
                end.isoformat(),
                None if self.info is None else self.info.remaining,
            )

    def _blocked_until(self, now: datetime) -> datetime | None:
        blocked = self._quota.blocked_until
        return blocked if blocked is not None and now < blocked else None

    def _make_plan(self) -> PollPlan:
        rates = [
            FLOW_REPROBE
            if key == "flow_temperature" and self._flow_status is FeatureStatus.ABSENT
            else rate
            for key, rate in ENDPOINT_RATES.items()
            if self.enabled(key)
        ]
        limit = None if self.info is None else self.info.limit
        return plan_polling(budget=self._budget, quota_limit=limit, endpoints=rates)

    def _entries_on_account(self) -> int:
        """Return the loaded entries that share this entry's tado account."""
        user_id = self._entry.data.get(CONF_USER_ID)
        others = [
            entry
            for entry in self._hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.entry_id != self._entry.entry_id
            and (user_id is None or entry.data.get(CONF_USER_ID) == user_id)
        ]
        return len(others) + 1

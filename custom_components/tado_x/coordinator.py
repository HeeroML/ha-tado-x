"""Coordinators of the tado X integration, one per polling cadence."""

from abc import abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from functools import partial
import logging
from typing import Any, ClassVar, Final, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .cache import CacheSeed
from .client import (
    AirComfort,
    FlowTemperature,
    HolidayMode,
    HomeGeneration,
    HomeInfo,
    HomeState,
    RoomState,
    RunningTimes,
    TadoXAuthError,
    TadoXClient,
    TadoXConnectionError,
    TadoXError,
    TadoXHome,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    Telemetry,
    Topology,
    Weather,
)
from .client.redact import home_label, safe_code
from .const import DOMAIN, LOGGER, MAX_STALE_DEVICES, MAX_STALE_HOME, MAX_STALE_ROOMS
from .issues import async_create_not_tado_x_issue, format_time
from .quota import QuotaMonitor
from .runtime import (
    DevicesSnapshot,
    FeatureStatus,
    HomeSnapshot,
    ReauthNotice,
    RoomsSnapshot,
    TadoXConfigEntry,
)

REFRESH_COOLDOWN: Final = 5.0
_MIN_DELAY: Final = timedelta(seconds=1)

type Fetcher = Callable[[], Awaitable[Any]]


def home_today(home_info: HomeInfo | None, now: datetime | None = None) -> date:
    """Return today's date in the home's time zone, else in Home Assistant's."""
    zone: tzinfo | None = None
    if home_info is not None and home_info.time_zone:
        zone = dt_util.get_time_zone(home_info.time_zone)
    return (
        (now or dt_util.utcnow())
        .astimezone(zone or dt_util.get_default_time_zone())
        .date()
    )


def rejected_placeholders(err: TadoXRequestError) -> dict[str, str]:
    """Return the placeholders of request_rejected: the status and tado's error code.

    tado's error title can name a home, a room or a person, so a message
    never shows it.
    """
    return {
        "status": str(err.status),
        "code": "-" if err.code is None else safe_code(err.code),
    }


def unexpected_placeholders(err: TadoXError) -> dict[str, str]:
    """Return the placeholder of api_error: the endpoint template of the request.

    A payload that fails validation after the request names the endpoint key
    of its parser. The message of the error and the JSON path are left out.
    """
    if err.request is not None:
        return {"error": err.request}
    if isinstance(err, TadoXParseError):
        return {"error": err.endpoint}
    return {"error": type(err).__name__}


def _update_failed(err: TadoXError) -> UpdateFailed:
    """Return the translated UpdateFailed for an error that is not a quota hold."""
    if isinstance(err, TadoXConnectionError):
        return UpdateFailed(translation_domain=DOMAIN, translation_key="cannot_connect")
    if isinstance(err, TadoXServerError):
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="server_error",
            translation_placeholders={"status": str(err.status)},
        )
    if isinstance(err, TadoXRequestError):
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="request_rejected",
            translation_placeholders=rejected_placeholders(err),
        )
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="api_error",
        translation_placeholders=unexpected_placeholders(err),
    )


def _quota_failed(
    reset: datetime | None, now: datetime, *, setup: bool
) -> UpdateFailed:
    """Return the UpdateFailed of a quota hold without usable data."""
    if reset is None:
        return UpdateFailed(
            translation_domain=DOMAIN, translation_key="quota_exhausted_unknown_reset"
        )
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="setup_quota_exhausted" if setup else "quota_exhausted",
        translation_placeholders={"reset": format_time(reset)},
        retry_after=max((reset - now).total_seconds(), 1.0),
    )


def _seed_value[T](
    seed: CacheSeed, key: str, parse: Callable[[Any], T]
) -> tuple[datetime, T] | None:
    """Parse one cached payload with the parser of live data; None if unusable."""
    if (item := seed.payloads.get(key)) is None:
        return None
    at, raw = item
    try:
        return at, parse(raw)
    except TadoXError, ValueError, TypeError:
        LOGGER.debug("Ignoring the cached %s payload", key)
        return None


@dataclass(slots=True)
class _Patch[S]:
    apply: Callable[[S], S]
    expires: datetime


def _target_name(target: Hashable) -> str:
    """Return the name of a patch target without its room id or serial."""
    return str(target[0] if isinstance(target, tuple) else target)


class _PendingPatches[S]:
    """Optimistic patches by target, re-applied until the server agrees.

    A target names what a patch writes, such as the setting of one room. A
    patch replaces the pending patch of its target, so a later command wins
    even when it changes less, as a resume does after a manual target.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._patches: dict[Hashable, _Patch[S]] = {}

    def add(self, target: Hashable, patch: Callable[[S], S], expires: datetime) -> None:
        self._patches.pop(target, None)
        self._patches[target] = _Patch(patch, expires)

    def apply(self, data: S, now: datetime) -> S:
        """Apply the live patches; drop expired ones and those the data shows."""
        kept: dict[Hashable, _Patch[S]] = {}
        for target, patch in self._patches.items():
            if now >= patch.expires:
                self._log_drop(target, "tado did not confirm it in time")
                continue
            patched = patch.apply(data)
            if patched == data:
                self._log_drop(target, "tado reports it")
                continue
            data = patched
            kept[target] = patch
        self._patches = kept
        return data

    def _log_drop(self, target: Hashable, reason: str) -> None:
        LOGGER.debug(
            "Dropped the optimistic %s change of the %s data: %s",
            _target_name(target),
            self._kind,
            reason,
        )


class TadoXCoordinator[S](DataUpdateCoordinator[S]):
    """Coordinator with per-endpoint periods, stale serving and optimistic patches.

    During a quota hold the last snapshot is served until the reset tado
    sent, or for up to MAX_STALE without one; network, server and parse
    errors make the entities unavailable as usual. stale_since is when the
    hold that the served snapshot waits out began.

    A failed poll is recorded in the telemetry as "poll <kind>". Refusals
    and 429 answers during a quota hold are not: the quota tracker records
    the closing of the gate once. An error the client recorded already, such
    as a failed token refresh, keeps its own context.
    """

    config_entry: TadoXConfigEntry

    KIND: ClassVar[str]
    ENDPOINTS: ClassVar[tuple[str, ...]]
    REQUIRED: ClassVar[frozenset[str]]
    MAX_STALE: ClassVar[timedelta]

    last_fetched: dict[str, datetime]
    last_live_update: datetime | None
    stale_since: datetime | None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TadoXConfigEntry,
        api: TadoXHome,
        monitor: QuotaMonitor,
        seed: CacheSeed | None,
        *,
        telemetry: Telemetry,
        reauth: ReauthNotice,
    ) -> None:
        """Initialize with always_update=False and a 5 s refresh debouncer.

        The name, which Home Assistant logs, holds the home label, never the
        entry title.
        """
        label = home_label(api.home_id)
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.KIND} {label}",
            update_interval=monitor.interval(self),
            always_update=False,
            request_refresh_debouncer=Debouncer(
                hass, LOGGER, cooldown=REFRESH_COOLDOWN, immediate=False
            ),
        )
        self.api = api
        self._label = label
        self._monitor = monitor
        self._telemetry = telemetry
        self._reauth = reauth
        self._context = f"poll {self.KIND}"
        self.last_fetched = {}
        self.last_live_update = None
        self.stale_since = None
        self._pending: _PendingPatches[S] = _PendingPatches(self.KIND)
        self._failing: set[str] = set()
        self._refresh_requested = False
        self._refreshed_at: datetime | None = None
        self._seed_data: S | None = None
        self._seed_at: datetime | None = None
        if seed is not None and (seeded := self._parse_seed(seed)) is not None:
            self._seed_data, self._seed_at = seeded

    @abstractmethod
    def _parse_seed(self, seed: CacheSeed) -> tuple[S, datetime] | None:
        """Build a snapshot from cached payloads, with the time of the required one."""

    @abstractmethod
    async def _async_fetch(
        self, previous: S | None, due: frozenset[str]
    ) -> tuple[S, bool]:
        """Fetch the due endpoints and merge them into the previous snapshot.

        Return the snapshot and whether any endpoint returned a live value.
        """

    @override
    async def async_request_refresh(self) -> None:
        """Request a refresh that reads the required endpoints whatever the plan."""
        self._refresh_requested = True
        await super().async_request_refresh()

    @override
    async def _async_update_data(self) -> S:
        """Fetch the due endpoints and map client errors to coordinator errors.

        The data counts as updated, and a quota hold as over, only when an
        endpoint returned a live value: a tick with nothing due sends nothing.
        """
        now = dt_util.utcnow()
        previous = self.data if self.data is not None else self._seed_data
        due = self._monitor.due(self, now)
        if previous is None or self._refresh_requested:
            due |= self.REQUIRED
        self._refresh_requested = False
        LOGGER.debug(
            "Polling %s of %s: %s",
            self.KIND,
            self._label,
            ", ".join(sorted(due)) or "nothing is due",
        )
        try:
            data, live = await self._async_fetch(previous, due)
        except TadoXRateLimitError as err:
            return self._serve_stale(err, now)
        except TadoXError as err:
            self._telemetry.record_tado_error(err, context=self._context)
            # tado answered, so a quota hold is over even when the answer is an error.
            self._end_stale()
            raise self._translate(err) from err
        self.last_fetched.update(dict.fromkeys(due, now))
        if live:
            self.last_live_update = now
            self._end_stale()
        return self._pending.apply(data, now)

    def _translate(self, err: TadoXError) -> Exception:
        """Return the coordinator error for a client error other than a quota hold."""
        if isinstance(err, TadoXHomeNotXError):
            async_create_not_tado_x_issue(self.hass, self.config_entry)
            return ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="not_tado_x_home",
                translation_placeholders={"home": self.config_entry.title},
            )
        if isinstance(err, TadoXAuthError):
            # Home Assistant starts the reauth flow for this error.
            self._reauth.note(err)
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        return _update_failed(err)

    @override
    @callback
    def _async_refresh_finished(self) -> None:
        """Let the quota monitor re-plan and update the status after every poll."""
        self._refreshed_at = dt_util.utcnow()
        self._monitor.async_on_poll(self)

    @callback
    def async_set_interval(self, interval: timedelta) -> None:
        """Use a new update interval; a shorter one brings the pending refresh forward.

        Home Assistant arms the next refresh only when a refresh ends, so a
        coordinator would otherwise wait out the long interval of an old plan.
        """
        previous = self.update_interval
        self.update_interval = interval
        if (
            previous is None
            or interval >= previous
            or self._refreshed_at is None
            or self._unsub_refresh is None
        ):
            return
        delay = max(self._refreshed_at + interval - dt_util.utcnow(), _MIN_DELAY)
        # _schedule_refresh arms the timer one update_interval from now.
        self.update_interval = delay
        self._schedule_refresh()
        self.update_interval = interval

    @callback
    def async_apply_optimistic(
        self,
        patches: Mapping[Hashable, Callable[[S], S]],
        *,
        ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        """Apply patches by target now; re-apply each until the data agrees or ttl passes.

        A patch replaces the pending patch of the same target. The poll timer
        keeps running: async_set_updated_data would restart it, so writes more
        often than the interval would stop the polling.
        """
        if self.data is None:
            return
        expires = dt_util.utcnow() + ttl
        data = self.data
        for target, patch in patches.items():
            self._pending.add(target, patch, expires)
            data = patch(data)
        LOGGER.debug(
            "Applied the optimistic %s change of the %s data for up to %s s",
            ", ".join(sorted({_target_name(target) for target in patches})),
            self.KIND,
            int(ttl.total_seconds()),
        )
        self.data = data
        self.async_update_listeners()

    def _serve_stale(self, err: TadoXRateLimitError, now: datetime) -> S:
        """Serve the last snapshot during a quota hold, while it is recent enough.

        A coordinator created by a reload or restart during a hold with a
        known end takes the start of that hold from the quota tracker. A
        setup that the restored quota state refuses continues a hold that was
        logged when it began, so serving the cache is logged at debug level;
        every setup retry would repeat the line otherwise.
        """
        setup = self.data is None
        data: S | None = self._seed_data if setup else self.data
        at = self._seed_at if setup else self.last_live_update
        reset = err.reset_at or self._monitor.reset_time
        began = self.stale_since or self._monitor.known_hold_began(now) or now
        if data is None or at is None or not self._servable(at, began, now):
            raise _quota_failed(reset, now, setup=setup) from err
        if self.stale_since is None:
            self.stale_since = began
            LOGGER.log(
                logging.DEBUG
                if setup and err.local and self._monitor.restored_hold
                else logging.INFO,
                "Serving cached %s data of %s from %s during a quota hold",
                self.KIND,
                self._label,
                at.isoformat(),
            )
        self.last_live_update = at
        return data

    def _end_stale(self) -> None:
        """Stop serving the snapshot of a quota hold."""
        if self.stale_since is None:
            return
        self.stale_since = None
        LOGGER.info("Stopped serving cached %s data of %s", self.KIND, self._label)

    def _servable(self, at: datetime, began: datetime, now: datetime) -> bool:
        """Return whether data read at a time may be served during the hold.

        A hold that ends at a reset tado sent keeps serving what was within
        MAX_STALE when the hold began; otherwise MAX_STALE counts from now.
        """
        if now - at <= self.MAX_STALE:
            return True
        return (
            self._monitor.known_hold_end(now) is not None
            and began - at <= self.MAX_STALE
        )

    async def _async_gather(
        self, due: frozenset[str], fetchers: Mapping[str, Fetcher]
    ) -> tuple[dict[str, Any], dict[str, TadoXError]]:
        """Run the due fetchers; return the values and the optional failures.

        The required endpoints go first and the optional ones together after
        them, so a used-up quota costs one request, not one per endpoint.
        A quota error wins over an auth error, which wins over "not a tado X
        home", then over a failed required endpoint. A failed optional
        endpoint does not fail the tick, nor does one the quota gate refused
        after the required endpoints were read.
        """
        keys = [key for key in fetchers if key in due]
        outcome: dict[str, Any] = {}
        for batch in (
            [key for key in keys if key in self.REQUIRED],
            [key for key in keys if key not in self.REQUIRED],
        ):
            results = await asyncio.gather(
                *(fetchers[key]() for key in batch), return_exceptions=True
            )
            outcome.update(zip(batch, results, strict=True))
            self._raise_first(outcome)
        values: dict[str, Any] = {}
        failures: dict[str, TadoXError] = {}
        for key, result in outcome.items():
            if isinstance(result, TadoXError):
                failures[key] = result
                self._note_failure(key, result)
            else:
                values[key] = result
                self._note_success(key)
        return values, failures

    def _raise_first(self, outcome: Mapping[str, Any]) -> None:
        """Raise the error that fails the tick, if any, by precedence."""
        errors = {
            key: result
            for key, result in outcome.items()
            if isinstance(result, BaseException) and not self._refused(key, result)
        }
        for kind in (TadoXRateLimitError, TadoXAuthError, TadoXHomeNotXError):
            for error in errors.values():
                if isinstance(error, kind):
                    raise error
        for key, error in errors.items():
            if key in self.REQUIRED or not isinstance(error, TadoXError):
                raise error

    def _refused(self, key: str, err: BaseException) -> bool:
        """Return True for an optional read the quota gate refused."""
        return (
            key not in self.REQUIRED
            and isinstance(err, TadoXRateLimitError)
            and err.local
        )

    def _expected_failure(self, key: str, err: TadoXError) -> bool:
        """Return True for a failure that is an answer, not a problem."""
        return False

    def _note_failure(self, key: str, err: TadoXError) -> None:
        if self._refused(key, err) or self._expected_failure(key, err):
            return
        self._telemetry.record_tado_error(err, context=self._context)
        if key in self._failing:
            return
        self._failing.add(key)
        LOGGER.warning(
            "Reading %s of %s failed, keeping the last value: %s",
            key,
            self._label,
            err,
        )

    def _note_success(self, key: str) -> None:
        if key in self._failing:
            self._failing.discard(key)
            LOGGER.info("Reading %s of %s works again", key, self._label)


class RoomsCoordinator(TadoXCoordinator[RoomsSnapshot]):
    """Live room state, polled at the planned rooms period."""

    KIND = "rooms"
    ENDPOINTS = ("rooms",)
    REQUIRED = frozenset({"rooms"})
    MAX_STALE = MAX_STALE_ROOMS

    @override
    def _parse_seed(self, seed: CacheSeed) -> tuple[RoomsSnapshot, datetime] | None:
        if (rooms := _seed_value(seed, "rooms", RoomState.list_from_api)) is None:
            return None
        at, parsed = rooms
        return RoomsSnapshot(rooms=parsed), at

    @override
    async def _async_fetch(
        self, previous: RoomsSnapshot | None, due: frozenset[str]
    ) -> tuple[RoomsSnapshot, bool]:
        """Fetch the rooms."""
        values, _ = await self._async_gather(due, {"rooms": self.api.async_get_rooms})
        if previous is None or "rooms" in values:
            return RoomsSnapshot(rooms=values["rooms"]), True
        return previous, False


_HOME_FIELDS: Final = {
    "home_state": "state",
    "weather": "weather",
    "air_comfort": "air_comfort",
}


class HomeCoordinator(TadoXCoordinator[HomeSnapshot]):
    """Presence, weather, air comfort and phones."""

    KIND = "home"
    ENDPOINTS = ("home_state", "weather", "air_comfort", "mobile_devices")
    REQUIRED = frozenset({"home_state"})
    MAX_STALE = MAX_STALE_HOME

    @override
    def _parse_seed(self, seed: CacheSeed) -> tuple[HomeSnapshot, datetime] | None:
        if (state := _seed_value(seed, "home_state", HomeState.from_api)) is None:
            return None
        at, parsed = state
        snapshot = HomeSnapshot(state=parsed)
        if self._monitor.enabled("weather") and (
            weather := _seed_value(seed, "weather", Weather.from_api)
        ):
            snapshot = replace(snapshot, weather=weather[1])
        if self._monitor.enabled("air_comfort") and (
            comfort := _seed_value(seed, "air_comfort", AirComfort.from_api)
        ):
            snapshot = replace(snapshot, air_comfort=comfort[1])
        return snapshot, at

    @override
    async def _async_fetch(
        self, previous: HomeSnapshot | None, due: frozenset[str]
    ) -> tuple[HomeSnapshot, bool]:
        """Fetch the due home endpoints."""
        api = self.api
        values, _ = await self._async_gather(
            due,
            {
                "home_state": api.async_get_home_state,
                "weather": api.async_get_weather,
                "air_comfort": api.async_get_air_comfort,
                "mobile_devices": api.async_get_mobile_devices,
            },
        )
        if previous is None:
            previous = HomeSnapshot(state=values["home_state"])
        changes: dict[str, Any] = {
            field: values[key] for key, field in _HOME_FIELDS.items() if key in values
        }
        if (phones := values.get("mobile_devices")) is not None:
            changes.update(
                mobile_devices=phones.phones, mobile_devices_complete=phones.complete
            )
        return replace(previous, **changes), bool(values)


class DevicesCoordinator(TadoXCoordinator[DevicesSnapshot]):
    """Topology, home details, running times, flow temperature and holiday mode."""

    KIND = "devices"
    ENDPOINTS = (
        "topology",
        "home_info",
        "running_times",
        "flow_temperature",
        "holiday_mode",
    )
    REQUIRED = frozenset({"topology"})
    MAX_STALE = MAX_STALE_DEVICES

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TadoXConfigEntry,
        api: TadoXHome,
        monitor: QuotaMonitor,
        seed: CacheSeed | None,
        *,
        telemetry: Telemetry,
        reauth: ReauthNotice,
        client: TadoXClient,
    ) -> None:
        """Initialize, and tell the monitor what the cached home details say.

        The home details are read through the account client, which serves
        my.tado.com homes/{id}.
        """
        self._client = client
        self._generation_warned = False
        super().__init__(
            hass, entry, api, monitor, seed, telemetry=telemetry, reauth=reauth
        )
        if self._seed_data is not None:
            monitor.async_note_devices(self._seed_data)

    @override
    def _parse_seed(self, seed: CacheSeed) -> tuple[DevicesSnapshot, datetime] | None:
        if (topology := _seed_value(seed, "topology", Topology.from_api)) is None:
            return None
        at, parsed = topology
        info = _seed_value(seed, "home_info", HomeInfo.from_api)
        snapshot = DevicesSnapshot(
            topology=parsed,
            home_info=info[1] if info else None,
            flow_status=seed.flow_status or FeatureStatus.UNKNOWN,
        )
        if self._monitor.enabled("running_times") and (
            cached := seed.payloads.get("running_times")
        ):
            day = home_today(snapshot.home_info, cached[0])
            if times := _seed_value(
                seed, "running_times", partial(RunningTimes.from_api, day=day)
            ):
                snapshot = replace(snapshot, running_times=times[1])
        if snapshot.flow_status is not FeatureStatus.ABSENT and (
            flow := _seed_value(seed, "flow_temperature", FlowTemperature.from_api)
        ):
            snapshot = replace(
                snapshot, flow_temperature=flow[1], flow_status=FeatureStatus.PRESENT
            )
        if self._monitor.enabled("holiday_mode") and (
            holiday := _seed_value(seed, "holiday_mode", HolidayMode.from_api)
        ):
            snapshot = replace(snapshot, holiday_mode=holiday[1])
        return snapshot, at

    @override
    async def _async_fetch(
        self, previous: DevicesSnapshot | None, due: frozenset[str]
    ) -> tuple[DevicesSnapshot, bool]:
        """Fetch the due device endpoints."""
        api = self.api
        today = home_today(previous.home_info if previous else None)
        values, failures = await self._async_gather(
            due,
            {
                "topology": api.async_get_topology,
                "home_info": self._async_get_home_info,
                "running_times": partial(api.async_get_running_times, today),
                "flow_temperature": api.async_get_flow_temperature,
                "holiday_mode": api.async_get_holiday_mode,
            },
        )
        if previous is None:
            previous = DevicesSnapshot(topology=values["topology"])
        # The endpoint keys of this coordinator are the snapshot field names.
        snapshot = replace(previous, **values)
        if "flow_temperature" in values:
            snapshot = replace(snapshot, flow_status=FeatureStatus.PRESENT)
        elif isinstance(failures.get("flow_temperature"), TadoXNotFoundError):
            snapshot = replace(
                snapshot, flow_temperature=None, flow_status=FeatureStatus.ABSENT
            )
        self._monitor.async_note_devices(snapshot)
        return snapshot, bool(values)

    async def _async_get_home_info(self) -> HomeInfo:
        """Read the home details; a home of the previous generation is refused (#40)."""
        home_id = self.api.home_id
        info = await self._client.async_get_home_info(home_id)
        if info.generation is HomeGeneration.PRE_LINE_X:
            raise TadoXHomeNotXError(home_id)
        if info.generation is HomeGeneration.UNKNOWN and not self._generation_warned:
            self._generation_warned = True
            LOGGER.warning(
                "tado reports no known generation for %s; it is used as a tado X "
                "home because the tado X API serves it",
                self._label,
            )
        return info

    @override
    def _expected_failure(self, key: str, err: TadoXError) -> bool:
        return key == "flow_temperature" and isinstance(err, TadoXNotFoundError)

"""Stateful fake of the tado cloud on top of aioclient_mock.

Every request of the integration runs through the real client code; only
HTTP is faked. The fake serves homes 1000001 (V3+, hops answers 404),
1000002 and 1000003 (tado X with identical room ids), rotates refresh tokens
(a reused one answers 400 invalid_grant), checks bearer tokens, and emits
ratelimit headers from an internal counter.
"""

import asyncio
from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl

from multidict import CIMultiDict
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tado_x.client.const import CLIENT_ID
from homeassistant.util import dt as dt_util

FIXTURES = Path(__file__).parent / "fixtures"

USER_ID = "00000000-0000-4000-8000-000000000001"
HOME_A = 1000001
HOME_B = 1000002
HOME_C = 1000003
X_HOMES = (HOME_B, HOME_C)

DEFAULT_QUOTA_LIMIT = 20000
DEFAULT_QUOTA_REMAINING = 17833
DEFAULT_429_HEADERS = {
    "ratelimit-policy": '"perday";q=20000;w=86400',
    "ratelimit": '"perday";r=0;t=7082',
}

type Matcher = str | re.Pattern[str]
type Result = tuple[int, Any]

_TADO_URL = re.compile(r"^https://[a-z-]+\.tado\.com/")
_WEEKDAYS = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)
HOME_NAMES = {HOME_A: "Haus A", HOME_B: "Haus B", HOME_C: "Haus C"}
"""The names tado gives the homes; the config flow makes them the entry titles."""


def load_fixture(name: str) -> Any:
    """Return a parsed fixture of tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def entry_tokens(home_id: int) -> tuple[str, str]:
    """Return the fake access and refresh token stored in a home's test entry."""
    return f"fake-access-token-{home_id}", f"fake-refresh-token-{home_id}"


def _home_fixture(name: str, home_id: int) -> Any:
    """Load a fixture of home 1000002, rewritten for another home."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    if home_id == HOME_C:
        text = re.sub(r"(VA|SU)00000001(\d\d)", r"\g<1>00000002\2", text)
        text = text.replace("IB0000000001", "IB0000000002")
        text = text.replace(str(HOME_B), str(HOME_C))
    payload = json.loads(text)
    if name.startswith("home_") and isinstance(payload, dict):
        payload["id"] = home_id
        payload["name"] = HOME_NAMES[home_id]
    return payload


def _errors(code: str, title: str) -> dict[str, Any]:
    return {"errors": [{"code": code, "title": title}]}


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Call:
    """One request the integration sent."""

    method: str
    url: URL
    body: Any
    headers: Mapping[str, str]

    @property
    def path(self) -> str:
        """Return the URL path."""
        return self.url.path


@dataclass(slots=True)
class FakeHome:
    """Server-side state of one home; tests may change it directly."""

    home_id: int
    tado_x: bool
    info: dict[str, Any]
    state: dict[str, Any]
    weather: dict[str, Any]
    rooms: list[dict[str, Any]]
    topology: dict[str, Any]
    air_comfort: dict[str, Any]
    mobile_devices: list[dict[str, Any]]
    running_times: dict[str, Any]
    holiday_mode: dict[str, Any]
    day_report: dict[str, Any]
    schedules: dict[int, dict[str, Any]]
    flow_temperature: dict[str, Any] | None = None
    meter_readings: list[dict[str, Any]] = field(default_factory=list)
    tariffs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, home_id: int) -> FakeHome:
        """Build a home from the fixtures."""
        info_name = "home_1000001.json" if home_id == HOME_A else "home_1000002.json"
        rooms = _home_fixture("rooms.json", home_id)
        schedule = load_fixture("schedule.json")
        schedules = {}
        for room in rooms:
            room_schedule = copy.deepcopy(schedule)
            room_schedule["room"] = {"id": room["id"], "name": room["name"]}
            schedules[room["id"]] = room_schedule
        return cls(
            home_id=home_id,
            tado_x=home_id != HOME_A,
            info=_home_fixture(info_name, home_id),
            state=_home_fixture("state.json", home_id),
            weather=_home_fixture("weather.json", home_id),
            rooms=rooms,
            topology=_home_fixture("rooms_and_devices.json", home_id),
            air_comfort=_home_fixture("air_comfort.json", home_id),
            mobile_devices=_home_fixture("mobile_devices.json", home_id),
            running_times=_home_fixture("running_times.json", home_id),
            holiday_mode=_home_fixture("holiday_mode.json", home_id),
            day_report=_home_fixture("day_report.json", home_id),
            schedules=schedules,
        )

    def room(self, room_id: int) -> dict[str, Any] | None:
        """Return the live state of a room."""
        return next((room for room in self.rooms if room["id"] == room_id), None)

    def scheduled_setting(self, room_id: int, now: datetime) -> dict[str, Any] | None:
        """Return the setting the schedule of a room gives at a time."""
        if (schedule := self.schedules.get(room_id)) is None:
            return None
        local = now.astimezone(dt_util.get_time_zone(self.info["dateTimeZone"]))
        day, clock = _WEEKDAYS[local.weekday()], local.strftime("%H:%M")
        for block in schedule["schedule"]:
            if block["dayType"] == day and block["start"] <= clock < block["end"]:
                setting: dict[str, Any] = copy.deepcopy(block["setting"])
                return setting
        return None

    def resume_schedule(self, room: dict[str, Any], now: datetime) -> None:
        """End the manual control of a room; it runs its schedule again."""
        room["manualControlTermination"] = None
        if (setting := self.scheduled_setting(room["id"], now)) is not None:
            room["setting"] = setting

    def topology_room(self, room_id: int) -> dict[str, Any] | None:
        """Return the topology entry of a room."""
        return next(
            (room for room in self.topology["rooms"] if room["roomId"] == room_id),
            None,
        )

    def device(self, serial: str) -> dict[str, Any] | None:
        """Return a device of the topology."""
        devices = [
            device for room in self.topology["rooms"] for device in room["devices"]
        ]
        devices += self.topology.get("otherDevices", [])
        return next((d for d in devices if d["serialNumber"] == serial), None)


@dataclass(slots=True)
class _Failure:
    method: str
    matcher: Matcher
    status: int | None
    exc: BaseException | None
    headers: Mapping[str, str] | None
    body: Any
    times: int | None


def _method_matches(expected: str | None, method: str) -> bool:
    return expected in (None, "*") or expected.upper() == method


def _url_matches(matcher: Matcher | None, url: URL) -> bool:
    if matcher is None:
        return True
    if isinstance(matcher, re.Pattern):
        return matcher.search(str(url)) is not None
    if matcher.startswith("https://"):
        return str(url.with_query(None)) == matcher
    suffix = matcher if matcher.startswith("/") else f"/{matcher}"
    return url.path == suffix or url.path.endswith(suffix)


class TadoCloudMock:
    """Stateful router over aioclient_mock side effects."""

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        *,
        clock: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        """Register the fake for every tado host and HTTP method."""
        self._mocker = aioclient_mock
        self._clock = clock
        self.homes: dict[int, FakeHome] = {
            home_id: FakeHome.load(home_id) for home_id in (HOME_A, HOME_B, HOME_C)
        }
        self.account_home_ids: list[int] = [HOME_A, HOME_B]
        self.user_id = USER_ID

        self.valid_access_tokens = {entry_tokens(home)[0] for home in X_HOMES}
        self.valid_refresh_tokens = {entry_tokens(home)[1] for home in X_HOMES}
        self.token_lifetime = 599
        self._issued = 0

        self.device_flow_pending = 0
        self.device_flow_error: str | None = None

        self.quota_limit = DEFAULT_QUOTA_LIMIT
        self.quota_remaining = DEFAULT_QUOTA_REMAINING
        self._quota_reset_at: datetime | None = None

        self._failures: list[_Failure] = []
        self._holds: list[tuple[str | None, Matcher, asyncio.Event]] = []
        self._calls: list[Call] = []
        self._routes = self._build_routes()
        for method in ("get", "post", "put", "patch", "delete"):
            aioclient_mock.request(method, _TADO_URL, side_effect=self._handle)

    def home(self, home_id: int) -> FakeHome:
        """Return the server-side state of a home."""
        return self.homes[home_id]

    def fail(
        self,
        method: str | None,
        path: Matcher,
        *,
        status: int | None = None,
        exc: BaseException | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        times: int | None = None,
    ) -> None:
        """Answer matching requests with a status or raise exc; times=None is forever.

        The newest matching failure wins.
        """
        if status is None and exc is None:
            raise ValueError("fail() needs a status or an exception")
        self._failures.append(
            _Failure(method or "*", path, status, exc, headers, body, times)
        )

    def clear_failures(self) -> None:
        """Remove every injected failure."""
        self._failures.clear()

    def set_quota(
        self,
        *,
        limit: int | None = None,
        remaining: int | None = None,
        reset_in: timedelta | int | None = None,
    ) -> None:
        """Set the account quota; with reset_in, t= is sent and the quota refills then."""
        if limit is not None:
            self.quota_limit = limit
        if remaining is not None:
            self.quota_remaining = remaining
        if reset_in is not None:
            seconds = (
                reset_in
                if isinstance(reset_in, timedelta)
                else timedelta(seconds=reset_in)
            )
            self._quota_reset_at = self._clock() + seconds

    def hold(self, method: str | None, path: Matcher) -> asyncio.Event:
        """Hold matching requests until the returned event is set."""
        event = asyncio.Event()
        self._holds.append((method, path, event))
        return event

    def calls(
        self, method: str | None = None, path: Matcher | None = None
    ) -> list[Call]:
        """Return the recorded requests, optionally filtered."""
        return [
            call
            for call in self._calls
            if _method_matches(method, call.method) and _url_matches(path, call.url)
        ]

    def clear_calls(self) -> None:
        """Forget the recorded requests."""
        self._calls.clear()

    def revoke_access_tokens(self) -> None:
        """Make every issued access token invalid, so the next API call gets 401."""
        self.valid_access_tokens.clear()

    async def _handle(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        method = method.upper()
        headers = CIMultiDict(self._mocker.mock_calls[-1][3] or {})
        self._calls.append(Call(method, url, data, headers))
        for hold_method, matcher, event in list(self._holds):
            if _method_matches(hold_method, method) and _url_matches(matcher, url):
                await event.wait()
        failure = self._take_failure(method, url)
        if failure is not None and failure.exc is not None:
            raise failure.exc
        if url.host == "login.tado.com":
            if failure is not None and failure.status is not None:
                return self._response(
                    method,
                    url,
                    failure.status,
                    failure.body
                    if failure.body is not None
                    else load_fixture("token_invalid_grant.json"),
                    failure.headers,
                )
            status, body = self._login(url, data)
            return self._response(method, url, status, body)
        return self._api(method, url, data, headers, failure)

    def _take_failure(self, method: str, url: URL) -> _Failure | None:
        for failure in reversed(self._failures):
            if _method_matches(failure.method, method) and _url_matches(
                failure.matcher, url
            ):
                if failure.times is not None:
                    failure.times -= 1
                    if failure.times <= 0:
                        self._failures.remove(failure)
                return failure
        return None

    def _api(
        self,
        method: str,
        url: URL,
        data: Any,
        headers: Mapping[str, str],
        failure: _Failure | None,
    ) -> AiohttpClientMockResponse:
        now = self._clock()
        if self._quota_reset_at is not None and now >= self._quota_reset_at:
            self.quota_remaining = self.quota_limit
            self._quota_reset_at = None
        if self.quota_remaining <= 0:
            return self._response(
                method,
                url,
                HTTPStatus.TOO_MANY_REQUESTS,
                None,
                self._ratelimit_headers(url, now),
            )
        self.quota_remaining -= 1
        if failure is not None and failure.status is not None:
            if failure.status == HTTPStatus.TOO_MANY_REQUESTS:
                headers_429 = (
                    DEFAULT_429_HEADERS if failure.headers is None else failure.headers
                )
                return self._response(
                    method, url, failure.status, failure.body, headers_429
                )
            body = failure.body
            if body is None and failure.status < 500:
                body = _errors("error", f"HTTP {failure.status}")
            return self._response(
                method,
                url,
                failure.status,
                body,
                {**self._ratelimit_headers(url, now), **(failure.headers or {})},
            )
        authorization = headers.get("Authorization", "")
        result: Result
        if authorization.removeprefix("Bearer ") not in self.valid_access_tokens:
            result = (
                HTTPStatus.UNAUTHORIZED,
                _errors("unauthorized", "Full authentication is required"),
            )
        else:
            result = self._route(method, url, data)
        status, body = result
        return self._response(
            method, url, status, body, self._ratelimit_headers(url, now)
        )

    def _ratelimit_headers(self, url: URL, now: datetime) -> dict[str, str]:
        value = f'"perday";r={max(self.quota_remaining, 0)}'
        if self._quota_reset_at is not None:
            seconds = max(0, int((self._quota_reset_at - now).total_seconds()))
            value = f"{value};t={seconds}"
        policy = f'"perday";q={self.quota_limit};w=86400'
        if url.host == "my.tado.com":
            return {"RateLimit-Policy": policy, "RateLimit": value}
        return {"ratelimit-policy": policy, "ratelimit": value}

    def _response(
        self,
        method: str,
        url: URL,
        status: int,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> AiohttpClientMockResponse:
        response_headers = dict(headers or {})
        if body is not None and url.host != "hops.tado.com":
            response_headers.setdefault("Content-Type", "application/json")
        return AiohttpClientMockResponse(
            method=method,
            url=url,
            status=status,
            json=body,
            headers=response_headers,
        )

    def _issue_tokens(self) -> dict[str, Any]:
        self._issued += 1
        access = f"fake-access-token-{self._issued:04d}"
        refresh = f"fake-refresh-token-{self._issued:04d}"
        self.valid_access_tokens.add(access)
        self.valid_refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "expires_in": self.token_lifetime,
            "refresh_token": refresh,
            "scope": "offline_access",
            "token_type": "Bearer",
            "userId": self.user_id,
        }

    def _login(self, url: URL, data: Any) -> Result:
        form = self._form(data)
        if form.get("client_id") != CLIENT_ID:
            return HTTPStatus.BAD_REQUEST, {
                "error": "invalid_request",
                "error_description": "The request is missing a required parameter: client_id",
                "error_reason": "missing_client_id",
            }
        if url.path == "/oauth2/device_authorize":
            return HTTPStatus.OK, load_fixture("device_authorize.json")
        if url.path != "/oauth2/token":
            return HTTPStatus.NOT_FOUND, None
        grant = form.get("grant_type")
        if grant == "refresh_token":
            refresh = form.get("refresh_token", "")
            if refresh not in self.valid_refresh_tokens:
                return HTTPStatus.BAD_REQUEST, load_fixture("token_invalid_grant.json")
            self.valid_refresh_tokens.discard(refresh)
            return HTTPStatus.OK, self._issue_tokens()
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.device_flow_error is not None:
                return HTTPStatus.BAD_REQUEST, {"error": self.device_flow_error}
            if self.device_flow_pending > 0:
                self.device_flow_pending -= 1
                return HTTPStatus.BAD_REQUEST, load_fixture("token_pending.json")
            return HTTPStatus.OK, self._issue_tokens()
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"}

    @staticmethod
    def _form(data: Any) -> dict[str, str]:
        if isinstance(data, Mapping):
            return {str(key): str(value) for key, value in data.items()}
        if isinstance(data, bytes):
            data = data.decode()
        if isinstance(data, str):
            return dict(parse_qsl(data))
        return {}

    def _build_routes(
        self,
    ) -> list[tuple[str, str, re.Pattern[str], Callable[..., Result]]]:
        my = "my.tado.com"
        hops = "hops.tado.com"
        home = r"/homes/(\d+)"
        my_home = rf"/api/v2{home}"
        routes: list[tuple[str, str, str, Callable[..., Result]]] = [
            ("GET", my, my_home, self._get_home_info),
            ("GET", my, rf"{my_home}/state", lambda h: (200, h.state)),
            ("PUT", my, rf"{my_home}/presenceLock", self._put_presence),
            ("DELETE", my, rf"{my_home}/presenceLock", self._delete_presence),
            ("GET", my, rf"{my_home}/weather", lambda h: (200, h.weather)),
            ("GET", my, rf"{my_home}/mobileDevices", lambda h: (200, h.mobile_devices)),
            ("GET", my, rf"{my_home}/zones/(\d+)/dayReport", self._get_day_report),
            ("GET", hops, rf"{home}/rooms", lambda h: (200, h.rooms)),
            ("GET", hops, rf"{home}/rooms/(\d+)", self._get_room),
            ("POST", hops, rf"{home}/rooms/(\d+)/manualControl", self._post_manual),
            ("DELETE", hops, rf"{home}/rooms/(\d+)/manualControl", self._delete_manual),
            ("POST", hops, rf"{home}/rooms/(\d+)/openWindow", self._post_open_window),
            (
                "DELETE",
                hops,
                rf"{home}/rooms/(\d+)/openWindow",
                self._delete_open_window,
            ),
            ("GET", hops, rf"{home}/rooms/(\d+)/schedule", self._get_schedule),
            ("POST", hops, rf"{home}/rooms/(\d+)/schedule", self._post_schedule),
            ("GET", hops, rf"{home}/roomsAndDevices", lambda h: (200, h.topology)),
            (
                "PATCH",
                hops,
                rf"{home}/roomsAndDevices/devices/(\w+)",
                self._patch_device,
            ),
            (
                "POST",
                hops,
                rf"{home}/roomsAndDevices/devices/(\w+)/identify",
                self._identify,
            ),
            ("PATCH", hops, rf"{home}/roomsAndDevices/rooms/(\d+)", self._patch_room),
            ("POST", hops, rf"{home}/quickActions/(\w+)", self._quick_action),
            ("GET", hops, rf"{home}/airComfort", lambda h: (200, h.air_comfort)),
            (
                "GET",
                hops,
                rf"{home}/settings/flowTemperatureOptimization",
                self._get_flow,
            ),
            (
                "PATCH",
                hops,
                rf"{home}/settings/flowTemperatureOptimization",
                self._patch_flow,
            ),
            (
                "GET",
                hops,
                rf"{home}/settings/holidayMode",
                lambda h: (200, h.holiday_mode),
            ),
            ("PATCH", hops, rf"{home}/settings/holidayMode", self._patch_holiday),
            (
                "GET",
                "minder.tado.com",
                rf"/v1{home}/runningTimes",
                lambda h: (200, h.running_times),
            ),
            (
                "POST",
                "energy-insights.tado.com",
                rf"/api{home}/meterReadings",
                self._post_meter_reading,
            ),
            (
                "POST",
                "energy-insights.tado.com",
                rf"/api{home}/tariffs",
                self._post_tariff,
            ),
        ]
        return [
            (method, host, re.compile(pattern), handler)
            for method, host, pattern, handler in routes
        ]

    def _route(self, method: str, url: URL, data: Any) -> Result:
        if method == "GET" and url.host == "my.tado.com" and url.path == "/api/v2/me":
            return self._get_me()
        for route_method, host, pattern, handler in self._routes:
            if route_method != method or host != url.host:
                continue
            if (match := pattern.fullmatch(url.path)) is None:
                continue
            home_id = int(match.group(1))
            home = self.homes.get(home_id)
            if host == "hops.tado.com" and (home is None or not home.tado_x):
                return HTTPStatus.NOT_FOUND, _errors(
                    "not.found", f"Timezone of home {home_id} not found"
                )
            if home is None:
                return HTTPStatus.NOT_FOUND, _errors("not.found", "Home not found")
            args: list[Any] = [home, *match.groups()[1:]]
            if method in ("POST", "PUT", "PATCH"):
                args.append(data)
            return handler(*args)
        return HTTPStatus.NOT_FOUND, _errors("not.found", f"No route for {url.path}")

    def _get_me(self) -> Result:
        me = load_fixture("me.json")
        me["id"] = self.user_id
        me["homes"] = [
            {"id": home_id, "name": self.homes[home_id].info["name"]}
            for home_id in self.account_home_ids
        ]
        return HTTPStatus.OK, me

    def _get_home_info(self, home: FakeHome) -> Result:
        return HTTPStatus.OK, home.info

    def _put_presence(self, home: FakeHome, body: Any) -> Result:
        home.state = {"presence": body["homePresence"], "presenceLocked": True}
        return HTTPStatus.NO_CONTENT, None

    def _delete_presence(self, home: FakeHome) -> Result:
        home.state = {**home.state, "presenceLocked": False}
        return HTTPStatus.NO_CONTENT, None

    def _get_day_report(self, home: FakeHome, room_id: str) -> Result:
        if home.room(int(room_id)) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Zone not found")
        return HTTPStatus.OK, home.day_report

    def _get_room(self, home: FakeHome, room_id: str) -> Result:
        if (room := home.room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        return HTTPStatus.OK, room

    def _post_manual(self, home: FakeHome, room_id: str, body: Any) -> Result:
        if (room := home.room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        setting = body["setting"]
        termination = body["termination"]
        power = setting["power"]
        room["setting"] = {
            "power": power,
            "temperature": setting.get("temperature") if power == "ON" else None,
        }
        seconds = termination.get("durationInSeconds")
        expiry: str | None = None
        if termination["type"] == "TIMER":
            expiry = _iso(self._clock() + timedelta(seconds=seconds))
        elif termination["type"] == "NEXT_TIME_BLOCK":
            expiry = (room.get("nextTimeBlock") or {}).get("start")
        room["manualControlTermination"] = {
            "type": termination["type"],
            "remainingTimeInSeconds": seconds,
            "projectedExpiry": expiry,
        }
        return HTTPStatus.NO_CONTENT, None

    def _delete_manual(self, home: FakeHome, room_id: str) -> Result:
        if (room := home.room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        home.resume_schedule(room, self._clock())
        return HTTPStatus.NO_CONTENT, None

    def _post_open_window(self, home: FakeHome, room_id: str, body: Any) -> Result:
        if (room := home.room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        if room.get("openWindow") is None:
            return HTTPStatus.UNPROCESSABLE_ENTITY, _errors(
                "openWindow.notDetected", "No open window detected"
            )
        room["openWindow"] = {**room["openWindow"], "activated": True}
        return HTTPStatus.NO_CONTENT, None

    def _delete_open_window(self, home: FakeHome, room_id: str) -> Result:
        if (room := home.room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        room["openWindow"] = None
        return HTTPStatus.NO_CONTENT, None

    def _get_schedule(self, home: FakeHome, room_id: str) -> Result:
        if (schedule := home.schedules.get(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        return HTTPStatus.OK, schedule

    def _post_schedule(self, home: FakeHome, room_id: str, body: Any) -> Result:
        if (schedule := home.schedules.get(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        day = body["dayType"]
        schedule["schedule"] = [
            block for block in schedule["schedule"] if block["dayType"] != day
        ] + list(body["daySchedule"])
        return HTTPStatus.OK, None

    def _patch_device(self, home: FakeHome, serial: str, body: Any) -> Result:
        if (device := home.device(serial)) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Device not found")
        device.update(body)
        return HTTPStatus.NO_CONTENT, None

    def _identify(self, home: FakeHome, serial: str, body: Any) -> Result:
        if home.device(serial) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Device not found")
        return HTTPStatus.NO_CONTENT, None

    def _patch_room(self, home: FakeHome, room_id: str, body: Any) -> Result:
        if (room := home.topology_room(int(room_id))) is None:
            return HTTPStatus.NOT_FOUND, _errors("not.found", "Room not found")
        termination = dict(body["deviceManualControlTermination"])
        termination.setdefault("durationInSeconds", None)
        room["deviceManualControlTermination"] = termination
        return HTTPStatus.NO_CONTENT, None

    def _quick_action(self, home: FakeHome, action: str, body: Any) -> Result:
        for room in home.rooms:
            if action == "allOff":
                room["setting"] = {"power": "OFF", "temperature": None}
                room["manualControlTermination"] = {
                    "type": "MANUAL",
                    "remainingTimeInSeconds": None,
                    "projectedExpiry": None,
                }
            elif action == "resumeSchedule":
                home.resume_schedule(room, self._clock())
                room["boostMode"] = None
            elif action == "boost":
                # A boosted room as in the rooms example of the API catalogue
                # (§8): heating at 25 °C, no manual control, ends after 30 min.
                room["setting"] = {"power": "ON", "temperature": {"value": 25.0}}
                room["manualControlTermination"] = None
                room["boostMode"] = {
                    "type": "TIMER",
                    "remainingTimeInSeconds": 1800,
                    "projectedExpiry": _iso(self._clock() + timedelta(minutes=30)),
                }
            else:
                return HTTPStatus.NOT_FOUND, _errors("not.found", "Unknown action")
        return HTTPStatus.OK, None

    def _get_flow(self, home: FakeHome) -> Result:
        if home.flow_temperature is None:
            return HTTPStatus.NOT_FOUND, _errors(
                "not.found", "No boiler control device"
            )
        return HTTPStatus.OK, home.flow_temperature

    def _patch_flow(self, home: FakeHome, body: Any) -> Result:
        if home.flow_temperature is None:
            return HTTPStatus.NOT_FOUND, _errors(
                "not.found", "No boiler control device"
            )
        if "maxFlowTemperature" in body:
            home.flow_temperature["maxFlowTemperature"] = body["maxFlowTemperature"]
        if "autoAdaptation" in body:
            home.flow_temperature["autoAdaptation"].update(body["autoAdaptation"])
        return HTTPStatus.NO_CONTENT, None

    def _patch_holiday(self, home: FakeHome, body: Any) -> Result:
        home.holiday_mode = {**home.holiday_mode, **body}
        return HTTPStatus.NO_CONTENT, None

    def _post_meter_reading(self, home: FakeHome, body: Any) -> Result:
        reading = {
            "id": f"00000000-0000-4000-8000-{len(home.meter_readings) + 1:012d}",
            "homeId": home.home_id,
            **body,
        }
        home.meter_readings.append(reading)
        return HTTPStatus.OK, reading

    def _post_tariff(self, home: FakeHome, body: Any) -> Result:
        tariff = {
            "id": f"00000000-0000-4000-9000-{len(home.tariffs) + 1:012d}",
            "homeId": home.home_id,
            **body,
        }
        home.tariffs.append(tariff)
        return HTTPStatus.OK, tariff

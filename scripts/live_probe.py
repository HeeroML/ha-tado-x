#!/usr/bin/env python3
"""Read-only probe of the tado X cloud API for the tado_x integration.

Signs in with the tado device-code flow in the terminal, sends GET requests
only, and writes what tado answered to a JSON file that is safe to share:
ids, serial numbers and names become stable pseudonyms (home#1, room#3,
VA04#2), and address, coordinates, contact details and e-mail are dropped.
A findings block answers the open questions of the v1.9.0 design (section
13.2) with booleans and enums only.

Standard library only (Python 3.11 or later); never imports Home Assistant
or the integration.

    python3 scripts/live_probe.py                          # pick the tado X home
    python3 scripts/live_probe.py --home-id 123456 --yes   # no questions
    python3 scripts/live_probe.py --emit-fixtures tests/fixtures
    python3 scripts/live_probe.py --keep-raw ~/tado_x_probe_raw.json   # private
    python3 scripts/live_probe.py --token-file ~/tado_x_probe_token.json

Tokens stay in memory. --token-file keeps only the rotating refresh token, in
a file with mode 0600; delete it when you are done. The probe has its own
sign-in, so the tokens of Home Assistant are not touched. --token-file and
--keep-raw are refused inside a git work tree, where a commit could publish
them.
"""

import argparse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
LOGIN = "https://login.tado.com/oauth2/"
DEVICE_AUTH_URL = f"{LOGIN}device_authorize"
TOKEN_URL = f"{LOGIN}token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MY = "https://my.tado.com/api/v2"
HOPS = "https://hops.tado.com"
MINDER = "https://minder.tado.com/v1"
EIQ = "https://energy-insights.tado.com/api"
HOST_NAMES = {
    "login.tado.com": "login",
    "my.tado.com": "my",
    "hops.tado.com": "hops",
    "minder.tado.com": "minder",
    "energy-insights.tado.com": "eiq",
}
RECORDED_HEADERS = ("ratelimit-policy", "ratelimit", "retry-after", "content-type")
USER_AGENT = "ha-tado-x-live-probe/2"
PROBE_VERSION = 2

HOME = HOPS + "/homes/{home}"
MY_HOME = MY + "/homes/{home}"
# (label, URL template, root kind for pseudonyms, fixture file)
HOME_READS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("hops home", HOME, "home", "hops_home.json"),
    ("hops rooms", f"{HOME}/rooms", "room", "rooms.json"),
    ("hops roomsAndDevices", f"{HOME}/roomsAndDevices", None, "rooms_and_devices.json"),
    ("hops features", f"{HOME}/features", None, "features.json"),
    ("hops airComfort", f"{HOME}/airComfort", None, "air_comfort.json"),
    ("hops airComfort api/v2", HOPS + "/api/v2/homes/{home}/airComfort", None, None),
    ("hops holidayMode", f"{HOME}/settings/holidayMode", None, "holiday_mode.json"),
    (
        "hops flowTemperatureOptimization",
        f"{HOME}/settings/flowTemperatureOptimization",
        None,
        "flow_temperature.json",
    ),
    (
        "hops domesticHotWater",
        f"{HOME}/programmer/domesticHotWater",
        None,
        "domestic_hot_water.json",
    ),
    ("my state", f"{MY_HOME}/state", None, "state.json"),
    ("my weather", f"{MY_HOME}/weather", None, "weather.json"),
    ("my mobileDevices", f"{MY_HOME}/mobileDevices", "phone", "mobile_devices.json"),
    (
        "minder runningTimes",
        MINDER + "/homes/{home}/runningTimes?from={day}&to={day}",
        None,
        "running_times.json",
    ),
    ("eiq tariffs", EIQ + "/homes/{home}/tariffs", None, "tariffs.json"),
    (
        "eiq meterReadings",
        EIQ + "/homes/{home}/meterReadings",
        None,
        "meter_readings.json",
    ),
)
# (label, URL template, root kind, fixture file of the first room)
ROOM_READS: tuple[tuple[str, str, str | None, str], ...] = (
    ("hops room", HOME + "/rooms/{room}", "room", "room.json"),
    ("hops schedule", HOME + "/rooms/{room}/schedule", None, "schedule.json"),
    ("hops away setting", HOME + "/settings/away/rooms/{room}", None, "away_room.json"),
)
DAY_REPORT = (
    "my dayReport",
    MY_HOME + "/zones/{room}/dayReport?date={day}",
    None,
    "day_report.json",
)
ACCOUNT_REQUESTS = 1
DELTA_HOSTS = ("hops", "minder", "eiq", "login")
DELTA_RUNS = 2
DELTA_REQUESTS = 1 + len(DELTA_HOSTS) * DELTA_RUNS * 2

DROP_KEYS = frozenset(
    {
        "address",
        "contactDetails",
        "email",
        "geolocation",
        "latitude",
        "longitude",
        "username",
    }
)
BLUR_KEYS = frozenset({"bearingFromHome", "relativeDistanceFromHomeFence"})
CONTEXT_KEYS = {
    "homes": "home",
    "rooms": "room",
    "zones": "room",
    "comfort": "room",
    "roomSettings": "room",
    "mobileDevices": "phone",
    "devices": "device",
    "otherDevices": "device",
}
ID_KEYS = {
    "homeId": "home",
    "roomId": "room",
    "zoneId": "room",
    "mobileDeviceId": "phone",
}
NAME_KEYS = {"roomName": "room", "homeName": "home"}
SERIAL_KEY = re.compile(r"serial", re.IGNORECASE)
SAFE_TEXT = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*"  # enums
    r"|[a-z][A-Za-z0-9_.]*"  # identifiers, units, locales, error codes
    r"|[A-Z][a-z]+/[A-Za-z_/+-]+"  # time zones
    r"|\d+(?:\.\d+)*"  # versions
    r"|\d{2}:\d{2}(?::\d{2})?"  # clock times
    r"|\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+)?(?:Z|[+-]\d{2}:?\d{2})?)$"  # dates
)
FIXTURE_FAKES: dict[str, Any] = {
    "address": {
        "addressLine1": "Musterstrasse 1",
        "addressLine2": None,
        "zipCode": "00000",
        "city": "Musterstadt",
        "state": None,
        "country": "DEU",
    },
    "contactDetails": {
        "name": "Test User",
        "email": "user@example.invalid",
        "phone": None,
    },
    "email": "user@example.invalid",
    "geolocation": {"latitude": 50.0, "longitude": 8.0},
    "latitude": 50.0,
    "longitude": 8.0,
    "username": "user@example.invalid",
}
FIXTURE_HOME_IDS = (1000002, 1000001, 1000003)
FIXTURE_HOME_NAMES = {1000001: "Haus A", 1000002: "Haus B", 1000003: "Haus C"}


class ProbeError(Exception):
    """A request failed in a way the probe cannot continue from."""


class QuotaExhaustedError(ProbeError):
    """tado answered 429; the run stops so it spends no more of the quota."""


@dataclass(frozen=True, slots=True)
class Response:
    """Status, lowercased headers and parsed body of one HTTP exchange."""

    status: int
    headers: Mapping[str, str]
    payload: Any


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded request; sign-in exchanges never keep their body."""

    label: str
    method: str
    template: str
    params: Mapping[str, Any]
    status: int
    headers: Mapping[str, str]
    payload: Any = None
    kind: str | None = None
    fixture: str | None = None

    @property
    def host(self) -> str:
        """Short name of the host the request went to."""
        netloc = urllib.parse.urlsplit(self.template).netloc
        return HOST_NAMES.get(netloc, netloc)

    @property
    def ok(self) -> bool:
        """Whether tado answered with a 2xx status."""
        return 200 <= self.status < 300


def _http(
    method: str,
    url: str,
    *,
    form: Mapping[str, str] | None = None,
    token: str | None = None,
    timeout: float = 30,
) -> Response:
    if method != "GET" and not url.startswith(LOGIN):
        raise ProbeError(f"refusing {method} request to {url}; the probe only reads")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw_headers, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        status, raw_headers, raw = err.code, err.headers, err.read()
    except (OSError, http.client.HTTPException) as err:
        host = urllib.parse.urlsplit(url).netloc
        raise ProbeError(f"cannot reach {host}: {err}") from err
    text = raw.decode("utf-8", "replace")
    try:
        payload: Any = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = text[:500]
    lowered = {key.lower(): value for key, value in raw_headers.items()}
    return Response(status, lowered, payload)


def verification_url(verification_uri: str, user_code: str) -> str:
    """Return the sign-in URL with user_code and client_id; tado needs both (#76)."""
    parts = urllib.parse.urlsplit(verification_uri)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.update({"user_code": user_code, "client_id": CLIENT_ID})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def has_client_id(url: object) -> bool | None:
    """Return whether a URL carries client_id; None when there is no URL."""
    if not isinstance(url, str):
        return None
    return "client_id" in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def token_meta(body: object) -> dict[str, Any]:
    """Return what the probe keeps of a token response: no token at all."""
    if not isinstance(body, Mapping):
        return {"expires_in": None, "token_type": None, "refresh_token_returned": False}
    return {
        "expires_in": body.get("expires_in"),
        "token_type": body.get("token_type"),
        "refresh_token_returned": bool(body.get("refresh_token")),
    }


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read, also when it already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def git_work_tree(path: Path) -> Path | None:
    """Return the git work tree a path would be written into, or None.

    Like git, a folder counts only when its .git is a repository (it holds
    HEAD) or a gitdir link of a linked work tree or submodule.
    """
    for folder in path.resolve().parents:
        marker = folder / ".git"
        if (marker / "HEAD").is_file() or (
            marker.is_file()
            and marker.read_text(encoding="utf-8", errors="replace").startswith(
                "gitdir:"
            )
        ):
            return folder
    return None


def parse_ratelimit(value: str | None) -> dict[str, int]:
    """Return the integer parameters (q, w, r, t) of a ratelimit header."""
    for member in (value or "").split(","):
        if params := re.findall(r"(\w+)=(\d+)", member):
            return {key: int(number) for key, number in params}
    return {}


def home_today(time_zone: str | None, now: datetime) -> date:
    """Return today in the home's time zone, or in the local zone without one."""
    return now.astimezone(_zone(time_zone)).date()


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None
    except ValueError:  # a malformed key such as "../x"
        return None


def planned_requests(*, homes: int, rooms: int, history: bool) -> int:
    """Return the number of tado API requests a run sends."""
    per_room = len(ROOM_READS) + int(history)
    return (
        ACCOUNT_REQUESTS + homes + len(HOME_READS) + rooms * per_room + DELTA_REQUESTS
    )


def classify_delta(deltas: Iterable[int | None]) -> str:
    """Rate whether a host counts against the quota from the r-delta runs.

    Each run is a call to the host followed by GET /me: r drops by 2 when the
    host counts and by 1 when it does not. Other clients of the account, such
    as the core tado integration, add noise, so only agreeing runs decide.
    """
    values = list(deltas)
    if values and all(value == 2 for value in values):
        return "likely"
    if values and all(value == 1 for value in values):
        return "unlikely"
    return "inconclusive"


class Anonymizer:
    """Replace identifying values with stable tokens.

    In pseudonym mode a real id, serial or name always maps to the same token
    (home#1, room#3, VA04#2) and dropped keys disappear. In fixture mode the
    values follow tests/fixtures: the probed home is 1000002, rooms are 1..n,
    serials look like VA0000000101 and dropped keys get fixed fake values.
    """

    def __init__(self, *, fixtures: bool = False) -> None:
        """Start with no tokens handed out."""
        self.fixtures = fixtures
        self._tokens: dict[tuple[str, object], object] = {}
        self._counts: dict[str, int] = {}
        self._serials: dict[str, tuple[str, object]] = {}

    def learn_topology(self, topology: object) -> None:
        """Record type and room of every device of a roomsAndDevices payload."""
        topology = _mapping(topology)
        for room in _mappings(topology.get("rooms")):
            for device in _mappings(room.get("devices")):
                self._learn_device(device, room.get("roomId"))
        for device in _mappings(topology.get("otherDevices")):
            self._learn_device(device, None)

    def _learn_device(self, device: Mapping[str, Any], room_id: object) -> None:
        serial = device.get("serialNumber")
        if isinstance(serial, str):
            self._serials[serial] = (str(device.get("type") or serial[:2]), room_id)

    def _next(self, counter: str) -> int:
        self._counts[counter] = self._counts.get(counter, 0) + 1
        return self._counts[counter]

    def token(self, kind: str, real: object) -> object:
        """Return the token of a real value of a kind (home, room, phone, ...)."""
        if real is None:
            return None
        key = (kind, real)
        if key not in self._tokens:
            self._tokens[key] = self._new_token(kind, real)
        return self._tokens[key]

    def _new_token(self, kind: str, real: object) -> object:
        if kind == "serial":
            return self._serial_token(str(real))
        number = self._next(kind)
        if not self.fixtures:
            return f"{kind}#{number}"
        if kind == "home":
            if number <= len(FIXTURE_HOME_IDS):
                return FIXTURE_HOME_IDS[number - 1]
            return 1000000 + number
        if kind == "room":
            return number
        if kind == "phone":
            return 5000000 + number
        if kind == "user":
            return f"00000000-0000-4000-8000-{number:012d}"
        return 9000000 + number if isinstance(real, int) else f"{kind}-{number}"

    def _serial_token(self, serial: str) -> str:
        device_type, room_id = self._serials.get(serial, (serial[:2], None))
        if not self.fixtures:
            return f"{device_type}#{self._next(f'serial:{device_type}')}"
        room = self.token("room", room_id) if room_id is not None else 0
        index = self._next(f"serial:{serial[:2]}:{room}")
        return f"{serial[:2]}000000{index:02d}{room:02d}"

    def name(self, kind: str, real: object, owner: object) -> object:
        """Return the token of a name; in fixtures a name follows its owner."""
        if not isinstance(real, str):
            return real
        if not self.fixtures:
            return self.token("name", real)
        if kind == "room":
            return f"Room {self.token('room', owner)}"
        if kind == "home":
            home = self.token("home", owner)
            return (
                FIXTURE_HOME_NAMES.get(home, f"Home {home}")
                if isinstance(home, int)
                else "Home"
            )
        if kind == "user":
            return "Test User"
        return f"{kind.capitalize()} {self._next(f'name:{kind}')}"

    def anonymize(self, value: Any, kind: str | None = None) -> Any:
        """Return a copy of a payload with every identifying value replaced."""
        if isinstance(value, Mapping):
            return self._mapping(value, kind)
        if isinstance(value, list):
            return [self.anonymize(item, kind) for item in value]
        return _scalar(value)

    def _mapping(self, value: Mapping[str, Any], kind: str | None) -> dict[str, Any]:
        owner = value.get("id", value.get("roomId"))
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in DROP_KEYS:
                if self.fixtures:
                    result[key] = None if item is None else FIXTURE_FAKES[key]
            elif key in BLUR_KEYS:
                result[key] = _blur(item)
            elif isinstance(item, (Mapping, list)):
                result[key] = self.anonymize(item, CONTEXT_KEYS.get(key))
            else:
                result[key] = self._field(key, item, kind, owner)
        return result

    def _field(
        self, key: str, value: object, kind: str | None, owner: object
    ) -> object:
        if key == "id":
            return self.token("serial" if kind == "device" else kind or "id", value)
        if key in ID_KEYS:
            return self.token(ID_KEYS[key], value)
        if SERIAL_KEY.search(key) and isinstance(value, str):
            return self.token("serial", value)
        if key == "name" or key in NAME_KEYS:
            return self.name(NAME_KEYS.get(key, kind or "other"), value, owner)
        return _scalar(value)


def _scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if SAFE_TEXT.match(value) else f"<text len={len(value)}>"
    return f"<{type(value).__name__}>"


def _blur(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _blur(item) for key, item in value.items()}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0.0
    return value


def _payloads(exchanges: Iterable[Exchange], label: str) -> list[Any]:
    return [ex.payload for ex in exchanges if ex.label == label and ex.ok]


def _payload(exchanges: Iterable[Exchange], label: str) -> Any:
    return next(iter(_payloads(exchanges, label)), None)


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a payload that is an object, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """Return the objects of a payload that is a list; anything else has none."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def room_ids(rooms: Any) -> list[Any]:
    """Return the room ids of a hops rooms payload of any shape."""
    return [room["id"] for room in _mappings(rooms) if room.get("id") is not None]


def _status(exchanges: Iterable[Exchange], label: str) -> int | None:
    return next((ex.status for ex in exchanges if ex.label == label), None)


def _shape(payload: Any, key: str) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        return "list"
    if isinstance(payload, Mapping) and isinstance(payload.get(key), list):
        return "wrapped"
    return "other"


def _topology_findings(topology: Any) -> dict[str, Any]:
    if not isinstance(topology, Mapping):
        return {
            "duration_present_when_not_timer": None,
            "termination_types_seen": [],
            "battery_values_seen": [],
            "device_fields_by_type": {},
        }
    rooms = _mappings(topology.get("rooms"))
    defaults = [_mapping(room.get("deviceManualControlTermination")) for room in rooms]
    devices = [
        *(device for room in rooms for device in _mappings(room.get("devices"))),
        *_mappings(topology.get("otherDevices")),
    ]
    fields: dict[str, dict[str, bool]] = {}
    for device in devices:
        seen = fields.setdefault(str(device.get("type")), {})
        for name in (
            "batteryState",
            "childLockEnabled",
            "controlMode",
            "mountingState",
        ):
            seen[name] = seen.get(name, False) or name in device
    return {
        "duration_present_when_not_timer": any(
            item.get("type") != "TIMER" and item.get("durationInSeconds") is not None
            for item in defaults
        ),
        "termination_types_seen": sorted(
            {str(item["type"]) for item in defaults if "type" in item}
        ),
        "battery_values_seen": sorted(
            {
                str(device["batteryState"])
                for device in devices
                if "batteryState" in device
            }
        ),
        "device_fields_by_type": dict(sorted(fields.items())),
    }


def _minder_ids_equal_room_ids(running_times: Any, rooms: Any) -> bool | None:
    if not isinstance(running_times, Mapping) or not isinstance(rooms, list):
        return None
    zone_ids = {
        zone.get("id")
        for period in _mappings(running_times.get("runningTimes"))
        for zone in _mappings(period.get("zones"))
    }
    if not zone_ids:
        return None
    return zone_ids <= set(room_ids(rooms))


def _ratelimit_findings(exchanges: Iterable[Exchange]) -> dict[str, Any]:
    hosts: dict[str, bool] = {}
    t_with_r: bool | None = None
    for exchange in exchanges:
        params = parse_ratelimit(exchange.headers.get("ratelimit"))
        hosts[exchange.host] = hosts.get(exchange.host, False) or bool(params)
        if params.get("r", 0) > 0:
            t_with_r = bool(t_with_r) or "t" in params
    return {
        "ratelimit_header_hosts": dict(sorted(hosts.items())),
        "t_with_r_gt_0": t_with_r,
    }


def compute_findings(
    exchanges: list[Exchange],
    *,
    login: Mapping[str, Any],
    deltas: Mapping[str, list[int | None]],
) -> dict[str, Any]:
    """Answer the live-check questions of design section 13.2 from raw data.

    Every value is a boolean, an enum, a status code or a key list, so the
    block holds no personal data.
    """
    rooms = _payload(exchanges, "hops rooms")
    homes = _mappings(_payloads(exchanges, "my home"))
    probed = next((home for home in homes if home.get("generation") == "LINE_X"), {})
    hops_home = _mapping(_payload(exchanges, "hops home"))
    holiday = _payload(exchanges, "hops holidayMode")
    room_states = _mappings([*_mappings(rooms), *_payloads(exchanges, "hops room")])
    return {
        **login,
        **_ratelimit_findings(exchanges),
        "status_429_seen": any(ex.status == 429 for ex in exchanges),
        "hosts_counting": {
            host: classify_delta(values) for host, values in deltas.items()
        },
        "generations": sorted({str(home.get("generation")) for home in homes}),
        "supports_flow_temperature": hops_home.get(
            "supportsFlowTemperatureOptimization",
            probed.get("supportsFlowTemperatureOptimization"),
        ),
        "air_comfort_eligible": probed.get("isAirComfortEligible"),
        "energy_iq_eligible": probed.get("isEnergyIqEligible"),
        "air_comfort_status": {
            "hops_homes": _status(exchanges, "hops airComfort"),
            "hops_api_v2": _status(exchanges, "hops airComfort api/v2"),
        },
        "flow_temperature_status": _status(
            exchanges, "hops flowTemperatureOptimization"
        ),
        "day_report_status": _status(exchanges, "my dayReport"),
        **_topology_findings(_payload(exchanges, "hops roomsAndDevices")),
        "open_window_activated_false_seen": any(
            isinstance(room.get("openWindow"), Mapping)
            and room["openWindow"].get("activated") is False
            for room in room_states
        ),
        "minder_ids_equal_room_ids": _minder_ids_equal_room_ids(
            _payload(exchanges, "minder runningTimes"), rooms
        ),
        "holiday_mode_keys": sorted(holiday) if isinstance(holiday, Mapping) else None,
        "tariffs_shape": _shape(_payload(exchanges, "eiq tariffs"), "tariffs"),
        "meter_readings_shape": _shape(
            _payload(exchanges, "eiq meterReadings"), "readings"
        ),
        "dhw_capable": _mapping(_payload(exchanges, "hops domesticHotWater")).get(
            "isDomesticHotWaterCapable"
        ),
    }


def shown_url(exchange: Exchange, anonymizer: Anonymizer) -> str:
    """Return the request URL with home and room ids replaced by tokens."""
    values = {
        key: anonymizer.token(key, value) if key in {"home", "room"} else value
        for key, value in exchange.params.items()
    }
    return exchange.template.format(**values)


def _recorded(response: Response) -> dict[str, str]:
    return {
        key: response.headers[key]
        for key in RECORDED_HEADERS
        if key in response.headers
    }


@dataclass(slots=True)
class Probe:
    """Signed-in session that records every exchange."""

    token_file: Path | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    login_findings: dict[str, Any] = field(default_factory=dict)
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    expires: float = 0.0

    def login(self) -> None:
        """Sign in from the token file if it still works, else with a device code."""
        if self.token_file is not None and self.token_file.exists():
            stored = json.loads(self.token_file.read_text(encoding="utf-8"))
            self.refresh_token = str(stored.get("refresh_token") or "")
            if self.refresh_token and self.refresh(
                label="login stored token", fatal=False
            ):
                return
            print("The stored refresh token no longer works; starting a new sign-in.")
        self._device_login()

    def _post(self, label: str, url: str, form: Mapping[str, str]) -> Response:
        response = _http("POST", url, form=form)
        self.exchanges.append(
            Exchange(label, "POST", url, {}, response.status, _recorded(response))
        )
        if response.status == 429:
            raise QuotaExhaustedError(f"login.tado.com answered 429 ({label})")
        return response

    def _device_login(self) -> None:
        response = self._post(
            "login device_authorize",
            DEVICE_AUTH_URL,
            {"client_id": CLIENT_ID, "scope": "offline_access"},
        )
        body = response.payload
        if response.status != 200 or not isinstance(body, Mapping):
            raise ProbeError(f"device_authorize failed with HTTP {response.status}")
        self.login_findings["verification_uri_complete_has_client_id"] = has_client_id(
            body.get("verification_uri_complete")
        )
        print("\nOpen this URL, sign in to tado and confirm the code:")
        print(f"  {verification_url(body['verification_uri'], body['user_code'])}")
        print(f"  code: {body['user_code']}\n")
        interval = int(body.get("interval") or 5)
        deadline = time.monotonic() + int(body.get("expires_in") or 300)
        form = {
            "client_id": CLIENT_ID,
            "device_code": body["device_code"],
            "grant_type": DEVICE_GRANT,
        }
        while time.monotonic() < deadline:
            time.sleep(interval)
            token = self._post("login device token", TOKEN_URL, form)
            if token.status == 200:
                self._adopt(token.payload)
                print("Signed in.\n")
                return
            error = (
                token.payload.get("error")
                if isinstance(token.payload, Mapping)
                else None
            )
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise ProbeError(f"sign-in failed: HTTP {token.status} {error}")
        raise ProbeError("the code expired before the sign-in was confirmed")

    def refresh(self, *, label: str = "login refresh", fatal: bool = True) -> bool:
        """Rotate the tokens; return False on a rejection unless fatal."""
        response = self._post(
            label,
            TOKEN_URL,
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )
        if response.status == 200:
            self._adopt(response.payload)
            return True
        if fatal:
            raise ProbeError(f"token refresh failed with HTTP {response.status}")
        return False

    def _adopt(self, body: Any) -> None:
        if not isinstance(body, Mapping) or not body.get("access_token"):
            raise ProbeError("tado sent a token response without an access token")
        self.login_findings.update(token_meta(body))
        self.access_token = str(body["access_token"])
        self.refresh_token = str(body.get("refresh_token") or self.refresh_token)
        self.expires = time.monotonic() + int(body.get("expires_in") or 600)
        if self.token_file is not None:
            write_private(
                self.token_file, json.dumps({"refresh_token": self.refresh_token})
            )

    def get(
        self,
        label: str,
        template: str,
        *,
        kind: str | None = None,
        fixture: str | None = None,
        record: bool = True,
        **params: Any,
    ) -> Any:
        """GET a URL template and return the payload of a 2xx answer, else None."""
        if time.monotonic() > self.expires - 60:
            self.refresh()
        response = _http("GET", template.format(**params), token=self.access_token)
        self.exchanges.append(
            Exchange(
                label,
                "GET",
                template,
                params,
                response.status,
                _recorded(response),
                response.payload if record else None,
                kind,
                fixture,
            )
        )
        print(f"  {response.status}  {label}")
        if response.status == 429:
            raise QuotaExhaustedError("tado answered 429; the API quota is used up")
        return response.payload if 200 <= response.status < 300 else None

    def remaining(self) -> int | None:
        """GET /me and return r= of its ratelimit header."""
        self.get("r-delta me", f"{MY}/me", record=False)
        return parse_ratelimit(self.exchanges[-1].headers.get("ratelimit")).get("r")


def choose_home(probe: Probe, home_id: int | None, *, ask: bool) -> int:
    """Read the account's homes and return the tado X home to probe."""
    account = probe.get("me", f"{MY}/me", kind="user", fixture="me.json") or {}
    x_homes: list[int] = []
    for home in account.get("homes") or []:
        info = probe.get(
            "my home", MY_HOME, kind="home", fixture="home_{home}.json", home=home["id"]
        )
        if (info or {}).get("generation") == "LINE_X":
            x_homes.append(home["id"])
    if home_id is not None:
        if home_id not in x_homes:
            raise ProbeError("that home is not a tado X home of this account")
        return home_id
    if len(x_homes) == 1:
        return x_homes[0]
    if not x_homes:
        raise ProbeError("this account has no tado X home")
    if not ask:
        raise ProbeError("this account has several tado X homes; pass --home-id")
    print("\ntado X homes on this account:")
    for index, hid in enumerate(x_homes):
        print(f"  [{index}] home {hid}")
    return x_homes[int(input("Number of the home to probe: "))]


def probe_home(probe: Probe, home: int, *, max_rooms: int, history: bool) -> None:
    """GET every read endpoint of one tado X home and of its rooms."""
    info = next(
        (
            ex.payload
            for ex in probe.exchanges
            if ex.label == "my home" and ex.ok and ex.params.get("home") == home
        ),
        None,
    )
    time_zone = info.get("dateTimeZone") if isinstance(info, Mapping) else None
    day = home_today(time_zone, datetime.now(UTC)).isoformat()
    for label, template, kind, fixture in HOME_READS:
        probe.get(label, template, kind=kind, fixture=fixture, home=home, day=day)
    rooms = room_ids(_payload(probe.exchanges, "hops rooms"))
    room_reads = (*ROOM_READS, DAY_REPORT) if history else ROOM_READS
    for index, room in enumerate(rooms[:max_rooms]):
        for label, template, kind, fixture in room_reads:
            probe.get(
                label,
                template,
                kind=kind,
                fixture=fixture if index == 0 else None,
                home=home,
                room=room,
                day=day,
            )


def measure_deltas(probe: Probe, home: int) -> dict[str, list[int | None]]:
    """Run the r-delta method for every host whose counting is unknown."""
    day = datetime.now(UTC).date().isoformat()
    calls: dict[str, Callable[[], object]] = {
        "hops": lambda: probe.get("r-delta hops", HOME, record=False, home=home),
        "minder": lambda: probe.get(
            "r-delta minder",
            MINDER + "/homes/{home}/runningTimes?from={day}&to={day}",
            record=False,
            home=home,
            day=day,
        ),
        "eiq": lambda: probe.get(
            "r-delta eiq", EIQ + "/homes/{home}/meterReadings", record=False, home=home
        ),
        "login": lambda: probe.refresh(label="r-delta login"),
    }
    deltas: dict[str, list[int | None]] = {}
    before = probe.remaining()
    for host in DELTA_HOSTS:
        deltas[host] = []
        for _ in range(DELTA_RUNS):
            calls[host]()
            after = probe.remaining()
            deltas[host].append(
                None if before is None or after is None else before - after
            )
            before = after
    return deltas


def _prepare(
    anonymizer: Anonymizer, exchanges: list[Exchange], home: int | None
) -> None:
    """Hand out tokens in a fixed order: probed home, other homes, rooms."""
    anonymizer.token("home", home)
    for exchange in exchanges:
        anonymizer.token("home", exchange.params.get("home"))
    for room in room_ids(_payload(exchanges, "hops rooms")):
        anonymizer.token("room", room)
    anonymizer.learn_topology(_payload(exchanges, "hops roomsAndDevices"))


def build_report(
    exchanges: list[Exchange], findings: Mapping[str, Any], home: int | None
) -> dict[str, Any]:
    """Return the shareable result: findings and pseudonymized exchanges."""
    anonymizer = Anonymizer()
    _prepare(anonymizer, exchanges, home)
    return {
        "probe_version": PROBE_VERSION,
        "generated": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "findings": dict(findings),
        "requests": [
            {
                "label": exchange.label,
                "method": exchange.method,
                "url": shown_url(exchange, anonymizer),
                "status": exchange.status,
                "headers": dict(exchange.headers),
                "body": anonymizer.anonymize(exchange.payload, exchange.kind),
            }
            for exchange in exchanges
        ],
    }


def build_fixtures(exchanges: list[Exchange], home: int | None) -> dict[str, Any]:
    """Return fixture file name -> payload in the format of tests/fixtures."""
    anonymizer = Anonymizer(fixtures=True)
    _prepare(anonymizer, exchanges, home)
    fixtures: dict[str, Any] = {}
    for exchange in exchanges:
        if exchange.fixture is None or not exchange.ok or exchange.payload is None:
            continue
        name = exchange.fixture.format(
            home=anonymizer.token("home", exchange.params.get("home"))
        )
        if name not in fixtures:
            fixtures[name] = anonymizer.anonymize(exchange.payload, exchange.kind)
    return fixtures


def _dump(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _confirm(args: argparse.Namespace) -> bool:
    cost = planned_requests(homes=2, rooms=args.max_rooms, history=args.history)
    print(
        f"This run sends up to {cost} requests to the tado API, counted against the "
        f"daily quota of the account: {len(ROOM_READS) + int(args.history)} per room "
        f"for up to {args.max_rooms} rooms and {DELTA_REQUESTS} to find out which hosts "
        "count. Each home beyond two adds one more; sign-in requests come on top."
    )
    if args.yes:
        return True
    return input("Send them? [y/N] ").strip().lower() in {"y", "yes"}


def write_outputs(
    args: argparse.Namespace,
    exchanges: list[Exchange],
    findings: Mapping[str, Any],
    home: int | None,
) -> None:
    """Write the shareable report, the private raw copy and the fixtures."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = args.out or Path(f"tado_x_probe_{stamp}.json")
    out.write_text(_dump(build_report(exchanges, findings, home)), encoding="utf-8")
    print(f"\nShareable result written to {out}. Read it before you share it.")
    print(_dump(findings), end="")
    if args.keep_raw is not None:
        raw = [
            {
                "label": ex.label,
                "url": ex.template.format(**ex.params),
                "status": ex.status,
                "headers": dict(ex.headers),
                "body": ex.payload,
            }
            for ex in exchanges
        ]
        write_private(args.keep_raw, _dump(raw))
        print(
            f"Unredacted copy written to {args.keep_raw} (personal data, do not share)."
        )
    if args.emit_fixtures is not None:
        args.emit_fixtures.mkdir(parents=True, exist_ok=True)
        for name, payload in build_fixtures(exchanges, home).items():
            (args.emit_fixtures / name).write_text(_dump(payload), encoding="utf-8")
        print(f"Fixtures written to {args.emit_fixtures}.")


def run(args: argparse.Namespace) -> int:
    """Run the probe and write what it collected, also when it stops early.

    Return 0 after a full run, 2 after a 429, 130 after Ctrl-C and 1 after
    any other stop.
    """
    if not _confirm(args):
        print("Nothing sent.")
        return 1
    probe = Probe(token_file=args.token_file)
    home: int | None = None
    deltas: dict[str, list[int | None]] = {}
    code = 0
    try:
        probe.login()
        print("Requests:")
        home = choose_home(probe, args.home_id, ask=not args.yes)
        probe_home(probe, home, max_rooms=args.max_rooms, history=args.history)
        deltas = measure_deltas(probe, home)
    except QuotaExhaustedError as err:
        print(f"\nStopped: {err}")
        code = 2
    except ProbeError as err:
        print(f"\nStopped early: {err}")
        code = 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        code = 130
    except Exception as err:  # noqa: BLE001 - the requests already sent are reported
        print(f"\nStopped by an unexpected error: {type(err).__name__}: {err}")
        code = 1
    try:
        findings = compute_findings(
            probe.exchanges, login=probe.login_findings, deltas=deltas
        )
        write_outputs(args, probe.exchanges, findings, home)
    finally:
        if args.token_file is not None:
            print(
                f"Delete {args.token_file} when you are done; "
                "it signs in to your tado account."
            )
    return code


def _user_path(value: str) -> Path:
    return Path(value).expanduser()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Return the command line options; private files must stay outside git."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--home-id", type=int, help="tado X home to probe")
    parser.add_argument("--out", type=_user_path, help="shareable result file")
    parser.add_argument(
        "--token-file",
        type=_user_path,
        help="keep the refresh token here (0600), outside any git work tree",
    )
    parser.add_argument(
        "--keep-raw",
        type=_user_path,
        help="unredacted copy (0600), outside any git work tree",
    )
    parser.add_argument(
        "--emit-fixtures", type=_user_path, help="write test fixtures to this directory"
    )
    parser.add_argument(
        "--max-rooms", type=int, default=10, help="probe at most this many rooms"
    )
    parser.add_argument(
        "--history", action="store_true", help="also read each room's dayReport"
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask before sending requests"
    )
    args = parser.parse_args(argv)
    for option, path, example in (
        ("--token-file", args.token_file, "~/tado_x_probe_token.json"),
        ("--keep-raw", args.keep_raw, "~/tado_x_probe_raw.json"),
    ):
        if path is not None and (tree := git_work_tree(path)) is not None:
            parser.error(
                f"{option} {path} is inside the git work tree {tree}, where a "
                f"commit could publish it; use a path outside it, such as {example}"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the probe from the command line."""
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

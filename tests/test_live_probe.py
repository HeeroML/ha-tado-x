"""Unit tests of scripts/live_probe.py; nothing here touches the network."""

from datetime import UTC, date, datetime
import importlib.util
import json
from pathlib import Path
import stat
from types import ModuleType
from typing import Any
import urllib.parse

import pytest

from tests.tado_mock import load_fixture

ROOT = Path(__file__).parent.parent


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "live_probe", ROOT / "scripts" / "live_probe.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

NORMAL = {
    "ratelimit-policy": '"perday";q=20000;w=86400',
    "ratelimit": '"perday";r=17833',
}

# Real-looking values of a made-up account; none may reach a shareable file.
PERSONAL = ("Max Mustermann", "max@example.invalid", "Ferienhaus", "Wohnung", "Bad")
ACCOUNT = {
    "id": "d4c3b2a1-0000-4000-8000-00000000abcd",
    "name": "Max Mustermann",
    "email": "max@example.invalid",
    "username": "max@example.invalid",
    "homes": [{"id": 4711, "name": "Ferienhaus"}, {"id": 815, "name": "Wohnung"}],
}
HOME_X = {
    "id": 815,
    "name": "Wohnung",
    "generation": "LINE_X",
    "dateTimeZone": "Europe/Berlin",
    "address": {"addressLine1": "Hauptstrasse 5", "city": "Kassel"},
    "geolocation": {"latitude": 51.3, "longitude": 9.5},
    "contactDetails": {"name": "Max Mustermann", "email": "max@example.invalid"},
    "isAirComfortEligible": True,
}
HOME_V3 = {"id": 4711, "name": "Ferienhaus", "generation": "PRE_LINE_X"}
ROOMS = [
    {
        "id": 42,
        "name": "Bad",
        "openWindow": {"activated": False, "expiresInSeconds": 900},
    },
    {"id": 7, "name": "Arbeitszimmer", "openWindow": None},
]
TOPOLOGY = {
    "rooms": [
        {
            "roomId": 42,
            "roomName": "Bad",
            "deviceManualControlTermination": {
                "type": "MANUAL",
                "durationInSeconds": None,
            },
            "devices": [
                {"serialNumber": "VA7766554433", "type": "VA04", "batteryState": "LOW"},
                {
                    "serialNumber": "SU1122334455",
                    "type": "SU04",
                    "batteryState": "NORMAL",
                },
            ],
        },
        {
            "roomId": 7,
            "roomName": "Arbeitszimmer",
            "deviceManualControlTermination": {
                "type": "TIMER",
                "durationInSeconds": 1800,
            },
            "devices": [
                {
                    "serialNumber": "VA9988776655",
                    "type": "VA04",
                    "childLockEnabled": True,
                }
            ],
        },
    ],
    "otherDevices": [{"serialNumber": "IB5544332211", "type": "IB02"}],
}


def _exchange(
    label: str,
    template: str,
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    kind: str | None = None,
    fixture: str | None = None,
    **params: Any,
) -> Any:
    return probe.Exchange(
        label,
        "GET",
        template,
        params,
        status,
        headers if headers is not None else NORMAL,
        payload,
        kind,
        fixture,
    )


def _account_exchanges() -> list[Any]:
    return [
        _exchange("me", f"{probe.MY}/me", ACCOUNT, kind="user", fixture="me.json"),
        _exchange(
            "my home",
            probe.MY_HOME,
            HOME_V3,
            kind="home",
            fixture="home_{home}.json",
            home=4711,
        ),
        _exchange(
            "my home",
            probe.MY_HOME,
            HOME_X,
            kind="home",
            fixture="home_{home}.json",
            home=815,
        ),
        _exchange(
            "hops rooms",
            f"{probe.HOME}/rooms",
            ROOMS,
            kind="room",
            fixture="rooms.json",
            home=815,
        ),
        _exchange(
            "hops roomsAndDevices",
            f"{probe.HOME}/roomsAndDevices",
            TOPOLOGY,
            fixture="rooms_and_devices.json",
            home=815,
        ),
        _exchange(
            "hops schedule",
            probe.HOME + "/rooms/{room}/schedule",
            {"roomId": 7, "otherRoomIds": []},
            fixture="schedule.json",
            home=815,
            room=7,
        ),
    ]


def test_verification_url_has_user_code_and_client_id() -> None:
    """The printed sign-in URL works even though tado's own one lacks client_id (#76)."""
    url = probe.verification_url(
        "https://login.tado.com/oauth2/device?user_code=OLD", "XY12"
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://login.tado.com/oauth2/device?")
    assert query == {"user_code": ["XY12"], "client_id": [probe.CLIENT_ID]}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://login.tado.com/oauth2/device?user_code=XY12", False),
        (
            f"https://login.tado.com/oauth2/device?user_code=XY12&client_id={probe.CLIENT_ID}",
            True,
        ),
        (None, None),
    ],
)
def test_has_client_id(url: str | None, expected: bool | None) -> None:
    """The finding records only whether tado's complete URI carries client_id."""
    assert probe.has_client_id(url) is expected


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://hops.tado.com/homes/1/rooms/1/manualControl"),
        ("DELETE", "https://hops.tado.com/homes/1/rooms/1/manualControl"),
        ("PATCH", "https://hops.tado.com/homes/1/roomsAndDevices/devices/VA1"),
        ("PUT", "https://my.tado.com/api/v2/homes/1/presenceLock"),
        ("POST", "https://login.tado.com.example.invalid/oauth2/token"),
        ("POST", "https://login.tado.com/other"),
    ],
)
def test_only_get_leaves_the_sign_in_host(method: str, url: str) -> None:
    """Writes are refused before anything is sent."""
    with pytest.raises(probe.ProbeError, match="only reads"):
        probe._http(method, url)  # noqa: SLF001


def test_token_meta_keeps_no_token() -> None:
    """Of a token response only expires_in, token_type and a flag survive."""
    body = load_fixture("token.json")
    meta = probe.token_meta(body)
    assert meta == {
        "expires_in": body["expires_in"],
        "token_type": body["token_type"],
        "refresh_token_returned": True,
    }
    assert body["access_token"] not in json.dumps(meta)
    assert body["refresh_token"] not in json.dumps(meta)
    assert probe.token_meta(None)["refresh_token_returned"] is False


def test_write_private_creates_owner_only_file(tmp_path: Path) -> None:
    """A new token file or raw copy is created with mode 0600."""
    path = tmp_path / "tokens.json"
    probe.write_private(path, "first")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "first"


def test_write_private_tightens_an_existing_file(tmp_path: Path) -> None:
    """An existing world-readable file is made private before it is rewritten."""
    path = tmp_path / "raw.json"
    path.write_text("old content that is longer", encoding="utf-8")
    path.chmod(0o644)
    probe.write_private(path, "new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"perday";r=17833', {"r": 17833}),
        ('"perday";r=0;t=7082', {"r": 0, "t": 7082}),
        ('"perday";q=20000;w=86400', {"q": 20000, "w": 86400}),
        ('"other", "perday";r=5', {"r": 5}),
        ("garbage", {}),
        (None, {}),
    ],
)
def test_parse_ratelimit(value: str | None, expected: dict[str, int]) -> None:
    """Header parameters are read as integers; garbage gives nothing."""
    assert probe.parse_ratelimit(value) == expected


@pytest.mark.parametrize(
    ("zone", "expected"),
    [("Europe/Berlin", date(2026, 10, 31)), ("Pacific/Honolulu", date(2026, 10, 30))],
)
def test_home_today_uses_the_home_time_zone(zone: str, expected: date) -> None:
    """Today follows the home, not the computer running the probe."""
    now = datetime(2026, 10, 30, 23, 30, tzinfo=UTC)
    assert probe.home_today(zone, now) == expected


@pytest.mark.parametrize("zone", [None, "", "Not/AZone", "../etc/passwd"])
def test_home_today_falls_back_to_the_local_zone(zone: str | None) -> None:
    """Without a usable zone the local date is used."""
    now = datetime(2026, 10, 30, 23, 30, tzinfo=UTC)
    assert probe.home_today(zone, now) == now.astimezone().date()


def test_planned_requests() -> None:
    """The cost shown before sending matches what the run sends."""
    fixed = 1 + 2 + len(probe.HOME_READS) + probe.DELTA_REQUESTS
    assert probe.planned_requests(homes=2, rooms=6, history=False) == fixed + 6 * 3
    assert probe.planned_requests(homes=2, rooms=6, history=True) == fixed + 6 * 4
    assert probe.DELTA_REQUESTS == 17


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        ([2, 2], "likely"),
        ([1, 1], "unlikely"),
        ([2, 3], "inconclusive"),
        ([1, 2], "inconclusive"),
        ([None, 2], "inconclusive"),
        ([], "inconclusive"),
    ],
)
def test_classify_delta(deltas: list[int | None], expected: str) -> None:
    """Only agreeing runs decide whether a host counts."""
    assert probe.classify_delta(deltas) == expected


def test_pseudonyms_are_stable_per_kind() -> None:
    """The same real value always gets the same token; kinds count separately."""
    anonymizer = probe.Anonymizer()
    assert anonymizer.token("home", 815) == "home#1"
    assert anonymizer.token("room", 815) == "room#1"
    assert anonymizer.token("home", 4711) == "home#2"
    assert anonymizer.token("home", 815) == "home#1"
    assert anonymizer.token("room", None) is None


def test_serial_pseudonyms_use_the_device_type() -> None:
    """Serials become type#n once the topology is known."""
    anonymizer = probe.Anonymizer()
    anonymizer.learn_topology(TOPOLOGY)
    assert anonymizer.token("serial", "VA9988776655") == "VA04#1"
    assert anonymizer.token("serial", "VA7766554433") == "VA04#2"
    assert anonymizer.token("serial", "SU1122334455") == "SU04#1"
    assert anonymizer.token("serial", "TR0000000301") == "TR#1"
    flow = anonymizer.anonymize(load_fixture("flow_temperature.json"))
    assert flow["openThermDeviceSerialNumber"] == "TR#1"


def test_anonymize_account() -> None:
    """Personal fields are dropped; ids and names become tokens."""
    result = probe.Anonymizer().anonymize(ACCOUNT, "user")
    assert result == {
        "id": "user#1",
        "name": "name#1",
        "homes": [
            {"id": "home#1", "name": "name#2"},
            {"id": "home#2", "name": "name#3"},
        ],
    }


def test_anonymize_keeps_shapes_and_masks_free_text() -> None:
    """Enums, clock times, dates, numbers and booleans stay; free text is masked."""
    payload = {
        "errors": [{"code": "not.found", "title": "Timezone of home 815 not found"}],
        "start": "06:30",
        "at": "2026-10-30T09:55:00.123Z",
        "day": "2026-10-30 00:00:00",
        "state": "NEXT_TIME_BLOCK",
        "unit": "kWh",
        "zone": "Europe/Berlin",
        "firmware": "280.1",
        "value": 21.5,
        "on": True,
        "location": {"bearingFromHome": {"degrees": 93.0}, "atHome": False},
    }
    result = probe.Anonymizer().anonymize(payload)
    assert result["errors"] == [{"code": "not.found", "title": "<text len=30>"}]
    assert result["location"] == {"bearingFromHome": {"degrees": 0.0}, "atHome": False}
    for key in (
        "start",
        "at",
        "day",
        "state",
        "unit",
        "zone",
        "firmware",
        "value",
        "on",
    ):
        assert result[key] == payload[key]


def test_anonymize_zones_like_rooms() -> None:
    """A minder zone id and the equal room id get the same token."""
    anonymizer = probe.Anonymizer()
    rooms = anonymizer.anonymize(ROOMS, "room")
    minder = anonymizer.anonymize({"runningTimes": [{"zones": [{"id": 42}]}]})
    assert rooms[0]["id"] == minder["runningTimes"][0]["zones"][0]["id"] == "room#1"


def test_report_holds_no_personal_data() -> None:
    """The shareable report has pseudonymized URLs and bodies only."""
    exchanges = _account_exchanges()
    report = probe.build_report(exchanges, {"status_429_seen": False}, 815)
    text = json.dumps(report, ensure_ascii=False)
    for value in (*PERSONAL, "Hauptstrasse", "Kassel", "51.3", "4711", "815", "VA7766"):
        assert value not in text
    urls = [request["url"] for request in report["requests"]]
    assert urls[2] == "https://my.tado.com/api/v2/homes/home#1"
    assert urls[5] == "https://hops.tado.com/homes/home#1/rooms/room#2/schedule"
    assert report["findings"] == {"status_429_seen": False}
    assert report["probe_version"] == probe.PROBE_VERSION


def test_fixtures_follow_the_test_fixture_format() -> None:
    """--emit-fixtures renumbers ids, names and serials like tests/fixtures."""
    fixtures = probe.build_fixtures(_account_exchanges(), 815)
    assert sorted(fixtures) == [
        "home_1000001.json",
        "home_1000002.json",
        "me.json",
        "rooms.json",
        "rooms_and_devices.json",
        "schedule.json",
    ]
    assert fixtures["me.json"]["homes"] == [
        {"id": 1000001, "name": "Haus A"},
        {"id": 1000002, "name": "Haus B"},
    ]
    assert fixtures["me.json"]["email"] == "user@example.invalid"
    assert fixtures["me.json"]["name"] == "Test User"
    home = fixtures["home_1000002.json"]
    assert (home["id"], home["name"], home["generation"]) == (
        1000002,
        "Haus B",
        "LINE_X",
    )
    assert home["geolocation"] == {"latitude": 50.0, "longitude": 8.0}
    assert home["address"]["city"] == "Musterstadt"
    assert [(room["id"], room["name"]) for room in fixtures["rooms.json"]] == [
        (1, "Room 1"),
        (2, "Room 2"),
    ]
    topology = fixtures["rooms_and_devices.json"]
    assert [room["roomName"] for room in topology["rooms"]] == ["Room 1", "Room 2"]
    assert [d["serialNumber"] for d in topology["rooms"][0]["devices"]] == [
        "VA0000000101",
        "SU0000000101",
    ]
    assert topology["rooms"][1]["devices"][0]["serialNumber"] == "VA0000000102"
    assert topology["otherDevices"][0]["serialNumber"] == "IB0000000100"
    assert fixtures["schedule.json"]["roomId"] == 2
    text = json.dumps(fixtures, ensure_ascii=False)
    for value in PERSONAL:
        assert value not in text


def test_fixtures_skip_failed_requests() -> None:
    """Only 2xx payloads become fixtures."""
    exchanges = [
        _exchange(
            "hops flowTemperatureOptimization",
            f"{probe.HOME}/settings/flowTemperatureOptimization",
            {"errors": []},
            status=404,
            fixture="flow_temperature.json",
            home=815,
        )
    ]
    assert probe.build_fixtures(exchanges, 815) == {}


def _fixture_exchanges() -> list[Any]:
    exhausted = {"ratelimit": '"perday";r=0;t=7082'}
    return [
        _exchange(
            "my home", probe.MY_HOME, load_fixture("home_1000001.json"), home=1000001
        ),
        _exchange(
            "my home", probe.MY_HOME, load_fixture("home_1000002.json"), home=1000002
        ),
        _exchange(
            "hops rooms",
            f"{probe.HOME}/rooms",
            load_fixture("rooms.json"),
            home=1000002,
        ),
        _exchange(
            "hops roomsAndDevices",
            f"{probe.HOME}/roomsAndDevices",
            load_fixture("rooms_and_devices.json"),
            home=1000002,
        ),
        _exchange(
            "minder runningTimes",
            probe.MINDER + "/homes/{home}/runningTimes",
            load_fixture("running_times.json"),
            headers={},
            home=1000002,
        ),
        _exchange(
            "hops holidayMode",
            f"{probe.HOME}/settings/holidayMode",
            load_fixture("holiday_mode.json"),
            home=1000002,
        ),
        _exchange(
            "hops flowTemperatureOptimization",
            f"{probe.HOME}/settings/flowTemperatureOptimization",
            None,
            status=404,
            home=1000002,
        ),
        _exchange("eiq tariffs", probe.EIQ + "/x", {"tariffs": []}, headers={}),
        _exchange("eiq meterReadings", probe.EIQ + "/y", [], headers={}),
        _exchange(
            "my state", probe.MY_HOME + "/state", None, status=429, headers=exhausted
        ),
    ]


def test_findings_from_the_test_fixtures() -> None:
    """The findings block answers the live-check questions from raw data."""
    findings = probe.compute_findings(
        _fixture_exchanges(),
        login={"verification_uri_complete_has_client_id": False},
        deltas={"hops": [2, 2], "minder": [1, 1], "eiq": [1, 3], "login": [None, 1]},
    )
    assert findings["verification_uri_complete_has_client_id"] is False
    assert findings["hosts_counting"] == {
        "hops": "likely",
        "minder": "unlikely",
        "eiq": "inconclusive",
        "login": "inconclusive",
    }
    assert findings["ratelimit_header_hosts"] == {
        "eiq": False,
        "minder": False,
        "my": True,
        "hops": True,
    }
    assert findings["t_with_r_gt_0"] is False
    assert findings["status_429_seen"] is True
    assert findings["generations"] == ["LINE_X", "PRE_LINE_X"]
    assert findings["supports_flow_temperature"] is False
    assert findings["air_comfort_eligible"] is True
    assert findings["flow_temperature_status"] == 404
    assert findings["day_report_status"] is None
    assert findings["duration_present_when_not_timer"] is False
    assert findings["termination_types_seen"] == ["MANUAL", "NEXT_TIME_BLOCK", "TIMER"]
    assert {"DEPLETED", "LOW", "NORMAL"} <= set(findings["battery_values_seen"])
    assert findings["device_fields_by_type"]["SU04"]["childLockEnabled"] is False
    assert findings["device_fields_by_type"]["VA04"]["childLockEnabled"] is True
    assert findings["minder_ids_equal_room_ids"] is True
    assert findings["holiday_mode_keys"] == sorted(load_fixture("holiday_mode.json"))
    assert findings["tariffs_shape"] == "wrapped"
    assert findings["meter_readings_shape"] == "list"
    assert findings["dhw_capable"] is None
    text = json.dumps(findings)
    for value in ("1000002", "Haus", "VA0000", "Wohnzimmer"):
        assert value not in text


def test_findings_without_data() -> None:
    """A run stopped before any home data still yields a findings block."""
    findings = probe.compute_findings([], login={}, deltas={})
    assert findings["status_429_seen"] is False
    assert findings["t_with_r_gt_0"] is None
    assert findings["duration_present_when_not_timer"] is None
    assert findings["minder_ids_equal_room_ids"] is None
    assert findings["tariffs_shape"] is None
    assert findings["hosts_counting"] == {}


def test_t_with_r_above_zero_is_detected() -> None:
    """A t= next to a positive r= is the reset time in normal operation."""
    exchanges = [
        _exchange(
            "me", f"{probe.MY}/me", {}, headers={"ratelimit": '"perday";r=5;t=60'}
        )
    ]
    findings = probe.compute_findings(exchanges, login={}, deltas={})
    assert findings["t_with_r_gt_0"] is True


def test_declining_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without y or --yes the probe stops before signing in."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert probe.main(["--max-rooms", "6"]) == 1
    out = capsys.readouterr().out
    expected = probe.planned_requests(homes=2, rooms=6, history=False)
    assert f"up to {expected} requests" in out
    assert "Nothing sent." in out


def test_options() -> None:
    """The command line matches the documented contract."""
    args = probe.parse_args(
        [
            "--home-id",
            "815",
            "--out",
            "out.json",
            "--token-file",
            "t.json",
            "--keep-raw",
            "raw.json",
            "--emit-fixtures",
            "fx",
            "--max-rooms",
            "6",
            "--history",
            "--yes",
        ]
    )
    assert args.home_id == 815
    assert args.keep_raw == Path("raw.json")
    assert args.emit_fixtures == Path("fx")
    assert (args.max_rooms, args.history, args.yes) == (6, True, True)


class FakeTado:
    """Answers the probe's requests in place of the tado cloud."""

    def __init__(self) -> None:
        """Start with a full quota."""
        self.remaining = 20000
        self.requests: list[tuple[str, str]] = []
        self.token = load_fixture("token.json")

    def __call__(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        token: str | None = None,
        timeout: float = 30,
    ) -> Any:
        """Return a canned response; hops and my.tado.com use up the quota."""
        self.requests.append((method, url))
        host = urllib.parse.urlsplit(url).netloc
        path = urllib.parse.urlsplit(url).path
        if host == "login.tado.com":
            if path.endswith("device_authorize"):
                return probe.Response(200, {}, load_fixture("device_authorize.json"))
            return probe.Response(200, {}, self.token)
        assert method == "GET"
        assert token == self.token["access_token"]
        if host in {"hops.tado.com", "my.tado.com"}:
            self.remaining -= 1
        headers = {"ratelimit": f'"perday";r={self.remaining}'}
        status, payload = self._answer(path)
        return probe.Response(status, headers, payload)

    def _answer(self, path: str) -> tuple[int, Any]:
        answers: dict[str, Any] = {
            "/api/v2/me": ACCOUNT,
            "/api/v2/homes/815": HOME_X,
            "/api/v2/homes/4711": HOME_V3,
            "/homes/815/rooms": ROOMS,
            "/homes/815/roomsAndDevices": TOPOLOGY,
        }
        if path in answers:
            return 200, answers[path]
        if path.endswith("flowTemperatureOptimization"):
            return 404, {"errors": [{"code": "not.found", "title": "no boiler"}]}
        return 200, {}


def test_full_run_against_a_fake_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A whole run writes the report, the private copies and the fixtures."""
    fake = FakeTado()
    monkeypatch.setattr(probe, "_http", fake)
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)
    out, raw, tokens, fixtures = (
        tmp_path / "report.json",
        tmp_path / "raw.json",
        tmp_path / "tokens.json",
        tmp_path / "fixtures",
    )
    code = probe.main(
        [
            "--yes",
            "--out",
            str(out),
            "--keep-raw",
            str(raw),
            "--token-file",
            str(tokens),
            "--emit-fixtures",
            str(fixtures),
        ]
    )
    assert code == 0
    assert all(
        method == "GET" for method, url in fake.requests if "login.tado.com" not in url
    )
    api_requests = [url for _, url in fake.requests if "login.tado.com" not in url]
    assert (
        len(api_requests) == probe.planned_requests(homes=2, rooms=2, history=False) - 2
    )
    printed = capsys.readouterr().out
    assert "client_id=" in printed
    assert f"Delete {tokens}" in printed

    report = json.loads(out.read_text(encoding="utf-8"))
    secret_values = (fake.token["access_token"], fake.token["refresh_token"], *PERSONAL)
    for value in secret_values:
        assert value not in out.read_text(encoding="utf-8")
    findings = report["findings"]
    assert findings["hosts_counting"] == {
        "hops": "likely",
        "minder": "unlikely",
        "eiq": "unlikely",
        "login": "unlikely",
    }
    assert findings["verification_uri_complete_has_client_id"] is False
    assert findings["flow_temperature_status"] == 404
    assert findings["open_window_activated_false_seen"] is True

    assert stat.S_IMODE(tokens.stat().st_mode) == 0o600
    assert json.loads(tokens.read_text(encoding="utf-8")) == {
        "refresh_token": fake.token["refresh_token"]
    }
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    assert fake.token["access_token"] not in raw.read_text(encoding="utf-8")
    assert "Bad" in raw.read_text(encoding="utf-8")
    assert (fixtures / "home_1000002.json").is_file()
    assert not (fixtures / "flow_temperature.json").exists()


def test_quota_exhaustion_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 ends the run with a non-zero code and keeps its headers."""
    fake = FakeTado()

    def exhausted(method: str, url: str, **kwargs: Any) -> Any:
        if "login.tado.com" in url:
            return fake(method, url, **kwargs)
        return probe.Response(429, {"ratelimit": '"perday";r=0;t=7082'}, None)

    monkeypatch.setattr(probe, "_http", exhausted)
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)
    out = tmp_path / "report.json"
    assert probe.main(["--yes", "--out", str(out)]) == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["findings"]["status_429_seen"] is True
    assert report["requests"][-1]["headers"] == {"ratelimit": '"perday";r=0;t=7082'}

#!/usr/bin/env python3
"""Read-only probe of the tado X cloud API for the tado_x integration.

Logs in with the tado device-code flow in the terminal, sends GET requests
only, and writes the *shape* of every response to a JSON file. Names, ids,
serial numbers, coordinates and free text are replaced by placeholders, so
the file can be shared to check the integration's test fixtures against the
real API.

Standard library only. Python 3.11+.

    python3 live_probe.py                     # pick the tado X home interactively
    python3 live_probe.py --home-id 123456    # probe one home
    python3 live_probe.py --keep-raw          # also write an unredacted copy (do not share it)

Tokens stay in memory unless --token-file is given (the file is created with
mode 0600 and holds the rotating refresh token; delete it afterwards).
Expect about 15 + 3 x rooms requests against the daily quota.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
MY_API = "https://my.tado.com/api/v2"
HOPS_API = "https://hops.tado.com"
MINDER_API = "https://minder.tado.com/v1"
USER_AGENT = "ha-tado-x-live-probe/1.0"

SENSITIVE_KEY = re.compile(
    r"(^id$|Id$|^serial|serialNo|shortSerial|name$|Name$|email|username|address|"
    r"zip|city|phone|latitude|longitude|^lat$|^lon$|geolocation|token|code$|"
    r"deviceMetadata|locale|contactDetails|partner|skills)",
    re.IGNORECASE,
)
ENUM_LIKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CLOCK = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?$")
RATE_HEADERS = ("ratelimit-policy", "ratelimit", "retry-after", "x-ratelimit-reset")


class ProbeError(Exception):
    """A request failed in a way the probe cannot continue from."""


def _http(
    method: str, url: str, *, data: dict[str, str] | None = None, token: str | None = None
) -> tuple[int, dict[str, str], Any]:
    if method != "GET" and not url.startswith("https://login.tado.com/"):
        raise ProbeError(f"refusing non-GET request to {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, resp_headers, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        status, resp_headers, raw = err.code, dict(err.headers), err.read()
    text = raw.decode("utf-8", "replace")
    try:
        payload: Any = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = text[:500]
    return status, {k.lower(): v for k, v in resp_headers.items()}, payload


def login(token_file: str | None) -> dict[str, Any]:
    """Return a token set, from --token-file if it still works, else via device code."""
    if token_file and os.path.exists(token_file):
        with open(token_file, encoding="utf-8") as fh:
            refresh = json.load(fh).get("refresh_token")
        if refresh:
            status, _, body = _http(
                "POST",
                TOKEN_URL,
                data={"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh},
            )
            if status == 200:
                _save_tokens(token_file, body)
                return body
            print(f"Stored refresh token rejected (HTTP {status}), starting a new login.")

    status, _, body = _http(
        "POST", DEVICE_AUTH_URL, data={"client_id": CLIENT_ID, "scope": "offline_access"}
    )
    if status != 200:
        raise ProbeError(f"device_authorize failed: HTTP {status} {body}")
    url = body.get("verification_uri_complete") or body.get("verification_uri")
    print("\nOpen this URL, log in to tado and confirm the code:")
    print(f"  {url}")
    print(f"  code: {body.get('user_code')}\n")
    interval = int(body.get("interval", 5))
    deadline = time.monotonic() + int(body.get("expires_in", 300))
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, _, tok = _http(
            "POST",
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "device_code": body["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if status == 200:
            if token_file:
                _save_tokens(token_file, tok)
            print("Logged in.\n")
            return tok
        error = (tok or {}).get("error") if isinstance(tok, dict) else None
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise ProbeError(f"login failed: HTTP {status} {tok}")
    raise ProbeError("login timed out")


def _save_tokens(path: str, tokens: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"refresh_token": tokens.get("refresh_token")}, fh)


def redact(value: Any, key: str = "") -> Any:
    """Keep structure, enum-like strings, clock times and numbers; hide identifying data."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value[:50]]
    if value is None or isinstance(value, bool):
        return value
    if SENSITIVE_KEY.search(key):
        return f"<{type(value).__name__}>"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if ENUM_LIKE.match(value) or CLOCK.match(value):
            return value
        if ISO_TS.match(value):
            return "<timestamp>"
        return f"<str len={len(value)}>"
    return f"<{type(value).__name__}>"


class Probe:
    def __init__(self, access_token: str, keep_raw: bool) -> None:
        self._token = access_token
        self._keep_raw = keep_raw
        self.results: list[dict[str, Any]] = []
        self.raw: list[dict[str, Any]] = []

    def get(self, label: str, url: str) -> Any:
        status, headers, payload = _http("GET", url, token=self._token)
        shown_url = re.sub(r"/\d{3,}(?=/|$|\?)", "/<id>", url)
        shown_url = re.sub(r"/[A-Z]{2}\d{6,}", "/<serial>", shown_url)
        self.results.append(
            {
                "label": label,
                "url": shown_url,
                "status": status,
                "rate_limit_headers": {h: headers[h] for h in RATE_HEADERS if h in headers},
                "body": redact(payload),
            }
        )
        if self._keep_raw:
            self.raw.append({"label": label, "url": url, "status": status, "body": payload})
        print(f"  {status}  {label}")
        if status == 429:
            raise ProbeError("daily API quota exhausted (HTTP 429); try again after the reset")
        return payload if 200 <= status < 300 else None


def choose_home(probe: Probe, home_id: int | None) -> int:
    me = probe.get("me", f"{MY_API}/me") or {}
    homes = me.get("homes", [])
    candidates = []
    for home in homes:
        detail = probe.get(f"home {home['id']} detail", f"{MY_API}/homes/{home['id']}") or {}
        generation = detail.get("generation")
        candidates.append((home["id"], home.get("name", ""), generation))
    if home_id is not None:
        return home_id
    x_homes = [c for c in candidates if c[2] == "LINE_X"]
    if len(x_homes) == 1:
        print(f"\nUsing the only tado X home: {x_homes[0][1]!r}\n")
        return x_homes[0][0]
    print("\nHomes on this account:")
    for idx, (_, name, generation) in enumerate(candidates):
        print(f"  [{idx}] {name!r}  generation={generation}")
    choice = int(input("Number of the tado X home to probe: "))
    return candidates[choice][0]


def run(args: argparse.Namespace) -> int:
    tokens = login(args.token_file)
    probe = Probe(tokens["access_token"], args.keep_raw)
    print("Requests:")
    try:
        home = choose_home(probe, args.home_id)
        today = dt.date.today().isoformat()
        hops = f"{HOPS_API}/homes/{home}"
        rooms = probe.get("hops rooms", f"{hops}/rooms") or []
        probe.get("hops roomsAndDevices", f"{hops}/roomsAndDevices")
        probe.get("hops features", f"{hops}/features")
        probe.get("hops airComfort", f"{hops}/airComfort")
        probe.get("hops holidayMode", f"{hops}/settings/holidayMode")
        probe.get("hops flowTemperatureOptimization", f"{hops}/settings/flowTemperatureOptimization")
        probe.get("my home state", f"{MY_API}/homes/{home}/state")
        probe.get("my weather", f"{MY_API}/homes/{home}/weather")
        probe.get("my mobileDevices", f"{MY_API}/homes/{home}/mobileDevices")
        probe.get("minder runningTimes", f"{MINDER_API}/homes/{home}/runningTimes?from={today}&to={today}")
        for room in rooms[: args.max_rooms]:
            rid = room.get("id")
            probe.get(f"hops room {rid}", f"{hops}/rooms/{rid}")
            probe.get(f"hops room {rid} schedule", f"{hops}/rooms/{rid}/schedule")
            probe.get(f"hops room {rid} away setting", f"{hops}/settings/away/rooms/{rid}")
            if args.history:
                probe.get(f"my zone {rid} dayReport", f"{MY_API}/homes/{home}/zones/{rid}/dayReport?date={today}")
    except ProbeError as err:
        print(f"\nStopped early: {err}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or f"tado_x_probe_{stamp}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"probe_version": 1, "results": probe.results}, fh, indent=2)
    print(f"\nRedacted result written to {out}. Check it, then share it.")
    if args.keep_raw:
        raw_out = out.replace(".json", ".RAW-do-not-share.json")
        fd = os.open(raw_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(probe.raw, fh, indent=2)
        print(f"Unredacted copy written to {raw_out} (contains personal data, do not share).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home-id", type=int, help="tado home id to probe (default: auto-detect the tado X home)")
    parser.add_argument("--out", help="output file (default: tado_x_probe_<timestamp>.json)")
    parser.add_argument("--token-file", help="keep the refresh token in this file (mode 0600) between runs")
    parser.add_argument("--max-rooms", type=int, default=20, help="probe at most this many rooms")
    parser.add_argument("--no-history", dest="history", action="store_false", help="skip dayReport requests")
    parser.add_argument("--keep-raw", action="store_true", help="also write an unredacted copy (private)")
    try:
        return run(parser.parse_args())
    except ProbeError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

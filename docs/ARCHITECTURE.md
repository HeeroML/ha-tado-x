# Architecture of tado_x 1.9

This is a condensed version of the v1.9.0 design. It describes how the integration is built and why. The API facts it relies on are in [API.md](API.md).

## Goals

- Control a tado X home from Home Assistant through the tado cloud API, next to the core `tado` integration that serves V3+ homes on the same account.
- Stay inside the account's daily API quota, which all clients of the account share.
- Never lose the sign-in: refresh tokens rotate on every use, so a lost rotation means a new sign-in.
- Keep the upgrade from 1.8.11 in place: same domain, same unique ids, rollback possible.
- Test everything through HTTP, without patching integration code.

## Module map

```text
custom_components/tado_x/
├── client/            pure tado client: stdlib + aiohttp + yarl, no Home Assistant imports
│   ├── const.py       hosts, public client id, timeouts
│   ├── errors.py      error taxonomy
│   ├── enums.py       open StrEnums; unknown values parse to None with one warning
│   ├── models.py      frozen dataclasses with from_api() parsers and to_api() writers
│   ├── ratelimit.py   ratelimit header parser, Priority, QuotaTracker, CallCounter
│   ├── auth.py        TokenStore protocol, TokenManager, DeviceCodeFlow
│   ├── http.py        Transport: quota gate, bearer, 401 retry, error mapping
│   ├── schedule.py    schedule validation, day report summary
│   └── api.py         TadoXClient (account), TadoXHome (one home)
├── budget.py          PURE: polling plan, throttle factor, due endpoints
├── termination.py     PURE: which termination a manual change gets
├── coordinator.py     three DataUpdateCoordinators: rooms, home, devices
├── quota.py           QuotaMonitor: plan, throttle, API status, quota issues
├── cache.py           SnapshotCache: raw payloads and quota state in a Store
├── token_store.py     ConfigEntryTokenStore: tokens in entry.data
├── devices.py         device registry: identifiers, names, via_device_id, stale removal
├── entity.py          entity base classes, error mapping, dynamic entity adding
├── issues.py          repair issues
├── diagnostics.py     redacted diagnostics
├── migration.py       config entry 1.1 → 1.2
├── config_flow.py     device-code flow, home selection, reauth, reconfigure, options
├── services.py        actions, registered once in async_setup
└── <platform>.py      binary_sensor, button, climate, device_tracker, number, select, sensor, switch
```

Boundaries are enforced: `client/ruff.toml` bans `homeassistant` imports in the client, and `tests/test_architecture.py` checks the client, `budget.py` and `termination.py` for Home Assistant imports, checks that the client does not import its parent package, and rejects `from __future__ import annotations` everywhere.

## Client

### Errors

```text
TadoXError
├── TadoXAuthError              invalid_grant and similar, or a 401 after one refresh → reauth
├── TadoXTransientError         retry later, never reauth
│   ├── TadoXConnectionError    no response: DNS, TLS, timeout, reset
│   ├── TadoXServerError        5xx, or a 429 from the login server
│   └── TadoXRateLimitError     API 429, or the local quota gate refused
├── TadoXRequestError           other 4xx, with tado's code and title
│   ├── TadoXForbiddenError     403
│   └── TadoXNotFoundError      404
│       └── TadoXHomeNotXError  hops 404 "Timezone of home … not found"
├── TadoXParseError             2xx with a payload that fails validation
└── TadoXDeviceFlowError        sign-in expired or denied
```

### Request path (`Transport.request`)

1. The quota gate decides first. While it is closed, the request fails locally and no HTTP is sent.
2. Get an access token (see below).
3. Send with a timeout. Every attempt counts toward the entry's own 24 h call counter.
4. Read the `ratelimit` headers **before** the status, so a 429 still updates the quota state.
5. On a 401, refresh once under the token lock and retry once. A second 401 is an authentication error.
6. Any 2xx is success; the body is parsed only when it is not empty. Cacheable reads are handed to the payload observer (the cache).
7. Map the status to the error taxonomy.

### Tokens (`TokenManager`)

- A fresh access token (more than 60 s left) is returned without a lock, so a start with a valid token sends no refresh.
- Otherwise one lock serializes refreshes per entry. Inside it the manager re-reads the store (another instance may have rotated the token) and checks again, so ten waiting callers cause one refresh.
- The 401 path uses the same lock. If the rejected token was already replaced, no second refresh is sent.
- **Persist first, publish second:** a new token set is saved to the store before any caller can use it.
- Transient refresh failures back off (5 s, 15 s, 60 s, 5 min). While the old access token is still valid and the call is not forced by a 401, the old token is used.
- `invalid_grant` while the store holds a different refresh token means another instance rotated it; the manager adopts it and retries once. Otherwise the failure is sticky until the entry reloads, which reauthentication does.
- Unloading the entry waits for a running refresh, so the next instance reads the rotated token.

`DeviceCodeFlow` builds the verification link from `verification_uri` with `user_code` and `client_id`, polls at the server's interval (at least 5 s), adds 5 s on `slow_down`, and retries transient errors until the code expires. Its sleep and clock are injectable for tests.

### Quota (`QuotaTracker`)

- `ratelimit` headers are parsed case-insensitively; the `perday` member wins.
- `r=0` with `t=`, or a 429 with `t=`, closes the gate until `t` plus 30 s. A 429 without `t=` closes it for 15, 30, 60, then 120 minutes; one request is let through after each step, and the first 2xx resets the backoff.
- With 20 or fewer calls remaining, polls are refused and commands still pass (`Priority.COMMAND`).
- The reset time is known only from `t=`; nothing is guessed.
- The tracker's state is persisted, so a restart during a hold sends nothing.

## Home Assistant layer

### Setup

`async_setup_entry` loads the cache, restores the quota state, builds one client per entry over HA's shared session, and runs the first refresh of the devices, rooms and home coordinators **one after another**, so a 429 in the first one closes the gate before the others send anything. It then starts the quota monitor, registers all devices, and forwards the platforms. No update listener exists; options use `OptionsFlowWithReload`, so writing a rotated token to `entry.data` never reloads the entry.

Each entry owns its client, token manager, quota tracker, coordinators and cache. The only module-level state is the enum warn-once set. Actions are registered once in `async_setup` and look up the target entry on each call, so they always reach the current client after a reload and the right home with several homes.

### Coordinators

| Coordinator | Endpoints | Stale cap during a quota hold |
|---|---|---|
| rooms | `rooms` | 6 h |
| home | `home_state`, `weather`, `air_comfort`, `mobile_devices` | 12 h |
| devices | `topology`, `home_info`, `running_times`, `flow_temperature`, `holiday_mode` | 48 h |

Each endpoint has its own period; on a tick the coordinator fetches only the endpoints that are due and merges them into the previous snapshot. The coordinator's interval is the shortest effective period among its endpoints.

- Required endpoints (`rooms`, `home_state`, `topology`) fail the tick. Optional endpoints keep their previous value and log one warning until they recover, except that a 429 or an authentication error from any endpoint is never swallowed.
- A 404 on flow temperature marks the feature absent; it is re-probed daily. Homes that report no flow temperature support are never polled for it.
- Snapshots are frozen dataclasses with no timestamps, and the coordinators use `always_update=False`, so unchanged polls write no state.

Error mapping:

| Client error | Coordinator | Entity action |
|---|---|---|
| auth | `ConfigEntryAuthFailed` | starts reauth, raises a translated error |
| rate limit | serve the last data (see below) | translated error with the reset time |
| connection, server | `UpdateFailed` | translated error |
| not a tado X home | `ConfigEntryError` plus repair issue | – |
| 404 on an optional endpoint | feature absent | translated error |
| other 4xx, parse | `UpdateFailed` | translated error; model `ValueError`s become `ServiceValidationError` |

### Polling budget (`budget.py`)

`plan_polling` turns the daily call budget into periods:

1. cap = budget, or min(budget, 80 % of the quota limit) once tado has reported one;
2. 90 % of the cap is for polling;
3. the slow endpoints are summed at their base periods; if they need more than half of the polling share, all of them are stretched by the same factor, each up to its maximum;
4. rooms get the rest: `86400 / (poll − slow)`, rounded up to 5 s, between 30 s and 1 h.

| Endpoint | Base period | Maximum |
|---|---|---|
| rooms | from the plan | 1 h |
| home_state | 5 min | 6 h |
| weather, air_comfort | 15 min | 6 h |
| mobile_devices | 5 min | 6 h |
| topology | 15 min | 1 day |
| home_info | 1 day | 1 day |
| running_times | 1 h | 1 day |
| flow_temperature, holiday_mode | 15 min | 1 day |

`throttle_factor` compares the plan with the account's remaining calls: the effective reserve is min(quota reserve, 10 % of the limit); the allowance is (remaining − reserve) split across the account's loaded entries; the need is the planned calls for the time until the reset (a full day if `t=` is unknown). The factor `need / allowance` is clamped to 1–30 (`ok` at 1, `throttled` above). At or below the reserve the mode is `conserving`: required endpoints at their maximum period, optional endpoints paused, commands allowed. A closed gate is `blocked`.

For a home with 6 rooms, the defaults give rooms every 45 s and 2,521 planned calls a day; the README shows the full table.

### Quota hold and cache (#62)

`SnapshotCache` keeps the last raw payload of each cacheable endpoint, the quota state, the call counter and the flow temperature status in `.storage/tado_x.<entry_id>`, written at most every 10 minutes and on unload. Phone data is never stored. A seed is parsed by the same `from_api` functions as live data, and a slice that fails to parse is dropped.

| Situation | Result |
|---|---|
| gate closed or 429, data younger than the stale cap | keep the data; entities stay available; `api_status` = `rate_limited`; repair issue; zero HTTP until the reset |
| same, data older than the cap | `UpdateFailed`; entities unavailable |
| first refresh, gate closed, no cache | `ConfigEntryNotReady`; HA's retries hit the persisted gate and send nothing |
| network, 5xx or parse error | standard HA behaviour, never served from the cache |
| first 2xx after the reset | gate open, issue deleted, live data |

This is the one deliberate deviation from the `entity-unavailable` quality rule.

### Writes

Write platforms use `PARALLEL_UPDATES = 1`. A write:

1. computes the payload (termination from `termination.py`);
2. sends it with command priority;
3. applies an optimistic patch to the coordinator data, so the UI updates at once;
4. for room commands, requests a debounced refresh (5 s cooldown), so a burst costs one read.

A pending patch is re-applied to later reads until the server agrees or 60 s pass, which hides eventually consistent reads.

### Termination (#94)

`resolve_termination(policy, timer, room_default, override)`:

1. an explicit termination from `set_climate_timer` wins;
2. policy `manual`, `next_time_block` or `timer` gives that termination;
3. policy `follow_app` (the default) uses the room's setting from the tado app (`deviceManualControlTermination`), read at write time; HA keeps no copy;
4. without one, `next_time_block`. There is no hard-coded 30-minute timer.

Turning a room off uses `manual` unless the option `off_follows_policy` is on. A termination is always sent explicitly.

### Devices and entities

`devices.py` registers the home, the bridge, the rooms, the physical devices and the phones, in that order, before the platforms load, and again after every coordinator update. Physical devices are connected via the Bridge X (the home when there is no bridge); rooms and phones via the home. Device names use translation keys with `{room}{index}` placeholders. After a live read, devices of the entry that tado no longer reports are removed; a cached read removes nothing.

Entities carry only the device identifier. `async_add_dynamic_entities` runs each platform's builder at setup and on every coordinator update and adds entities whose unique id it has not seen, so rooms, devices, phones and fields that appear later get entities without a reload.

### Config entry

Version 1, minor version 2. `unique_id` is the home id. `data` holds `home_id`, `home_name`, `user_id` (when known), and the flat `access_token`, `refresh_token` and `token_expiry` (UTC ISO 8601) that `ConfigEntryTokenStore` always writes together. The 1.8 keys (`scan_interval`, `has_auto_assist`, `enable_*`, `api_calls_today`, `api_reset_time`) stay untouched, so 1.8.11 still loads the entry.

The migration from 1.1:

- derives `daily_call_budget` from the old `scan_interval` so the room interval stays about the same (500–20,000), and copies the `enable_*` toggles into options;
- sets the unique id unless another entry already owns the home, in which case it raises a repair issue;
- moves the oldest climate unique id format `{home}_{room}` to `{home}_{room}_climate`;
- raises the `breaking_changes_1_9` notice;
- leaves `data` untouched and is idempotent after a rollback and a second upgrade. A naive `token_expiry` from 1.8 reads as expired and causes one refresh.

### Config flow

The user step starts the device-code flow and shows a progress step with the link and the code while a background task waits for the token; no request blocks the flow. After the sign-in it reads `/me` and each home, keeps `LINE_X` homes and names the others. Every error at this boundary becomes an abort reason. Reauth requires the same tado account; reconfigure switches the account and requires that it has the home. The options flow has three sections (polling, features, control) and shows the plan for the current settings.

## Logging and privacy

- Never logged: tokens, device and user codes, the verification link, the `Authorization` header, login response bodies, e-mail, raw `/me`.
- DEBUG: method, host, path, status, remaining calls, duration; for non-auth errors the first 300 characters of the body.
- WARNING once per episode: quota hold, conserving mode, the first transient refresh failure, an auth failure (error code only), an optional endpoint failing, an unknown enum value.
- Diagnostics redact tokens, user id, e-mail, address, coordinates, contact details and device metadata, and shorten serial numbers.

`tests/test_logging.py` runs the sign-in, setup, rotation, a 401 cycle and a rejected refresh at DEBUG and fails if any fake secret appears in the log.

## Tests

- `tests/tado_mock.py` provides `TadoCloudMock`, a stateful fake of the tado hosts on top of `aioclient_mock`. It rotates tokens (reusing a rotated refresh token gives `invalid_grant`), emits `ratelimit` headers from a counter, injects failures and holds responses for concurrency tests.
- No integration code is patched. Time moves with `freezer`; the device flow gets an injected sleep.
- Coverage gates: 95 % line and branch overall; 100 % line for `client/auth.py`, `client/ratelimit.py`, `client/http.py`, `budget.py`, `termination.py`, `migration.py` and `config_flow.py`.
- `tests/test_translations.py` checks that `en.json` equals `strings.json`, that German is complete, that every key used in code exists, and that placeholders match.

## Decisions worth knowing

| Decision | Reason |
|---|---|
| Three coordinators, one quota gate | endpoints change at very different rates; one gate stops a 429 from turning into three |
| Budget instead of an interval option | the quota is a per-day resource; HA discourages interval options |
| Entry 1.2 with flat token keys | 1.8.11 can still load it, so rollback needs no new sign-in |
| Stale data only during a quota hold | a hold has a known end; outdated room temperatures beyond 6 h are misleading |
| Devices via the Bridge X | it is the routing parent, as in the core tado integration |
| Presets `home`/`away`/`auto` kept | existing automations use them |
| `off` stays manual by default | an "off" that reverts on its own surprises people |
| No learned reset time | sources disagree on when tado resets; only `t=` is trusted |
| HTTP as the only test seam | parsing, tokens, headers and errors run in every test |

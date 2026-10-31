# Architecture of tado_x 1.9

This is a condensed version of the v1.9.0 design, with the observability of 1.9.1 (see [Observability](#observability)). It describes how the integration is built and why. The API facts it relies on are in [API.md](API.md).

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
│   ├── redact.py      log-safe text: redact_text, describe_value, mask_serial
│   ├── telemetry.py   Telemetry, Exchange, ErrorRecord, endpoint templates, WARN_ONCE
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
│   ├── TadoXServerError        5xx, or from the login server a 429 or a 4xx without an OAuth error
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
3. Send with a timeout. Every attempt counts toward the entry's own 24 h call counter and is recorded in the telemetry as an `Exchange`.
4. Read the `ratelimit` headers **before** the status, so a 429 still updates the quota state.
5. On a 401, refresh once under the token lock and retry once. A second 401 is an authentication error.
6. Any 2xx is success, except an Energy IQ answer that carries only a `message`, which is a refusal (`TadoXRequestError`). The body is parsed only when it is not empty. A cacheable read reaches the payload observer (the cache) only after its endpoint parser accepted it, so a malformed answer never replaces the last good payload; home details are cached without contact details, address and location.
7. Map the status to the error taxonomy.

### Tokens (`TokenManager`)

- A fresh access token (more than 60 s left) is returned without a lock, so a start with a valid token sends no refresh.
- Otherwise one lock serializes refreshes per entry. Inside it the manager re-reads the store (another instance may have rotated the token) and checks again, so ten waiting callers cause one refresh.
- The 401 path uses the same lock. If the rejected token was already replaced, no second refresh is sent.
- **Store first, publish second:** a new token set is written to the token store, the config entry data, before any caller can use it. Home Assistant saves config entries with a delay and flushes it to disk about 1 s later; a normal stop writes it out, but a crash within that second loses the rotation, and the next start needs a reauthentication. The save is a compare-and-set against the tokens the manager knew when it decided to refresh, a snapshot that a caller adopting newer tokens during the request does not change: tokens a reauth or reconfigure stored while the request ran win over both its rotation and an OAuth rejection of it, the refresh result is dropped, and stored tokens that are already stale are refreshed instead. After a failed save the new tokens stay in memory and the next refresh saves its successor.
- Transient refresh failures back off (5 s, 15 s, 60 s, 5 min). While the old access token is still valid and the call is not forced by a 401, the old token is used.
- An auth rejection while the store holds tokens someone else wrote after the refresh was decided means the grant was rotated elsewhere; the manager adopts those tokens and refreshes them if they are stale. Otherwise the failure is sticky until the entry reloads, which reauthentication does.
- The refresh request and the save of its rotation run in a task the manager owns, so a cancelled poll never aborts them. Unloading the entry (the client closes first) and stopping Home Assistant (a shutdown job) wait up to 10 s for a running refresh, so the next instance reads the rotated token. A closed client refuses new refreshes but still serves a valid access token.

`DeviceCodeFlow` builds the verification link from `verification_uri` with `user_code` and `client_id`, polls at the server's interval (at least 5 s), adds 5 s on `slow_down`, and retries transient errors until the code expires. Its sleep and clock are injectable for tests.

### Quota (`QuotaTracker`)

- `ratelimit` headers are parsed case-insensitively; the `perday` member wins.
- `r=0` with `t=`, or a 429 with `t=`, closes the gate until `t` plus 30 s. A 429 without `t=` closes it for 15, 30, 60, then 120 minutes; one request is let through after each step, and the first 2xx resets the backoff. A late answer to a request sent before the gate closed does not reopen it.
- With 20 or fewer calls remaining, polls are refused and commands still pass (`Priority.COMMAND`).
- The reset time is known only from `t=`; nothing is guessed.
- A hold whose end tado sent starts with the response that began it. Later responses during the hold, such as the answer to a command, do not move that start; it is dropped when the hold ends or the quota recovers. A hold without a known end has no start.
- The tracker's state, including that start, is persisted, so a restart during a hold sends nothing.

## Home Assistant layer

### Setup

`async_setup_entry` loads the cache, restores the quota state, builds one client per entry over HA's shared session, and runs the first refresh of the devices, rooms and home coordinators **one after another**, so a 429 in the first one closes the gate before the others send anything. It then starts the quota monitor, registers all devices, and forwards the platforms. No update listener exists; options use `OptionsFlowWithReload`, so writing a rotated token to `entry.data` never reloads the entry.

Each entry owns its client, token manager, quota tracker, telemetry, coordinators and cache. The only module-level state is `WARN_ONCE`, the bounded warn-once set of the client. Actions are registered once in `async_setup` and look up the target entry on each call, so they always reach the current client after a reload and the right home with several homes.

### Coordinators

| Coordinator | Endpoints | Stale cap during a quota hold without `t=` |
|---|---|---|
| rooms | `rooms` | 6 h |
| home | `home_state`, `weather`, `air_comfort`, `mobile_devices` | 12 h |
| devices | `topology`, `home_info`, `running_times`, `flow_temperature`, `holiday_mode` | 48 h |

Each endpoint has its own period; on a tick the coordinator fetches only the endpoints that are due and merges them into the previous snapshot. The required endpoint is read first and the optional ones together after it, so a used-up quota costs one request per tick; an optional read the gate refuses after that keeps its value. The coordinator's interval is the shortest effective period among its endpoints. `last_live_update`, which the "… updated" sensors show, moves only when a due endpoint returned a value, and only then does a quota hold end for the coordinator: Home Assistant arms the next poll before the monitor lengthens the interval, so one tick after a slow-down finds nothing due and sends nothing.

A coordinator is named `tado_x <kind> home …NNNN` (`home_label()`, the last four digits of the home id), because Home Assistant logs the name.

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
| other 4xx | `UpdateFailed` `request_rejected` | `request_rejected` with the status and tado's error code |
| parse | `UpdateFailed` `api_error` | `api_error` with the endpoint template; model `ValueError`s become `ServiceValidationError` |

A translated error is built from the status, tado's error code (`safe_code()`) and the endpoint template only, never from the error message, its title, its JSON path or a URL path, which can hold ids, serials or names.

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

`throttle_factor` compares the plan with the account's remaining calls: the effective reserve is min(quota reserve, 10 % of the limit); the allowance is (remaining − reserve) split across the account's loaded entries; the need is the planned calls for the time until the reset (a full day if `t=` is unknown). The factor `need / allowance` is clamped to 1–30 (`ok` at 1, `throttled` above). At or below the reserve the mode is `conserving`: required endpoints at their maximum period, optional endpoints paused, commands allowed. A closed gate is `blocked`. A reset from `t=` that has passed counts as a refilled quota; the monitor re-plans at that reset, and a shorter interval brings a coordinator's pending poll forward instead of waiting out the old timer.

For a home with 6 rooms, the defaults give rooms every 45 s and 2,521 planned calls a day; the README shows the full table.

### Quota hold and cache (#62)

`SnapshotCache` keeps the last raw payload of each cacheable endpoint, the quota state, the call counter and the flow temperature status in `.storage/tado_x.<entry_id>`, written at most every 10 minutes and on unload. Phone data is never stored. A seed is parsed by the same `from_api` functions as live data, and a slice that fails to parse is dropped.

| Situation | Result |
|---|---|
| gate closed or 429, and the data is younger than the stale cap, or the hold ends at a reset from `t=` and the data was younger than the cap when the hold began (the start the quota tracker keeps and persists, so it holds across reloads and restarts; a quota state saved without it counts the hold from the restart, which never extends serving past what the real start allows) | keep the data; entities stay available; `api_status` = `rate_limited`; repair issue; zero HTTP until the reset |
| gate closed or 429, otherwise | `UpdateFailed`; entities unavailable |
| first refresh, gate closed, no cache | `ConfigEntryNotReady`; HA's retries hit the persisted gate and send nothing |
| network, 5xx or parse error | standard HA behaviour, never served from the cache |
| first 2xx after the reset | gate open, issue deleted, live data |

This is the one deliberate deviation from the `entity-unavailable` quality rule.

### Writes

Write platforms use `PARALLEL_UPDATES = 1`. A write:

1. computes the payload (termination from `termination.py`);
2. sends it with command priority;
3. applies an optimistic patch to the coordinator data, so the UI updates at once without restarting the poll timer;
4. for room commands, requests a debounced refresh (5 s cooldown), so a burst costs one read.

A pending patch is re-applied to later reads until the server agrees or 60 s pass, which hides eventually consistent reads. Patches are keyed by what they write (a room's setting, a room's open window, a device field, a room default, presence, flow and holiday settings); a new patch replaces the pending one of its target, so a resume right after a manual target shows the schedule. A quick action patches the setting of every room, so boost, all off and resume schedules each replace the pending room writes.

### Termination (#94)

`resolve_termination(room_id=, policy=, timer=, room_default=, override=)` (keyword-only; the room id names the room in its debug line):

1. an explicit termination from `set_climate_timer` wins;
2. policy `manual`, `next_time_block` or `timer` gives that termination;
3. policy `follow_app` (the default) uses the room's setting from the tado app (`deviceManualControlTermination`), read at write time; HA keeps no copy;
4. without one, `next_time_block`. There is no hard-coded 30-minute timer.

Turning a room off uses `manual` unless the option `off_follows_policy` is on. A termination is always sent explicitly.

### Devices and entities

`devices.py` registers the home, the bridge, the rooms, the physical devices and the phones, in that order, before the platforms load, and again after every coordinator update. Physical devices are connected via the Bridge X (the home when there is no bridge); rooms and phones via the home. Device names use translation keys with `{room}{index}` placeholders. After a live and complete read, devices of the entry that tado no longer reports are removed; a cached read, or an incomplete topology (a room or device that failed to parse, or a missing device list), removes nothing, and a phone list with a phone that failed to parse removes no phone.

Entities carry only the device identifier. `async_add_dynamic_entities` runs each platform's builder at setup and on every coordinator update and adds entities whose unique id it has not seen, so rooms, devices, phones and fields that appear later get entities without a reload. It forgets the unique ids of a device once the device is removed, so a device that returns gets all its entities again.

### Config entry

Version 1, minor version 2. `unique_id` is the home id. `data` holds `home_id`, `home_name`, `user_id` (when known), and the flat `access_token`, `refresh_token` and `token_expiry` (UTC ISO 8601) that `ConfigEntryTokenStore` always writes together. The 1.8 keys (`scan_interval`, `has_auto_assist`, `enable_*`, `api_calls_today`, `api_reset_time`) stay untouched, so 1.8.11 still loads the entry.

The migration from 1.1:

- derives `daily_call_budget` from the old `scan_interval` so the room interval stays about the same (500–20,000), and copies the `enable_*` toggles into options;
- sets the unique id unless another entry already owns the home, in which case it raises a repair issue;
- moves the oldest climate unique id format `{home}_{room}` to `{home}_{room}_climate`;
- raises the `breaking_changes_1_9` notice;
- leaves `data` untouched and is idempotent after a rollback and a second upgrade. A naive `token_expiry` from 1.8 reads as expired and causes one refresh.

Home Assistant migrates an entry only once, so setup redoes two parts: it derives the options again when a v1.8.11 options save emptied them (no `daily_call_budget`), and it gives an entry without unique id its home id once no other entry owns it, deleting `duplicate_home`; otherwise it raises `duplicate_home` again.

### Config flow

The user step starts the device-code flow and shows a progress step with the link and the code while a background task waits for the token; no request blocks the flow. After the sign-in it reads `/me` and each home, keeps `LINE_X` homes and names the others. Every error at this boundary becomes an abort reason. Reauth requires the same tado account; reconfigure switches the account and requires that it has the home. The options flow has three sections (polling, features, control) and shows the plan for the current settings.

## Observability

The goal: a problem in a live home is visible quickly and can be pinned to a request, without log floods and without personal data.

### Telemetry

`client/telemetry.py` is pure (no Home Assistant import). Setup creates one `Telemetry` per entry and hands the same instance to the quota tracker, the token manager and the client; the runtime data carries it. It lives in memory only and is empty after a start or reload.

- `Exchange`: one HTTP attempt, recorded by the transport: time, method, endpoint template, status, duration, attempt (2 for the retry after a 401), quota numbers, outcome (`ok`, `retried` or the error class), the `shape()` of a 2xx body (keys and value types, never values) and a short safe error text. The last 50 are kept.
- `ErrorRecord`: one error with its kind (error class or `quota_gate`), its context, the endpoint template, the status and a redacted message. The last 20 are kept. An error that repeats the newest record (same kind, context, endpoint, status and message) increments its `count` and sets its `last_at` instead, so an outage does not push out older errors. `last_error` feeds the "Last API error" sensors.
- Requests per endpoint template and errors per kind are counted over a rolling 24 hours in minute buckets, like `CallCounter`, so memory stays bounded.
- `endpoint_template()` turns a URL into `GET hops:/homes/{home}/rooms/{room}/schedule`: short host names, ids and serials as placeholders, query keys without values. Templates are the only form in which requests appear in logs, telemetry and entity states.

**Error recording rule.** An error is recorded once, by the innermost layer that knows why the request was made:

| Layer | Context |
|---|---|
| client, token refresh | `token refresh` |
| client, write | `command <endpoint>`, as in `command manualControl` |
| quota tracker, gate closing | `quota` (kind `quota_gate`), once per closing |
| coordinator, failed poll | `poll rooms`, `poll home`, `poll devices` |
| setup | `setup` |
| entity action | `command <entity description key>` |
| action (service) | `service <name>` |

`Telemetry` marks each error object it records (`TadoXError.recorded`) and ignores a marked one, so an outer layer that records an error the client recorded (a failed refresh that also fails a poll, a failed write that an entity action re-raises) adds nothing. It keeps no reference to the error, so its traceback and frames are freed. A write that tado refuses as the command expects (`TadoXHome._command(..., expected=…)`: open window mode without a detected open window, presence `AUTO` without Auto-Assist) is audited as `Write refused` and not recorded; the entity raises its own `ServiceValidationError`. Local refusals and 429 answers during a quota hold are not poll errors; the closing of the gate is the one record. A new layer that catches `TadoXError` records it with its own context through `raise_for_tado_error()` or `record_tado_error()`, never by formatting the exception itself.

Status entities listen to the telemetry, so the error sensors update when an error is recorded, not only on the next poll.

### Log levels

Loggers are `custom_components.tado_x` (Home Assistant layer) and `custom_components.tado_x.client`; setting the first covers both.

| Level | Rule | Examples |
|---|---|---|
| DEBUG | every exchange and every decision | `GET hops:/homes/{home}/rooms -> 200 in 182 ms, quota 18342/20000 left, reset in 7082 s`; 401 retry; refresh backoff; 429 backoff step; local gate refusal; endpoints due per tick; optimistic patch applied or dropped; `Termination of room 3: follow_app -> TIMER 3600 s`; token stored (expiry only); cache load and write (slices and age) |
| INFO | changes of state, once each, and writes | setup summary; first failed token refresh and the recovery; rejected sign-in (OAuth code only); a reauth that a 401 with a freshly refreshed token starts (reason code only, once per entry through `ReauthNotice`, and not when the token manager logged the rejected sign-in); quota hold below the poll floor starts and ends; conserving mode on and off; cached data served and stopped, per coordinator; device or room added after setup or removed; `Write: manualControl room 3: ON 21.0 °C until TIMER 3600 s -> ok (HTTP 204, 312 ms)`, `Write failed: … -> HTTP 422 code=… title=<str len=N>` and, for a refusal the command expects, `Write refused: … -> HTTP 422 …` |
| WARNING | needs attention, rate-limited | quota used up (stops polling); an optional endpoint failing, once per episode; an unknown enum value, an unusable optional field (`Ignoring an unusable <endpoint> field: <path> is <description>`) or an unreadable list item, once per field and path through `WARN_ONCE`, later ones at DEBUG; a home whose generation tado does not report; a failed sign-in in the config flow |
| ERROR | unexpected | unexpected exceptions at the config flow boundary; storing rotated tokens failed |

Budget: a steady home logs nothing at INFO. Routine token refreshes (about every 10 minutes) and polls stay at DEBUG. `test_steady_state_logs_nothing_at_info` runs six hours of polling with refreshes and fails on any INFO line. A new INFO line must describe a transition and must be logged once per transition.

A setup is not a transition. The quota monitor starts from the plan mode and hold that the restored quota state shows, and a coordinator that the restored state refuses at setup logs serving its cache at DEBUG, so setup retries during a hold (every 10 minutes at most, until the reset) and reloads repeat nothing. The end of a restored hold is logged as usual.

`WARN_ONCE` is a bounded set (512 keys, oldest evicted). A field warning names the path and a `describe_value()` of what was there, never the raw value. Unknown extra keys are not warned about; tado sends many fields the integration ignores.

### Privacy rules

These apply to every log line, diagnostics field, entity state and exception message.

- Never: access or refresh tokens, the `Authorization` header, device code, user code, verification link, login.tado.com bodies, e-mail, user name, address, coordinates, contact details, phone names and metadata, room names, home names from the API, full serial numbers, raw response bodies.
- Allowed: HTTP method, endpoint template, status, durations, rate-limit numbers, tado's error `code` (`safe_code()`), tado's error `title` only when it is one of a few known texts without personal data and after `redact_text()` (every run of four or more digits becomes `#`, truncated), otherwise only its length, error class names, enum-like constants (`^[A-Z][A-Z0-9_]{0,39}$`), room ids as the room pseudonym (`room 3`), masked serials (`VA…0101`) and the home label `home …NNNN` (`home_label()`, the last four digits of the home id).
- The config entry title is never logged by the integration nor put into diagnostics: the config flow takes it from tado's name of the home. Log lines, coordinator names and diagnostics use the home label instead. UI texts (repair issues, the home device name, placeholders of translated errors such as "… is not a tado X home") keep the title. Home Assistant's own lines about the entry log it regardless.
- Any other string from the API is described as `<str len=N>` by `describe_value()`; `shape()` keeps a constant only under the known enum keys in `ENUM_KEYS`.
- A client error that replaces a third-party exception is raised `from None`: `TadoXConnectionError.cause` keeps only the class name of the original error, so a traceback in Home Assistant's log carries no URL, serial or body.
- Writes are logged by `describe_command()` from an allow-list of fields; meter readings and tariffs say `(value hidden)`.
- Diagnostics add the telemetry (`exchanges`, `errors`, `requests_by_endpoint_24h`, `errors_by_kind_24h`, `last_error`) and pass everything through `async_redact_data()`, which also removes room and home names, and through the serial masking. The entry's `title` is replaced by the home label.

`tests/test_logging.py` runs the sign-in, setup, polls, writes, rotation, a 401 cycle, a rejected refresh, 4xx and 5xx bodies laced with token-like strings, an e-mail, an address and a home id, and diagnostics, and fails if any of it reaches the log. Its entries are titled with tado's name of the home, as the config flow titles them, and the tests fail if that name reaches the integration's log lines or diagnostics. It also checks the INFO budget, setup retries during a quota hold, and that each warning is logged once.

## Tests

- `tests/tado_mock.py` provides `TadoCloudMock`, a stateful fake of the tado hosts on top of `aioclient_mock`. It rotates tokens (reusing a rotated refresh token gives `invalid_grant`), emits `ratelimit` headers from a counter, injects failures and holds responses for concurrency tests.
- No integration code is patched. Time moves with `freezer`; the device flow gets an injected sleep.
- Coverage gates: 95 % line and branch overall; 100 % line for `client/auth.py`, `client/ratelimit.py`, `client/http.py`, `budget.py`, `termination.py`, `migration.py` and `config_flow.py`; 100 % line and branch for `client/telemetry.py`.
- `tests/test_translations.py` checks that `en.json` equals `strings.json`, that German is complete, that every key used in code exists, and that placeholders match.

## Decisions worth knowing

| Decision | Reason |
|---|---|
| Three coordinators, one quota gate | endpoints change at very different rates; one gate stops a 429 from turning into three |
| Budget instead of an interval option | the quota is a per-day resource; HA discourages interval options |
| Entry 1.2 with flat token keys | 1.8.11 can still load it, so rollback needs no new sign-in |
| Stale data only during a quota hold | with `t=` a hold has a known end, so data is kept until then; without it, room temperatures older than 6 h are misleading |
| Devices via the Bridge X | it is the routing parent, as in the core tado integration |
| Presets `home`/`away`/`auto` kept | existing automations use them |
| `off` stays manual by default | an "off" that reverts on its own surprises people |
| No learned reset time | sources disagree on when tado resets; only `t=` is trusted |
| HTTP as the only test seam | parsing, tokens, headers and errors run in every test |
| Telemetry in memory only | it explains the recent past; persisting it would put request history on disk for no gain |
| INFO only for changes of state and writes | users can leave INFO on; a steady home logs nothing |
| Endpoint templates instead of URLs | ids stay out of logs, and the counters stay low-cardinality |

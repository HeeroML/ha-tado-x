# Changelog

All notable changes of the HeeroML fork of [exabird/ha-tado-x](https://github.com/exabird/ha-tado-x). Issue and pull request numbers refer to the upstream repository. Versions up to 1.8.11 are upstream releases; see the upstream [releases](https://github.com/exabird/ha-tado-x/releases) for them.

## 1.9.0

A rewrite of v1.8.11. The domain stays `tado_x`, every entity unique id of 1.8.11 is kept, and an existing installation upgrades in place without a new sign-in. Home Assistant 2026.10.0 or newer is required.

### Breaking changes

Check automations, scripts and dashboards for these. After the upgrade, a dismissible repair notice ("tado X 1.9 changed entities and options") lists them.

1. **ENUM states are lowercase**, as Home Assistant requires: `api_status` (`ok`, `throttled`, `conserving`, `rate_limited`), `presence_state` (`home`, `away`), `presence_mode` (`auto`, `manual`) and `weather_state` (for example `cloudy_partly`).
2. **Phone trackers:** the state `away` is now `not_home`, and the literal states `not_tracking` and `unknown` are now `unknown`. Trackers report the zone chosen in the new option "Zone of this home" (default `zone.home`). Their attributes are reduced to `geofencing_enabled` and `location_stale`. A new at-home binary sensor per phone gives the same answer without zones.
3. **Climate:**
   - the `schedule` preset is removed. It could never be shown as active; use HVAC mode `auto` to resume the schedule;
   - the attributes `next_schedule_change`, `next_schedule_temperature`, `heating_power`, `manual_control_active`, `manual_control_remaining_minutes` and `open_window_detected` are removed. Sensors replace them; only `manual_control_type` remains;
   - a temperature set from Home Assistant no longer reverts after a hard-coded 30 minutes. It follows the room's setting in the tado app, or the new option "Manual changes end" (#94).
4. **Devices:**
   - device names are translated into your language unless you renamed them, for example "Radiator valve Living room" instead of the hard-coded French "Vanne - Living room";
   - valves and sensors are connected via the Bridge X instead of the room.
5. **Options:** the update interval and the Auto-Assist checkbox are replaced by a daily call budget and quota detection from tado's headers. The migration derives the budget from your old interval. New entries have phone tracking off by default.
6. **Sensor classes:**
   - the temperature offset sensor uses the device class `temperature_delta`;
   - `heating_time_today` uses the state class `total_increasing`, so the recorder may report a state class change once.
7. **Minimum Home Assistant version** in HACS is 2026.10.0.
8. **Rollback** to 1.8.11 stays possible without a new sign-in; your old 1.8 settings come back (see the README).

### Fixed issues

| Issue | Problem | Fix |
|---|---|---|
| #36 | refresh token races lead to `invalid_grant` | one refresh at a time per entry, the 401 retry path included; waiting requests reuse the new token |
| #40 | "Timezone of home not found" after picking a V3+ home | the config flow offers only `LINE_X` homes and names the skipped ones; an existing non-X entry gets a repair issue and a clear setup error |
| #47 | the API reset time is a hard-coded guess | the reset time comes from the `t=` value of tado's `ratelimit` header and is `unknown` otherwise |
| #51 | the offset service fails silently | errors are raised to the caller; a device that cannot take an offset is rejected; a per-device offset number is added; the new value shows without an extra read |
| #57 | no refresh service | not added: `homeassistant.update_entity` refreshes a tado X entity |
| #58 | no serial numbers | every physical device has its serial number |
| #59 | no next schedule change | sensors for next schedule change, next schedule temperature, manual control end and next time block |
| #60, #77 | no schedule access | `get_schedule` and `set_schedule` actions |
| #62 | entities unavailable after a restart while the quota is used up | raw payloads and the quota state are cached; a restart or reload during a quota hold creates every entity from the cache and sends no requests; last values are kept until the reset when tado sent its time, also across a restart or reload, otherwise with limits |
| #64 | `set_climate_timer` rejects area, device and label targets | it is an entity action now; any target works, and the 1.8 data form still validates |
| #69 | no holiday mode | holiday mode actions and an optional binary sensor (experimental) |
| #72, #85 | a depleted battery raises an error | `depleted` is a valid state; `battery_low` is on for low and depleted; unknown values become `unknown` with one warning |
| #73 | config flow error 500 on Python 3.14 | every error path aborts with a reason; the flow uses HA's shared session, imports on Python 3.14 without deprecation warnings, and is tested through the HTTP view. The original 500 was not reproduced |
| #75, #79 | the token is lost after a restart or a reload | a rotated refresh token is written to the config entry before it is used, and Home Assistant flushes it to disk about 1 s later; unloading and stopping Home Assistant wait for a running refresh; the startup refresh is gone, so a fresh token costs no request |
| #76 | reauthentication fails with `missing_client_id` | the sign-in link always carries `client_id` and `user_code` |
| #78, #82, #83 | deprecation warnings | `via_device_id`, `in_zones`, `runtime_data`, `OptionsFlowWithReload`, `AddConfigEntryEntitiesCallback`; a test fails on any deprecation warning from the integration |
| #80 | a network error during a refresh forces a reauthentication | network errors, timeouts, 429, 5xx and any other answer of the login server that is no OAuth rejection (such as a proxy error page) are transient, retried with backoff (5 s to 5 min), and never start a reauthentication |
| #92 | actions reach the wrong home with two homes | actions are registered once and resolve the target home per call; home actions take a `config_entry_id` |
| #94 | manual changes revert after 30 minutes | see breaking change 3 |
| #56 | clearing the maximum flow temperature | not addressed; #52 claimed it, but its diff does not contain it |
| #89 | `shortSerialNo` error | belongs to the core tado integration; nothing to change here |

Other fixes from the review of 1.8.11:

- an optional endpoint's 429 or authentication error is no longer swallowed;
- polling stops during a quota hold instead of producing a 429 per interval;
- a 204 response after a token refresh is no longer an error;
- the air comfort humidity level is no longer mixed up with the home's freshness;
- the phone tracker no longer ignores a stale location;
- the device-code sign-in no longer blocks a request for 120 s and honours tado's polling interval and `slow_down`;
- "today" for heating time, day reports and meter readings uses the home's time zone, not the host's.

### Ported pull requests

The behaviour of these pull requests is ported and tested; the code is rewritten for the new structure. Thank you to every author.

| PR | Author | Taken | Left out, and why |
|---|---|---|---|
| #96 | [cmatte](https://github.com/cmatte) | the HA deprecation fixes: `via_device_id`, `in_zones`, HA's SSL context through the shared session, no user code in the log, minimum HA version | the lookup-or-omit helper for `via_device_id`: devices are registered centrally before the platforms load, so the parent always exists. The tracker no longer assumes HA's home zone is the tado home: the zone is an option, and an at-home sensor is added |
| #93 | [bananaseason](https://github.com/bananaseason) | transient refresh failures (network, timeout, 429, 5xx) are not authentication failures; setup retries instead of failing | a dedicated transient error type replaces the generic API error |
| #90 | [ilyesAj](https://github.com/ilyesAj) | `get_schedule`, `set_schedule`, `get_day_report` with the summary, `get_holiday_mode`, `set_holiday_mode`, next-schedule sensors | the refresh-schedule button and the `schedule` climate attribute (stale by design, heavy for the recorder); JSON text input for schedules (now structured and validated before any request); aggregate day types (tado X takes single weekdays); a holiday PATCH after a failed read |
| #88 | [rleatherb](https://github.com/rleatherb), including the closed #54 by [xiic](https://github.com/xiic) | termination from the room's tado app setting; per-room select and timer number that write that setting; `termination` in `set_climate_timer` with 5–30 °C; multi-home routing; serial numbers | the restored timer value: the server value is the only source, so after a restart the number shows what tado has. Renamed keys `manual_termination` and `manual_termination_timer` with lowercase states; `config_entry_id` instead of a free-text `home_id` |
| #87 | [Somberland](https://github.com/Somberland) | the `depleted` battery state | extended: `battery_low` is also on for depleted, and unknown values no longer raise |
| #86 | [Somberland](https://github.com/Somberland) | the presence select's name in every language | wording aligned with the existing translations |
| #84 | [Somberland](https://github.com/Somberland) | single-flight token refresh without locking every request | extended: the 401 retry path uses the same lock |
| #81 | [agu2347](https://github.com/agu2347) | the six test cases, rewritten for pytest | the code: #93 covers it and more |
| #66 | [donalfenwick](https://github.com/donalfenwick) | the device control mode sensor | uppercase state keys (hassfest rejects them); the entity is now an ENUM sensor and is also created when the field appears later |
| #52 | [LeroyK](https://github.com/LeroyK) | the offset write without an extra read and with errors raised; a snapshot cache, used only as a fallback during a quota hold; the reset time from the `ratelimit` header instead of a configured time; serial numbers; the next time block sensor | the lock around every request (it serializes all calls), the background refresh task, the "sleep and retry with the same token" heuristic, the `refresh_data` service, and always preferring the cache at startup |
| #45 | [rofra](https://github.com/rofra) | the French translation, mapped to the new keys | strings for new features are still missing and fall back to English |

Not ported:

| PR | Author | Reason |
|---|---|---|
| #53 | [donalfenwick](https://github.com/donalfenwick) | domestic hot water control. The fork's maintainer has no hot water control to test it on. The endpoints are documented in `docs/API.md` |
| #74 | [lchovanec](https://github.com/lchovanec) | domestic hot water target temperature; same reason |

### Added

- A daily API call budget with a planner (rooms every 45 s for a 6-room home at the default 3,000 calls a day), throttling against the account's remaining calls, a quota reserve and a conserving mode. See "API rate limits & smart polling" in the README.
- Quota handling: a local gate that stops requests until tado's reset, persisted across restarts; a 15 to 120 minute backoff when a 429 has no reset time; commands keep priority over polling.
- Sensors: API calls by this integration (24 h), data updated, next schedule change, next schedule temperature, manual control end, next time block (disabled), boost (disabled), control mode, holiday mode (option).
- Per-room manual control duration select and timer number; per-device temperature offset number; identify button (disabled, experimental).
- Options: daily call budget, quota reserve, holiday mode, manual changes end, timer, turning a room off ends the same way, zone of this home.
- Reconfigure flow to switch the tado account of a home; reauthentication checks that the same account is used.
- Repair issues: quota used up, quota low, small quota tier, not a tado X home, home set up twice, breaking changes in 1.9.
- Entry and device diagnostics, with tokens, account data and coordinates redacted and serial numbers shortened.
- Translated exceptions, entity names, states, device names, services and issues. The German translation is complete; French and Italian are carried over and partial.
- `scripts/live_probe.py`, a read-only probe of the live API that writes a pseudonymized report.
- A test suite over HTTP against a simulated tado cloud, with coverage gates; CI with ruff, mypy (strict), pytest, hassfest and HACS validation; a release workflow that checks the tag against the manifest.

### Changed

- The tado client is a separate typed package (`custom_components/tado_x/client`) that does not import Home Assistant.
- Three coordinators (rooms, home, devices) instead of one; each endpoint has its own period.
- Commands update the UI at once; one debounced verification read follows room commands.
- Rooms, devices and phones that appear later get entities without a reload; devices missing from a live read are removed.
- Config entry version 1.2. The tokens keep their keys, `token_expiry` is a UTC timestamp, and the 1.8 settings stay in place for a rollback.
- Brand images moved to `custom_components/tado_x/brand`.
- Devices no longer suggest an area. Home Assistant 2026.10 puts the area name in front of new entity ids, so a suggested area named like the room produced ids such as `climate.wohnzimmer_wohnzimmer`.
- The hard-coded `aiohttp` requirement is removed; Home Assistant's own session is used.

### Removed

- The `schedule` climate preset and the climate attributes listed under breaking change 3.
- The update interval and Auto-Assist options.
- The hard-coded French device names ("Vanne", "Capteur Temp", "Récepteur", "Thermostat").
- The guess that puts a Wireless Receiver X without a room into the room with the most devices; it stays unassigned.

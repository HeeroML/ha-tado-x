# tado X for Home Assistant (HeeroML fork)

[![HACS custom repository](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub release](https://img.shields.io/github/v/release/HeeroML/ha-tado-x)](https://github.com/HeeroML/ha-tado-x/releases)

A Home Assistant custom integration for **tado X** homes: Bridge X, Radiator Valve X, Temperature Sensor X, Wireless Receiver X and Smart Thermostat X. It uses the tado cloud API.

This repository is a fork of [exabird/ha-tado-x](https://github.com/exabird/ha-tado-x). Version 1.9.0 rewrites upstream v1.8.11. It keeps the domain `tado_x` and every existing entity unique id, so it upgrades an existing installation in place. The [changelog](CHANGELOG.md) lists every change and credits the authors of the upstream pull requests it ports.

Homes of the previous tado generation (V3+ and older) are not supported here. Use the [core tado integration](https://www.home-assistant.io/integrations/tado/) for them. Both integrations can run side by side on the same tado account (see [Coexistence](#coexistence-with-the-core-tado-integration)).

> **Status.** 1.9.0 is tested against a simulated tado cloud over HTTP, not yet against a live home. [Known limitations and risks](#known-limitations-and-risks) lists what still needs a live check. Features that rest on a single source are marked experimental.

## Contents

- [Differences from upstream v1.8.11](#differences-from-upstream-v1811)
- [Requirements](#requirements)
- [Installation](#installation)
- [Coexistence with the core tado integration](#coexistence-with-the-core-tado-integration)
- [Setup](#setup)
- [Upgrading from exabird v1.8.x](#upgrading-from-exabird-v18x)
- [Options](#options)
- [API rate limits & smart polling](#api-rate-limits--smart-polling)
- [Entities](#entities)
- [Supported devices](#supported-devices)
- [Actions](#actions)
- [Automation examples](#automation-examples)
- [How data is updated](#how-data-is-updated)
- [Live API probe](#live-api-probe)
- [Diagnostics and privacy](#diagnostics-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Known limitations and risks](#known-limitations-and-risks)
- [Rolling back to v1.8.11](#rolling-back-to-v1811)
- [Removal](#removal)
- [Credits and license](#credits-and-license)

## Differences from upstream v1.8.11

Fixed upstream issues:

- **Token handling** (#36, #75, #79, #80): one refresh at a time per home, including after a 401. A rotated refresh token is saved before it is used. A network error, timeout, 429 or 5xx during a refresh is retried with backoff and never starts a reauthentication. Only answers such as `invalid_grant` do.
- **Sign-in link** (#76): the device-code link always carries `client_id`, so tado no longer answers `missing_client_id`.
- **Config flow error 500 on Python 3.14** (#73): every error path aborts cleanly, HA's shared HTTP session is used, and the flow is also tested through HA's HTTP view. The original 500 was never reproduced, so please report a traceback if you still see one.
- **"Timezone of home not found"** (#40): only tado X (`LINE_X`) homes are offered. Older homes are listed as skipped, and an existing entry for a non-X home gets a repair issue.
- **Manual changes revert after 30 minutes** (#94): a temperature set in Home Assistant now ends the way the room is set up in the tado app: until you end it, at the next time block, or after the app's timer. An option overrides this.
- **Services reach the wrong home** (#92) and **`set_climate_timer` rejects targets** (#64): actions are registered once and resolve their target on each call. Room actions accept entity, device, area, floor and label targets.
- **Offset service fails silently** (#51): errors reach the caller. Each device also has an offset number entity.
- **Entities unavailable after a restart while the quota is used up** (#62): the last data is cached, the quota state is persisted, and entities are created from the cache. See [Quota hold](#quota-hold).
- **A depleted battery breaks the battery sensor** (#85, #72): `depleted` is a state, `battery_low` is on for low and depleted, and unknown values no longer raise.
- **Invented reset time** (#47): the quota reset time comes only from tado's `ratelimit` header. Without it the time is `unknown`.
- **Serial numbers** (#58) on every physical device.
- **Next schedule change** (#59), **schedules** (#60, #77) and **holiday mode** (#69) are available as entities and actions.
- **Home Assistant deprecations** (#78, #82, #83): `via_device_id`, `in_zones` for phone trackers, `runtime_data`, `OptionsFlowWithReload`.

Ported pull requests. The behaviour is ported and tested again; the code is rewritten:

| PR | Author | What 1.9.0 takes |
|---|---|---|
| #96 | [cmatte](https://github.com/cmatte) | HA deprecation fixes; devices are registered in one place instead of looked up |
| #93 | [bananaseason](https://github.com/bananaseason) | transient refresh failures are not authentication failures |
| #90 | [ilyesAj](https://github.com/ilyesAj) | schedule, day report and holiday mode actions, next-schedule sensors, day-report summary |
| #88 | [rleatherb](https://github.com/rleatherb), including #54 by [xiic](https://github.com/xiic) | per-room termination from the tado app, termination select and timer number, `termination` in `set_climate_timer`, multi-home routing, serial numbers |
| #87 | [Somberland](https://github.com/Somberland) | `depleted` battery state |
| #86 | [Somberland](https://github.com/Somberland) | translated name for the presence select |
| #84 | [Somberland](https://github.com/Somberland) | serialized token refresh |
| #81 | [agu2347](https://github.com/agu2347) | its test cases; the code is superseded by #93 |
| #66 | [donalfenwick](https://github.com/donalfenwick) | device control mode sensor |
| #52 | [LeroyK](https://github.com/LeroyK) | offset without an extra read, cache for #62, reset time from headers, serial numbers, next time block |
| #45 | [rofra](https://github.com/rofra) | French translation, re-keyed |

Not ported: #53 and #74 (domestic hot water, by [donalfenwick](https://github.com/donalfenwick) and [lchovanec](https://github.com/lchovanec)). This fork's maintainer has no hot water control to test them on. The endpoints are documented in [docs/API.md](docs/API.md).

Other changes:

- Polling follows a daily call budget instead of a fixed interval.
- Three coordinators poll each endpoint at its own pace.
- The tado client is a separate package with no Home Assistant imports.
- The German translation is complete.
- A 6-room home gets 216 entities, 51 of them disabled by default.

The [breaking changes](CHANGELOG.md#breaking-changes) are listed in the changelog.

## Requirements

- Home Assistant **2026.10.0** or newer.
- A tado account with a tado X home: a Bridge X and at least one tado X device.
- An Auto-Assist (or AI Assist) subscription is strongly recommended. Without one, tado allows 100 API calls a day per account, so rooms update only about every 45 minutes.

## Installation

### HACS (custom repository)

1. Open HACS in Home Assistant.
2. Open the menu (⋮, top right) and choose **Custom repositories**.
3. Enter `https://github.com/HeeroML/ha-tado-x`, choose the type **Integration** and select **Add**.
4. Search for **Tado X** in HACS and select **Download**.
5. Restart Home Assistant.

If you installed the upstream `exabird/ha-tado-x` through HACS, remove it in HACS first (open it, ⋮ → **Remove**). That deletes only its files; your config entry and its entities stay. Both repositories install to `custom_components/tado_x`, so only one can be installed at a time. Do not delete the integration entry in Home Assistant.

### Manual

Copy `custom_components/tado_x` from a [release](https://github.com/HeeroML/ha-tado-x/releases) (the `tado_x.zip` asset) to `config/custom_components/tado_x` and restart Home Assistant.

## Coexistence with the core tado integration

A tado account can hold a V3+ home and a tado X home at the same time. They need two integrations:

| Home | Integration |
|---|---|
| V3+ and older (`PRE_LINE_X`) | core `tado`, built into Home Assistant |
| tado X (`LINE_X`) | this one, `tado_x` |

Inside Home Assistant the two share nothing: they have different domains and config entries, and each signs in with its own device code, so their refresh tokens do not interfere. This integration never offers a V3+ home and never touches the core integration's entries.

At tado they do share one thing: **the daily API quota of the account**. The quota (20,000 calls a day with Auto-Assist) covers the core integration, this integration and possibly the tado apps. This integration therefore plans its polling against a budget (3,000 calls a day by default) and keeps a reserve for the rest of the account. See [API rate limits & smart polling](#api-rate-limits--smart-polling).

## Setup

1. Go to **Settings → Devices & services → Add integration → Tado X**.
2. The dialog shows a link and a code. Open the link, sign in to tado and confirm. The dialog continues on its own, and nothing blocks while you sign in. The code expires after a few minutes; the dialog then offers a new one.
3. The integration reads your account and each of its homes once: one call for the account plus one per home. It offers only tado X homes:
   - with one tado X home, the entry is created directly;
   - with several, you pick one; add the integration again for the next one;
   - with none, the dialog names the skipped homes and points to the core integration.

Each config entry controls one home. Its tokens, quota state, polling and cache are separate from any other entry.

Later changes:

- **Reauthenticate:** when tado rejects the stored sign-in, Home Assistant asks you to sign in again. Use the same tado account.
- **Reconfigure:** on the integration page, **Reconfigure** switches the tado account used for this home, for example to a separate account just for Home Assistant. Entities and history stay.
- **Options:** see [Options](#options).

## Upgrading from exabird v1.8.x

Install this repository in place of the upstream one (see [Installation](#installation)) and restart. Nothing else is needed:

- The config entry is migrated from version 1.1 to 1.2. Your tokens are kept, so you do not sign in again.
- Your update interval becomes a daily call budget that keeps about the same room interval. Your feature toggles (weather, air comfort, running times, phones) are carried over.
- Every existing entity keeps its unique id and history. New entities are added, some of them disabled by default.
- A repair notice, "tado X 1.9 changed entities and options", summarizes the breaking changes. You can ignore it once you have checked your automations.

The old settings stay in the config entry, so you can [roll back](#rolling-back-to-v1811).

## Options

Go to **Settings → Devices & services → Tado X → Configure**. The top of the form shows the result of the current settings: the room update interval, the planned calls per day, the account's limit and how many calls are left today.

| Option | Default | Range | Meaning |
|---|---|---|---|
| **Daily call budget** | 3,000 | 100–20,000 | API calls a day this integration plans for polling. At most 80 % of the account limit is used. A smaller budget means slower room updates. |
| **Quota reserve** (collapsed) | 1,000 | 0–10,000 | Calls left for the rest of the account. When the account's remaining calls fall to this level, the integration switches to [conserving mode](#throttling-and-conserving-mode). |
| Weather | on | | outdoor temperature, solar intensity and weather; about 96 calls a day |
| Air comfort | on | | humidity level and comfort level per room; about 96 a day |
| Heating time today | on | | heating time per room; about 24 a day |
| Phones | off for new entries | | a tracker and an at-home sensor per phone with geofencing; about 288 a day |
| Holiday mode | off | | a holiday mode sensor, experimental; about 96 a day |
| **Manual changes end** | following the tado app | following the tado app, until ended, next time block, timer | When a temperature set from Home Assistant returns to the schedule. Following the tado app uses each room's setting in the tado app. |
| Timer | 60 min | 5–1,440 | Used when the option above is the timer. |
| Turning a room off ends the same way | off | | Off: a room turned off from Home Assistant stays off until you change it. On: it returns to its schedule like other manual changes. |
| Zone of this home | `zone.home` | any zone | The zone a phone tracker reports while the phone is at this home. Choose your own zone if Home Assistant's home zone is another place. |

Saving the options reloads the entry. Token updates never do.

There is no update interval option, because the budget decides the interval. Home Assistant's [polling rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/appropriate-polling/) discourages interval options. To refresh now, call `homeassistant.update_entity` on any tado X entity.

## API rate limits & smart polling

tado limits each **account** to 100 API calls a day, or 20,000 with Auto-Assist ([tado help article](https://help.tado.com/en/articles/12165739)). Every response carries a header like `ratelimit: "perday";r=17833` with the remaining calls. Sometimes it also has `t=`, the seconds until the quota refills. The integration reads these headers and never invents a reset time.

### The plan

Each endpoint has its own period. Rooms (temperatures, targets, heating power, windows) get whatever the budget leaves after the slower endpoints:

1. cap = the budget, but at most 80 % of the account limit;
2. 90 % of the cap is for polling, and 10 % stays free for your commands;
3. the slower endpoints are planned at their base periods, stretched if they would use more than half;
4. rooms get the rest, between 30 s and 1 h, rounded to 5 s.

Example: a home with **6 rooms, a Radiator Valve X and a Temperature Sensor X in each, and a Bridge X**, with the default options (budget 3,000; weather, air comfort and heating time on; phones and holiday mode off; no OpenTherm boiler control):

| Endpoint | Period | Calls a day |
|---|---|---|
| rooms | **45 s** | 1,920 |
| home state (presence) | 5 min | 288 |
| weather | 15 min | 96 |
| air comfort | 15 min | 96 |
| rooms and devices (batteries, firmware, offsets) | 15 min | 96 |
| heating time today | 1 h | 24 |
| home info | 1 day | 1 |
| flow temperature | not polled, because the home reports no support | 0 |
| **Planned polling** | | **2,521** |
| commands (10 % reserve; each command costs one write, and a burst adds one shared verification read) | | ≤ 300 |
| token refresh at login.tado.com (unknown whether it counts) | | ≤ 144 |
| **Worst case** | | **≤ 2,965**, about 15 % of 20,000 |

With Auto-Assist, upstream v1.8.11 planned about 23,000 calls a day for the same home, more than the whole account quota.

Other settings for the same home:

| Setting | Rooms period | Planned calls a day |
|---|---|---|
| defaults plus phones and holiday mode | 55 s | 2,556 |
| budget 6,000 | 30 s (the fastest) | 3,481 |
| budget 2,000 | 75 s | 1,753 |
| free tier (limit 100) | about 44 min | 72 |

The number of rooms does not change the call count, because tado returns all rooms in one call.

### Throttling and conserving mode

The plan assumes a whole day is ahead. The integration also compares what it still needs with what the account has left, minus the reserve:

- If the account has enough left, the plan runs as it is (`api_status` = `ok`).
- If not, all periods are stretched by the same factor, up to 30 times (`throttled`). Example at 18:00 with 1,800 calls left and a reserve of 1,000: if tado reported the reset 6 h away, nothing changes. If tado sent no reset time, the integration assumes 24 h, and rooms slow down to about 145 s.
- When the remaining calls drop to the reserve, rooms poll hourly, optional data pauses, and commands still go through (`conserving`). After 10 minutes in this mode a repair issue appears.
- At 20 remaining calls or fewer, polling stops and only commands are sent.

If more than one tado X entry uses the same tado account, the remaining calls are split between them.

### Quota hold

When tado answers 429, or reports 0 remaining calls with a reset time, the integration sends no requests until that time plus 30 s. During the hold it:

- keeps showing the last known values, for at most 6 h for rooms, 12 h for home data and 48 h for devices, after which the entities become unavailable;
- sets `api_status` to `rate_limited` and the reset sensor to the reset time;
- raises the repair issue "tado API quota used up";
- fails commands with a message that names the reset time.

The hold and the last data are saved. A restart during a hold therefore sends no requests and still creates every entity from the cache. Polling resumes on its own after the reset. Network, server and parse errors are not holds: entities go unavailable as usual.

### API sensors

All are on the home device, in the diagnostic category:

| Sensor | Meaning |
|---|---|
| API calls used (account) | limit minus remaining: everything the account used today, including the core integration and the apps |
| API calls by this integration (24 h) | this entry's own calls over the last 24 hours |
| API quota remaining | `r=` from the latest response |
| API quota limit (disabled) | `q=`: 100 or 20,000 |
| API quota used (disabled) | used calls as a percentage of the limit |
| API quota reset | from `t=`, or the end of a hold; `unknown` otherwise |
| API status | `ok`, `throttled`, `conserving` or `rate_limited` |
| Data updated | the last time room data came live from tado |

## Entities

For the 6-room home above, a new installation creates 216 entities, 165 of them enabled. Phones add two each when the option is on. Upgraded installations keep the enabled state of their existing entities.

**Home device**

| Entity | Notes |
|---|---|
| Presence mode select | `home`, `away`, `auto` (geofencing, needs Auto-Assist) |
| Presence sensor, presence mode sensor (disabled) | |
| Outdoor temperature, solar intensity, weather | option Weather |
| Buttons: boost all rooms, turn off all rooms, resume all schedules | tado quick actions |
| Maximum flow temperature number, flow temperature auto-adaptation switch | only if the home has OpenTherm boiler control |
| Holiday mode binary sensor | option Holiday mode |
| API sensors | see [API sensors](#api-sensors) |

**Room device** (one per room)

| Entity | Notes |
|---|---|
| Climate | target 5–30 °C in steps of 0.5; HVAC modes heat, off and auto (schedule); presets `home`, `away`, `auto`, which change presence for the whole home |
| Temperature, humidity, heating power | |
| Heating time today | option Heating time today |
| Humidity level, comfort level | option Air comfort |
| Next schedule change, next schedule temperature, manual control end, next time block (disabled) | |
| Window open, heating, manual control, boost (disabled) | binary sensors |
| Open window mode switch | turns tado's open window mode on or off; turning it on works only after tado has detected an open window |
| Manual control duration select | writes the room's setting in the tado app: until ended, until the next time block, or timer |
| Manual control timer number | minutes; available only while the select is set to timer |

**Physical devices** (valves, sensors, receivers, thermostats and the Bridge X)

| Entity | Created when |
|---|---|
| Connectivity | always |
| Battery (`normal`, `low`, `depleted`), battery low | the device reports a battery |
| Temperature offset number (−9.9 to 9.9 °C) | the device reports an offset |
| Temperature offset sensor, device temperature (both disabled) | the device reports them |
| Child lock switch | the device reports a child lock (Radiator Valve X) |
| Control mode | the device reports one |
| Identify button (disabled, experimental) | valves, sensors, receivers and thermostats |

**Phones** (option Phones): a device tracker per phone. It reports the zone from the options while tado says the phone is at home, `not_home` while it is away, and `unknown` when geofencing is off or the location is stale. An at-home binary sensor per phone gives the same answer without zones.

Valves and sensors are connected via the Bridge X, and rooms belong to the home. A new room device suggests an area with its name to Home Assistant.

## Supported devices

| Code | Device | Notes |
|---|---|---|
| IB02 | Bridge X | connectivity, firmware |
| VA04 | Radiator Valve X | battery, offset, child lock, measured temperature |
| SU04 | Temperature Sensor X | battery, offset, measured temperature |
| TR04 | Wireless Receiver X | entities depend on what tado reports; not tested on hardware by this fork |
| RU04 | Smart Thermostat X | entities depend on what tado reports; not tested on hardware by this fork |

Unknown device types are registered as generic devices with a connectivity sensor.

Not supported: domestic hot water control, local control over Matter or Thread, and homes of the previous tado generation.

## Actions

All actions are registered once and work with several tado X homes:

- Room actions take a normal target: entities, devices, areas, floors or labels.
- Home actions take a `config_entry_id`. You can leave it out when only one tado X home is set up.
- Invalid input raises a validation error, and an API error raises an error with a translated message. Nothing fails silently.

There is no `resume_schedule` or refresh action. Use `climate.set_hvac_mode` with `auto` for rooms, the "Resume all schedules" button for the whole home, and `homeassistant.update_entity` to refresh.

### `tado_x.set_climate_timer`

Sets a temperature and says when the schedule takes over again.

```yaml
# 21.5 °C for 90 minutes in every tado X room of an area
action: tado_x.set_climate_timer
target:
  area_id: living_room
data:
  temperature: 21.5
  duration: 90
```

```yaml
# 19 °C until the next time block of the schedule
action: tado_x.set_climate_timer
target:
  entity_id: climate.bedroom
data:
  temperature: 19
  termination: next_time_block
```

`termination` is `manual` (until ended), `next_time_block` or `timer`:

- `duration` without `termination` means a timer;
- with neither, the option "Manual changes end" applies;
- `timer` without `duration`, or `duration` with another termination, is rejected.

The v1.8 form with `entity_id` under `data` still works.

### `tado_x.get_schedule` and `tado_x.set_schedule`

```yaml
action: tado_x.get_schedule
target:
  entity_id: climate.bedroom
response_variable: schedule
```

The response has one item per entity: `{"days": {"monday": [{"start": "06:30", "end": "22:00", "power": "on", "temperature": 21.0}, ...], ...}}`.

```yaml
# Weekdays: 17 °C at night, 21 °C during the day, off late in the evening
action: tado_x.set_schedule
target:
  entity_id: climate.bedroom
data:
  days: [monday, tuesday, wednesday, thursday, friday]
  blocks:
    - {start: "00:00", end: "06:30", temperature: 17}
    - {start: "06:30", end: "22:00", temperature: 21}
    - {start: "22:00", end: "24:00", power: "off"}
```

Rules for the blocks:

- they must cover 00:00 to 24:00 without gaps;
- `power` defaults to `on`;
- heating blocks need a temperature from 5 to 30 °C;
- `off` blocks take no temperature.

The whole input is checked before anything is sent. tado takes one weekday per request, so the example sends 5 writes per room. If a write fails partway, the error names the days already written.

### `tado_x.get_day_report`

```yaml
action: tado_x.get_day_report
target:
  entity_id: climate.bedroom
data:
  date: "2026-10-29"
response_variable: report
```

Returns a `summary` (average, minimum and maximum temperature, average humidity in %, heating hours) and the raw `report`. The date defaults to today in the home's time zone. tado serves past days only with Auto-Assist. Each room costs one call; reports are never polled.

### `tado_x.set_temperature_offset`

```yaml
action: tado_x.set_temperature_offset
data:
  device_id: 0123456789abcdef0123456789abcdef  # a Radiator Valve X, Temperature Sensor X or Smart Thermostat X
  offset: -1.2
```

Any other device (room, home, bridge or phone), and any device without an offset, is rejected. The offset number entity on each device does the same. The new value shows at once, without an extra read.

### `tado_x.add_meter_reading` and `tado_x.set_eiq_tariff` (Energy IQ)

```yaml
action: tado_x.add_meter_reading
data:
  reading: 12345
  date: "2026-10-30"  # optional; defaults to today in the home's time zone
```

```yaml
# 0.29 per kWh from 1 October; tado stores 29 (cents, rounded half up)
action: tado_x.set_eiq_tariff
data:
  tariff: 0.29
  unit: kwh  # or m3
  start_date: "2026-10-01"
```

### `tado_x.get_holiday_mode` and `tado_x.set_holiday_mode` (experimental)

```yaml
action: tado_x.set_holiday_mode
data:
  enabled: true
  start: "2026-12-22 08:00:00"
  end: "2027-01-02 18:00:00"
```

The set action reads the current holiday settings, changes only `enabled`, the start and the end, and writes everything else back unchanged. If the read fails, nothing is written. `get_holiday_mode` returns `enabled`, `active`, `start` and `end`. The endpoint shape comes from one reverse-engineered source, and the feature needs Auto-Assist.

## Automation examples

Heat the bathroom for 30 minutes when someone presses a button, then return to the schedule:

```yaml
automation:
  - alias: Bathroom boost
    triggers:
      - trigger: state
        entity_id: event.bathroom_button
    actions:
      - action: tado_x.set_climate_timer
        target:
          area_id: bathroom
        data:
          temperature: 23
          duration: 30
```

Warn when a valve battery runs low or is empty:

```yaml
automation:
  - alias: tado X battery warning
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.living_room_radiator_valve_battery_low
        to: "on"
    actions:
      - action: notify.notify
        data:
          message: "{{ trigger.to_state.name }}: replace the batteries"
```

Set the whole home to away when the last person leaves (presence is home-wide in tado):

```yaml
automation:
  - alias: tado away
    triggers:
      - trigger: state
        entity_id: zone.home
        to: "0"
    actions:
      - action: select.select_option
        target:
          entity_id: select.my_home_presence_mode
        data:
          option: away
```

The entity ids are examples; look up yours under **Settings → Entities**.

## How data is updated

- Three coordinators poll tado. The rooms coordinator is the fast one. The home coordinator reads presence, weather, air comfort and phones. The devices coordinator reads rooms and devices, home info, heating time, flow temperature and holiday mode.
- Each endpoint has its own period from [the plan](#the-plan). Unchanged data writes no new state.
- Commands are sent at once and have priority over polling. The UI shows the new value right away.
- After room commands, one verification read follows 5 s later, however many commands you sent in that time. If tado still reports the old value for a moment, the new value is kept for up to 60 s.
- Rooms, devices and phones that appear later get their entities on the next poll.
- Devices missing from a live read are removed from Home Assistant. A read from the cache never removes anything.

## Live API probe

`scripts/live_probe.py` checks facts about your account that could not be verified without a live home, for example:

- which hosts count against the quota;
- whether tado sends a reset time while calls remain;
- what the room termination setting looks like;
- which fields your devices report.

The result helps settle the open points under [Known limitations and risks](#known-limitations-and-risks). The probe needs only Python 3.11 or newer with its standard library, not Home Assistant:

```bash
git clone https://github.com/HeeroML/ha-tado-x && cd ha-tado-x
python3 scripts/live_probe.py --max-rooms 6
```

What it does:

- It signs in with its own device code, the same kind of link and code as the config flow. Home Assistant's tokens are not touched.
- Tokens stay in memory unless you pass `--token-file`. That file holds only the refresh token, with mode 0600; delete it afterwards.
- Before the first request it says how many it will send and waits for `y` (or `--yes`). With `--max-rooms 6` that is about 53 requests plus the sign-in. `--history` adds one day report per room.
- It sends **GET requests only**. The only POSTs are the sign-in at login.tado.com. It stops at the first 429.
- It skips homes that are not tado X homes before any tado X request.

What it writes:

- `tado_x_probe_<time>.json`, or the file given with `--out`. Every id, serial number and name in it is replaced by a stable pseudonym (`home#1`, `room#3`, `VA04#2`), and address, coordinates, contact details and e-mail are dropped. It ends with a findings block of booleans and enums. **Read the file before you share it.**
- With `--keep-raw PATH`, an unredacted copy (mode 0600) for yourself. Do not share that one.
- With `--emit-fixtures DIR`, pseudonymized payloads in the format of `tests/fixtures/`.

## Diagnostics and privacy

Select **Download diagnostics** on the integration page (per entry) or on a device page. Diagnostics contain:

- the options;
- token metadata only: the expiry and whether a refresh token exists;
- the quota and the polling plan;
- the state of each coordinator;
- the last raw payloads of rooms, devices, home state, weather and air comfort.

Tokens, codes, user id, e-mail, address and coordinates are removed. Serial numbers are shortened to the type and the last four characters. Phone data is never included.

At no log level do the logs contain tokens, device or user codes, the sign-in link, the `Authorization` header, login responses or your e-mail. A test enforces this.

The cache in `.storage/tado_x.<entry_id>` holds the last raw payloads (never phone data) and the quota state. It is deleted with the entry.

To log every request (method, host, path, status and remaining calls):

```yaml
logger:
  logs:
    custom_components.tado_x: debug
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "This tado account has no tado X home" | The account has only older homes. Use the core tado integration for them. |
| Repair issue "… is not a tado X home" | An entry from v1.8 points at a V3+ home (upstream #40). Remove the entry and add that home with the core tado integration. |
| Repair issue "… is set up twice" | Two entries control the same home, which v1.8 allowed. Remove one. |
| Home Assistant asks you to sign in again | tado rejected the stored sign-in, for example because the refresh token was revoked or not used for 30 days after a long shutdown. Sign in again with the same account. Network problems never cause this. |
| Entities unavailable, and the log says tado is unreachable | A network or tado outage. The integration retries on its own. |
| `api_status` is `rate_limited` | The account's quota is used up; see [Quota hold](#quota-hold). Compare "API calls used (account)" with "API calls by this integration (24 h)". If the first is much higher, the core integration or the apps use most of the quota. Lower the budget or raise the reserve. |
| `api_status` is `conserving` or `throttled` | The account is close to its limit. Values update more slowly; commands still work. |
| Repair issue "Small tado API quota" | The account has no Auto-Assist (100 calls a day). Rooms update about every 45 minutes. |
| A temperature change reverts | Check the room's "Manual control duration" select and the option "Manual changes end". |
| Open window mode cannot be turned on | tado allows it only after it has detected an open window in the room. |
| Presence `auto` fails | Geofencing needs Auto-Assist. |
| Past day reports fail | tado serves past days only with Auto-Assist. |
| A phone tracker says `home` for the wrong home | Home Assistant's home zone is another place. Create a zone for this home and choose it in the option "Zone of this home", or use the at-home binary sensor. |

When you report a bug, attach the diagnostics and, if you can, a debug log. Both are free of secrets.

## Known limitations and risks

- **Not yet verified against a live home.** The tests simulate the tado cloud from the shapes in [docs/API.md](docs/API.md). Some behaviour rests on a single source and may fail on a real home: writing the room's termination setting (select and timer number), the identify button, holiday mode, the meter reading and tariff responses, and whether heating time ids match room ids. Each of these fails with a translated error instead of breaking the integration, and the [probe](#live-api-probe) checks most of them without writing anything.
- **A quota hold serves old data.** During a hold, values can be up to 6 h old for rooms (12 h for home data, 48 h for devices) while entities stay available. Automations that must not act on old values can check `api_status` and "Data updated".
- **The reset time is often unknown.** tado documents `t=` only for a used-up quota. Without it, the reset sensor is `unknown` and throttling assumes a whole day is still ahead.
- **Login calls.** It is unknown whether token refreshes at login.tado.com count against the quota. The plan leaves room for them.
- **Power loss right after a token rotation.** Home Assistant writes the config entry about 1 s after a change. If the power fails in that second, the new refresh token is lost and you have to sign in again. Home Assistant's own OAuth helper has the same exposure.
- **Entity ids of new installations.** Room devices carry the room name and suggest an area with the same name. Home Assistant 2026.10 builds new entity ids from the area and device names, so a new installation can get ids such as `climate.living_room_living_room`. Existing installations keep their ids. You can rename entity ids in the UI.
- **A device that disappears and comes back** while the entry stays loaded is registered again, but its entities return only after the entry is reloaded.
- **Presence is home-wide.** The climate presets and the presence select change the whole home, as in the tado app. The tado X API has no per-room boost; boost is a whole-home quick action.
- **Legacy keys.** The v1.8 settings stay in the config entry so that a rollback works. They show up, redacted, in diagnostics. A future major version will remove them.
- **French and Italian are partial.** Missing strings fall back to English. German is complete.

## Rolling back to v1.8.11

1.9.0 keeps the entry loadable by v1.8.11: the entry version stays 1 (minor version 2), the tokens use the old keys, and the old settings are still there.

1. Make a backup of Home Assistant.
2. In HACS, open Tado X, choose **Redownload** and pick `v1.8.11` if this repository lists it. Otherwise remove this repository in HACS (only the files go; the config entry stays), install `exabird/ha-tado-x` and download v1.8.11.
3. Restart Home Assistant. Do not delete the integration entry.

v1.8.11 then uses the stored tokens, so you do not sign in again, and it uses your old interval and toggles. Entities that only 1.9 creates stay in the entity registry as unavailable; remove them by hand if you do not upgrade again. Upgrading again later works and runs no second migration.

## Removal

1. Go to **Settings → Devices & services → Tado X**, open the menu of each entry and choose **Delete**. This removes its devices, entities and cache file.
2. In HACS, open Tado X and choose **Remove**, or delete `config/custom_components/tado_x`.
3. Restart Home Assistant.

Deleting the entry does not revoke the integration's sign-in at tado. An unused refresh token expires after 30 days.

## Credits and license

- Original integration by [exabird](https://github.com/exabird/ha-tado-x).
- Ported contributions by cmatte, bananaseason, ilyesAj, rleatherb, xiic, Somberland, agu2347, donalfenwick, LeroyK and rofra; the [changelog](CHANGELOG.md) says what came from whom.
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). API notes: [docs/API.md](docs/API.md). Development: [CONTRIBUTING.md](CONTRIBUTING.md). Plans: [ROADMAP.md](ROADMAP.md).

This project is not affiliated with tado GmbH. MIT License, see [LICENSE](LICENSE).

# tado X cloud API notes

What tado_x knows about the tado cloud API for tado X homes. tado publishes no reference for it; these notes combine the upstream integration, other open-source tado clients, tado help articles and real responses quoted in upstream issues. Nothing here comes from a live capture by this fork yet; `scripts/live_probe.py` is meant to confirm the open points.

Confidence:

- **multi-source:** at least two independent clients or documents agree;
- **single-source:** one source states it;
- **open:** conflicting or inferred.

Unless marked otherwise, an entry is multi-source.

## Hosts

| Base | Used for |
|---|---|
| `https://login.tado.com/oauth2/` | device-code sign-in, token refresh |
| `https://my.tado.com/api/v2/` | `/me`, home details (`generation`), presence, weather, phones, day reports |
| `https://hops.tado.com/homes/{homeId}/` | rooms, devices, manual control, schedules, quick actions, settings (no `/api/v2` prefix) |
| `https://minder.tado.com/v1/` | heating running times |
| `https://energy-insights.tado.com/api/` | Energy IQ meter readings and tariffs |

Conventions:

- Every API call sends `Authorization: Bearer <access token>`. JSON bodies send `Content-Type: application/json`; login POSTs are form-encoded.
- Writes often answer 204, or 200 with an empty body. Treat any 2xx as success and parse JSON only when the body is not empty; hops sometimes omits the content type.
- Error bodies: my and hops send `{"errors": [{"code": "...", "title": "..."}]}`; login sends `{"error": "...", "error_description": "...", "error_reason": "..."}`.
- Ids: home ids and room ids are integers; the room id is also the `zoneId` of my.tado.com day reports. Devices are addressed by serial number.
- Device types: `IB02` Bridge X, `VA04` Radiator Valve X, `SU04` Temperature Sensor X, `TR04` Wireless Receiver X, `RU04` Smart Thermostat X.
- The `ngsw-bypass=true` query parameter the web app sends is optional (it only matters to a browser's service worker).

### "Timezone of home not found"

hops answers `404 {"errors":[{"code":"not.found","title":"Timezone of home <id> not found"}]}` for a home that is not a tado X home (upstream #40). The integration checks `generation` first (see `/homes/{id}`) and maps this 404 to a "not a tado X home" error. Other hops 404s mean "feature not present", for example flow temperature without an OpenTherm device.

## Sign-in (OAuth 2.0 device flow)

Public client, no secret: `client_id=1bb50063-6b0c-4d11-bd99-387f4a91cc46`, `scope=offline_access` (needed for a refresh token).

| Request | Body (form) | Answer |
|---|---|---|
| `POST login/device_authorize` | `client_id`, `scope=offline_access` | `device_code`, `user_code`, `verification_uri`, `verification_uri_complete`, `expires_in` (about 300 s), `interval` (5 s) |
| `POST login/token` (polling) | `client_id`, `device_code`, `grant_type=urn:ietf:params:oauth:grant-type:device_code` | 400 `authorization_pending` until confirmed; `slow_down` (add 5 s), `expired_token`, `access_denied` per RFC 8628; then the token response |
| `POST login/token` (refresh) | `client_id`, `grant_type=refresh_token`, `refresh_token` | token response with a **new** refresh token |

Token response: `access_token` (a JWT), `token_type` (`Bearer`, case varies), `expires_in` (sources show 599 and 3599; use the returned value), `refresh_token`, `scope`.

Facts that drive the token design:

- **Rotation:** every refresh revokes the old refresh token and returns a new one (tado help article). Two refreshes with the same token: the second gets 400 `invalid_grant` / `refresh_token_not_found`. Hence one refresh at a time and "persist before use".
- **Lifetime:** a refresh token is valid for up to 30 days or until used.
- **`missing_client_id`:** `verification_uri_complete` from tado has been seen without `client_id`, and opening it gives `invalid_request` / `missing_client_id` (upstream #76). The integration always builds the link from `verification_uri` with `user_code` and `client_id`.
- **Classification:** no response, a timeout, 429 or 5xx from the login server are transient. 400 `invalid_grant` / `invalid_client` / `invalid_request`, 401 and 403 are authentication failures.
- Separate device-flow grants (the core tado integration, this integration, the probe) have separate refresh chains. Standard OAuth behaviour; no tado source states it.

## my.tado.com

| Request | Notes |
|---|---|
| `GET /me` | `id` (user UUID), `name`, `email`, `homes: [{id, name}]` (no generation), `mobileDevices`. The integration uses only `id` and `homes` |
| `GET /homes/{h}` | **`generation`: `LINE_X` (tado X) or `PRE_LINE_X`**; `dateTimeZone`, `temperatureUnit`, `isAirComfortEligible`, `isEnergyIqEligible`, `supportsFlowTemperatureOptimization` (optional), plus address and geolocation (never stored) |
| `GET /homes/{h}/state` | `presence` (`HOME`/`AWAY`), `presenceLocked` (absent without Auto-Assist). Presence setting: `AUTO` when `presenceLocked` is false or absent |
| `PUT /homes/{h}/presenceLock` | `{"homePresence": "HOME"}` or `"AWAY"`; 204 |
| `DELETE /homes/{h}/presenceLock` | back to geofencing (`AUTO`); needs Auto-Assist, 422 otherwise |
| `GET /homes/{h}/weather` | `outsideTemperature.celsius`, `solarIntensity.percentage`, `weatherState.value` (open enum: `SUN`, `SUNNY`, `CLOUDY`, `CLOUDY_PARTLY`, `CLOUDY_MOSTLY`, `DRIZZLE`, `RAIN`, `SCATTERED_RAIN`, `SNOW`, `SCATTERED_SNOW`, `RAIN_SNOW`, `SCATTERED_RAIN_SNOW`, `RAIN_HAIL`, `HAIL`, `FOGGY`, `FREEZING`, `THUNDERSTORM`, `THUNDERSTORMS`, `WINDY`, `NIGHT_CLEAR`, `NIGHT_CLOUDY`) |
| `GET /homes/{h}/mobileDevices` | `[{id, name, settings.geoTrackingEnabled, location: {atHome, stale, ...} or null, deviceMetadata: {platform, osVersion, model}}]`; `location` only with geo tracking |
| `GET /homes/{h}/zones/{room}/dayReport?date=YYYY-MM-DD` | `measuredData.insideTemperature.dataPoints[{timestamp, value.celsius}]`, `measuredData.humidity.dataPoints[{timestamp, value}]` with humidity as a **fraction 0..1**, `callForHeat.dataIntervals[{from, to, value: NONE/LOW/MEDIUM/HIGH}]`, `settings.dataIntervals`. Past days answer 403 without Auto-Assist. One call per room and day; never polled |

## hops.tado.com (`/homes/{h}/...`)

### Reads

**`GET rooms`**, the main poll, returns a list of rooms:

| Field | Shape |
|---|---|
| `id`, `name` | int, string |
| `sensorDataPoints` | `{insideTemperature: {value}, humidity: {percentage}}`, **may be null** |
| `setting` | `{power: ON/OFF, temperature: {value} or null}` |
| `manualControlTermination` | `{type: MANUAL/TIMER/NEXT_TIME_BLOCK, remainingTimeInSeconds, projectedExpiry}`; null = no manual control |
| `boostMode` | same shape; null = no boost |
| `heatingPower` | `{percentage}` |
| `connection` | `{state: CONNECTED/DISCONNECTED}` |
| `openWindow` | `{activated, expiryInSeconds}`; null = none. Read `activated` |
| `nextScheduleChange` | `{start, setting}` or null |
| `nextTimeBlock` | `{start}` or null |

`GET rooms/{room}` returns one room in the same shape.

**`GET roomsAndDevices`** returns the topology:

```text
{ rooms: [ { roomId, roomName,
             deviceManualControlTermination: {type, durationInSeconds or null},
             devices: [Device] } ],
  otherDevices: [Device] }          # the bridge, and receivers without a room
```

| Device field | Present on |
|---|---|
| `serialNumber`, `type`, `firmwareVersion`, `connection.state` | all |
| `batteryState`: `NORMAL`, `LOW`, `DEPLETED` (open enum) | battery devices |
| `temperatureAsMeasured`, `temperatureOffset` | VA04, SU04 |
| `childLockEnabled` | VA04 |
| `mountingState` (e.g. `CALIBRATED`) | VA04 |
| `controlMode`: `GAS_BOILER`, `UNDERFLOOR_HEATING`, `HEAT_PUMP`, `NO_TEMPERATURE_CONTROL` | thermostats (single-source) |
| `roomId` | some `otherDevices` entries (single-source) |

`deviceManualControlTermination` is the room's "manual control ends" setting from the tado app. Whether `durationInSeconds` is null for non-TIMER types is open.

**Other reads**

| Request | Notes |
|---|---|
| `GET airComfort` | `{freshness: {value, lastOpenWindow}, comfort: [{roomId, temperatureLevel: COLD/COMFY/WARM/HOT, humidityLevel: DRY/COMFY/HUMID}]}`. One client uses `hops.tado.com/api/v2/homes/{h}/airComfort` and one documents it on my.tado.com; the path above is the one upstream shipped for over a year |
| `GET settings/flowTemperatureOptimization` | `{maxFlowTemperature, maxFlowTemperatureConstraints: {min, max}, autoAdaptation: {enabled, maxFlowTemperature}, ...}`; **404 without an OpenTherm device** |
| `GET settings/holidayMode` | `{enabled, active, startDate, endDate, roomSettings, hotWaterSettings}`; single-source (reverse-engineered), needs Auto-Assist |
| `GET rooms/{room}/schedule` | `{room, otherRooms, schedule: [{dayType: MONDAY..SUNDAY, start: "HH:MM", end: "HH:MM" up to "24:00", setting}]}` |
| `GET settings/away/rooms/{room}` | `{roomId, mode, awayTemperatureCelsius}`; single-source, not used |
| `GET features` | `{availableFeatures: [...]}`; not used |

### Writes

| Request | Body | Notes |
|---|---|---|
| `POST rooms/{room}/manualControl` | `{"setting": {"power": "ON", "isBoost": false, "temperature": {"value": 21.5}}, "termination": {"type": "MANUAL"}}` | `termination.type` `MANUAL`, `NEXT_TIME_BLOCK`, or `TIMER` with `durationInSeconds` (only for TIMER). Off: `{"setting": {"power": "OFF"}, ...}`. 5–30 °C. Always send a termination; what tado does without one is unknown |
| `DELETE rooms/{room}/manualControl` | – | resume the schedule of one room (`POST rooms/{room}/resumeSchedule` is equivalent) |
| `POST quickActions/{action}` | – | `boost`, `allOff`, `resumeSchedule` for the whole home; 200 with an empty body. No per-room boost exists |
| `POST` / `DELETE rooms/{room}/openWindow` | – | turns the runtime open window mode on or off; turning it on only works after a detection. It is not the detection setting |
| `PATCH roomsAndDevices/devices/{serial}` | `{"temperatureOffset": -1.5}` and/or `{"childLockEnabled": true}` | offset range −9.9 to 9.9 per upstream (the server limit is unknown) |
| `POST roomsAndDevices/devices/{serial}/identify` | – | the device flashes; single-source |
| `PATCH roomsAndDevices/rooms/{room}` | `{"deviceManualControlTermination": {"type": "TIMER", "durationInSeconds": 3600}}` | writes the app's per-room setting; **single-source** (upstream PR #88) |
| `POST rooms/{room}/schedule` | `{"dayType": "TUESDAY", "daySchedule": [{"dayType": "TUESDAY", "start": "00:00", "end": "06:30", "setting": {"power": "ON", "temperature": {"value": 17.0}}}, ...]}` | **one weekday per request**; blocks cover 00:00–24:00 without gaps; OFF blocks have `"temperature": null`. Aggregate day types are not known to work on tado X |
| `PATCH settings/flowTemperatureOptimization` | `{"maxFlowTemperature": 45}` and/or `{"autoAdaptation": {"enabled": true}}` | |
| `PATCH settings/holidayMode` | the GET result with `enabled`, `startDate`, `endDate` replaced | single-source; read-modify-write, abort if the read fails |

### Domestic hot water (not implemented)

Documented for upstream PRs #53 and #74; the integration does not use these.

| Request | Notes |
|---|---|
| `GET programmer/domesticHotWater` | `{isDomesticHotWaterCapable}` |
| `GET programmer/domesticHotWater/state` | `{state, nextStateChange, setpoint, setpointConstraints}`; states conflict between sources (`SCHEDULE_ON`, `SCHEDULE_OFF`, `BOOST_ON`, `BOOST_OFF`, `NONE`, `OFF`, `BOOST`, `MANUAL_ON`) |
| `POST programmer/domesticHotWater/boost` | `{"boost": "ON"}` or `"OFF"`; boost lasts 60 minutes |
| `POST programmer/domesticHotWater/resumeSchedule` | – |
| `POST programmer/domesticHotWater/manualControl` | `{"setpoint": 48}`; single-source |
| `GET` / `PUT programmer/domesticHotWater/schedule` | one weekday per PUT |

## minder and Energy IQ

| Request | Notes |
|---|---|
| `GET minder/homes/{h}/runningTimes?from=D&to=D` | `{runningTimes: [{runningTimeInSeconds, zones: [{id, runningTimeInSeconds}]}], summary: {...}}`. Whether `zones[].id` equals the hops room id is **single-source** (upstream). D is today in the home's time zone |
| `POST eiq/homes/{h}/meterReadings` | `{"date": "YYYY-MM-DD", "reading": 12345}`; errors may come back as a 2xx with a `message` field (single-source) |
| `POST eiq/homes/{h}/tariffs` | `{"tariffInCents": 29, "unit": "m3" or "kWh", "startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD"}`. The integration sends whole cents, rounded half up |
| `GET eiq/homes/{h}/tariffs` | a list or `{tariffs: [...]}` depending on the source; parse both |

## Rate limits

- **Quota:** 100 requests a day, or 20,000 with Auto-Assist or AI Assist, **per account** (tado help article 12165739, January 2026). All clients of the account share it: the core tado integration, this integration and possibly the apps.
- **Headers** (names are case-insensitive):

  ```text
  ratelimit-policy: "perday";q=20000;w=86400
  ratelimit:        "perday";r=17833
  ratelimit:        "perday";r=0;t=7082      when used up
  ```

  `q` is the limit, `w` the window in seconds, `r` the remaining requests, `t` the seconds until the quota refills. tado documents `t` with `r=0`; whether it also appears while `r > 0` is open.
- **Reset time:** sources disagree (12:00 UTC, about 11:30 UTC, 00:00 UTC, or a rolling window). The integration uses only `t`.
- **429:** carries the same `ratelimit` headers; read them before handling the status. The body has never been captured.
- **Which hosts count:** my.tado.com and hops do. minder, Energy IQ and login are open. Whether a 429 itself counts is open.

## Open questions for a live check

Ordered by importance; `scripts/live_probe.py` answers items 2–10 read-only.

1. The room termination PATCH is accepted and shows in the app; `manualControl` with `NEXT_TIME_BLOCK` and `TIMER`; presence lock `DELETE`; an open window `POST` without a detection gives a 4xx. (Writes; needs a manual test.)
2. `durationInSeconds` in `deviceManualControlTermination` for non-TIMER types.
3. `t=` while `r > 0`; which hosts send `ratelimit` headers; the 429 body.
4. Which hosts reduce `r` (measured by comparing `r` before and after a call).
5. `generation`, `supportsFlowTemperatureOptimization` and `isAirComfortEligible` of real homes.
6. Whether `verification_uri_complete` contains `client_id`; `expires_in`; the case of `token_type`.
7. Whether minder zone ids equal hops room ids.
8. The holiday mode shape; the tariff list shape.
9. `controlMode`, `childLockEnabled` and `mountingState` per device type; whether `openWindow.activated: false` occurs.
10. The air comfort path.

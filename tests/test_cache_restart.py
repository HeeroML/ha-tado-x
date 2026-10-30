"""Tests for restarts during a quota hold and the Store cache (#62)."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tado_x.cache import SAVE_DELAY, SnapshotCache
from custom_components.tado_x.client import RoomState
from custom_components.tado_x.const import DOMAIN
from custom_components.tado_x.coordinator import home_today
from custom_components.tado_x.runtime import FeatureStatus, RoomsSnapshot
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from tests.tado_mock import HOME_B, HOME_C, TadoCloudMock, load_fixture
from tests.test_init import PLATFORMS_STUBBED, make_entry, state

START = "2026-10-30T10:00:00+00:00"
CACHED_AT = datetime(2026, 10, 30, 9, 30, tzinfo=UTC)
PAYLOAD_FIXTURES = {
    "rooms": "rooms.json",
    "topology": "rooms_and_devices.json",
    "home_state": "state.json",
    "weather": "weather.json",
    "air_comfort": "air_comfort.json",
    "home_info": "home_1000002.json",
    "running_times": "running_times.json",
}


def _key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def _store(
    entry: MockConfigEntry,
    *,
    at: datetime = CACHED_AT,
    home_id: int = HOME_B,
    schema: int = 1,
    payloads: dict[str, Any] | None = None,
    calls: list[int] | None = None,
    flow_status: str | None = "absent",
) -> dict[str, Any]:
    """Return the hass_storage content of a cache written by an earlier run."""
    raw = payloads or {
        key: load_fixture(name) for key, name in PAYLOAD_FIXTURES.items()
    }
    return {
        _key(entry): {
            "version": 1,
            "minor_version": 1,
            "key": _key(entry),
            "data": {
                "schema": schema,
                "home_id": home_id,
                "payloads": {
                    key: {"at": at.isoformat(), "raw": value}
                    for key, value in raw.items()
                },
                "quota": {},
                "calls": calls or [],
                "flow_status": flow_status,
            },
        }
    }


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Start every test at the same time, before the fake cloud plans its reset."""
    freezer.move_to(START)


async def _start(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    **options: Any,
) -> None:
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float, times: int = 1
) -> None:
    for _ in range(times):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_restart_during_quota_hold_serves_the_cache(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A 429 at startup loads the entry from the cache and resumes after the reset."""
    entry = make_entry()
    hass_storage.update(_store(entry))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry)

    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data
    assert data.rooms.data == RoomsSnapshot(
        rooms=RoomState.list_from_api(load_fixture("rooms.json"))
    )
    assert data.rooms.last_live_update == CACHED_AT
    assert data.rooms.stale_since == dt_util.utcnow()
    assert data.home.data.weather is not None
    assert data.home.data.air_comfort is not None
    assert data.devices.data.home_info is not None
    assert data.devices.data.running_times is not None
    assert data.devices.data.flow_status is FeatureStatus.ABSENT
    assert issue_registry.async_get_issue(DOMAIN, f"quota_exhausted_{entry.entry_id}")
    # The first 429 closes the gate: the rest of the startup stays local.
    assert [call.url.path for call in mock_tado.calls()] == [
        "/homes/1000002/roomsAndDevices"
    ]

    entry.async_on_unload(data.rooms.async_add_listener(lambda: None))
    mock_tado.clear_calls()
    await _tick(hass, freezer, 300, times=23)

    assert mock_tado.calls() == []
    assert data.rooms.last_update_success

    await _tick(hass, freezer, 300, times=2)

    assert mock_tado.calls("GET", "/homes/1000002/rooms")
    assert data.rooms.stale_since is None
    assert data.rooms.last_live_update is not None
    assert data.rooms.last_live_update > CACHED_AT
    assert not issue_registry.async_get_issue(
        DOMAIN, f"quota_exhausted_{entry.entry_id}"
    )


@pytest.mark.skipif(
    PLATFORMS_STUBBED, reason="the platforms of WP4 and WP5 are interface stubs"
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_restart_during_quota_hold_creates_every_entity(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every entity of home B exists with cached values during a quota hold."""
    entry = make_entry()
    hass_storage.update(_store(entry))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry)

    states = hass.states.async_all()
    assert len(states) == 216
    assert [
        state.entity_id for state in states if state.state == STATE_UNAVAILABLE
    ] == []


async def test_quota_hold_without_cache_retries_without_requests(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fresh install during a hold retries setup with the persisted gate closed."""
    caplog.set_level(logging.DEBUG, logger="homeassistant.config_entries")
    entry = make_entry()
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "setup_quota_exhausted"
    stored = hass_storage[_key(entry)]["data"]
    assert stored["quota"]["blocked_until"] is not None

    mock_tado.clear_calls()
    await _tick(hass, freezer, 60, times=6)

    assert mock_tado.calls() == []
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert caplog.text.count("not ready yet") >= 3

    await _tick(hass, freezer, 7200)
    await _tick(hass, freezer, 600)

    assert state(entry) is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    "changes",
    [
        {"home_id": HOME_C},
        {"schema": 2},
        {"payloads": {"rooms": {"not": "rooms"}, "topology": {"rooms": "x"}}},
    ],
    ids=["other_home", "other_schema", "unreadable"],
)
async def test_unusable_cache_is_ignored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    changes: dict[str, Any],
) -> None:
    """A cache of another home or schema, or with unreadable payloads, is no seed."""
    entry = make_entry()
    hass_storage.update(_store(entry, **changes))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_cache_older_than_max_stale_is_not_served(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device cache older than 48 hours is no seed."""
    entry = make_entry()
    hass_storage.update(_store(entry, at=CACHED_AT - timedelta(days=3)))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "setup_quota_exhausted"


async def test_unreadable_optional_slice_is_dropped(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One broken cached payload loses only its own values."""
    entry = make_entry()
    payloads = {key: load_fixture(name) for key, name in PAYLOAD_FIXTURES.items()}
    payloads["weather"] = ["not", "weather"]
    payloads["flow_temperature"] = load_fixture("flow_temperature.json")
    payloads["holiday_mode"] = load_fixture("holiday_mode.json")
    hass_storage.update(_store(entry, payloads=payloads, flow_status="present"))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(hass, freezer, entry, holiday_mode=True)

    data = entry.runtime_data
    assert data.home.data.weather is None
    assert data.home.data.air_comfort is not None
    assert data.devices.data.flow_status is FeatureStatus.PRESENT
    assert data.devices.data.flow_temperature is not None
    assert data.devices.data.holiday_mode is not None
    running_times = data.devices.data.running_times
    assert running_times is not None
    assert running_times.day == home_today(data.devices.data.home_info, CACHED_AT)


async def test_cache_of_disabled_features_is_not_served(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cached values of features turned off since, or found absent, stay unused."""
    entry = make_entry()
    payloads = {key: load_fixture(name) for key, name in PAYLOAD_FIXTURES.items()}
    payloads["flow_temperature"] = load_fixture("flow_temperature.json")
    payloads["holiday_mode"] = load_fixture("holiday_mode.json")
    hass_storage.update(_store(entry, payloads=payloads, flow_status="absent"))
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _start(
        hass, freezer, entry, weather=False, air_comfort=False, running_times=False
    )

    data = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert data.home.data.weather is None
    assert data.home.data.air_comfort is None
    assert data.devices.data.running_times is None
    assert data.devices.data.flow_temperature is None
    assert data.devices.data.flow_status is FeatureStatus.ABSENT
    assert data.devices.data.holiday_mode is None


async def test_live_data_replaces_the_cache(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With quota left, the first refresh reads everything live."""
    entry = make_entry()
    payloads = {key: load_fixture(name) for key, name in PAYLOAD_FIXTURES.items()}
    payloads["rooms"][0]["name"] = "Cached name"
    hass_storage.update(_store(entry, payloads=payloads))

    await _start(hass, freezer, entry)

    data = entry.runtime_data
    assert data.rooms.data.rooms[1].name == "Wohnzimmer"
    assert data.rooms.stale_since is None
    assert data.rooms.last_live_update == dt_util.utcnow()


async def test_call_counter_survives_a_restart(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The calls of the last 24 hours are restored from the cache."""
    entry = make_entry()
    minute = int(datetime.fromisoformat(START).timestamp()) // 60 - 10
    buckets = [0] * 1440
    buckets[minute % 1440] = 7
    hass_storage.update(_store(entry, calls=[minute, *buckets]))

    await _start(hass, freezer, entry)

    assert entry.runtime_data.monitor.calls_24h() == 7 + len(mock_tado.calls())


async def test_phone_data_is_never_stored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Phones are read but never written to disk."""
    entry = make_entry()
    payloads = {key: load_fixture(name) for key, name in PAYLOAD_FIXTURES.items()}
    payloads["mobile_devices"] = load_fixture("mobile_devices.json")
    hass_storage.update(_store(entry, payloads=payloads))

    await _start(hass, freezer, entry, mobile_devices=True)
    assert entry.runtime_data.home.data.mobile_devices
    assert await hass.config_entries.async_unload(entry.entry_id)

    stored = hass_storage[_key(entry)]["data"]
    assert "mobile_devices" not in stored["payloads"]
    assert "Phone" not in str(stored)


async def test_saves_are_delayed(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cache is written 600 s after a change at the earliest, then flushed on unload."""
    entry = make_entry()
    await _start(hass, freezer, entry)
    entry.async_on_unload(entry.runtime_data.rooms.async_add_listener(lambda: None))

    await _tick(hass, freezer, 45, times=13)
    assert _key(entry) not in hass_storage

    await _tick(hass, freezer, SAVE_DELAY - 13 * 45 + 1)
    stored = hass_storage[_key(entry)]["data"]
    written_at = datetime.fromisoformat(stored["payloads"]["rooms"]["at"])
    assert stored["quota"]["latest"]["remaining"] is not None

    await _tick(hass, freezer, 45)
    assert hass_storage[_key(entry)]["data"] == stored

    assert await hass.config_entries.async_unload(entry.entry_id)
    flushed = hass_storage[_key(entry)]["data"]
    assert datetime.fromisoformat(flushed["payloads"]["rooms"]["at"]) > written_at


async def test_late_responses_do_not_write_after_removal(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A response that arrives after the unload leaves the Store of a removed entry gone."""
    entry = make_entry()
    await _start(hass, freezer, entry)
    cache = entry.runtime_data.cache
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    assert _key(entry) not in hass_storage

    cache.record(HOME_B, "rooms", [], dt_util.utcnow())
    cache.set_quota({}, [])
    cache.set_flow_status(FeatureStatus.PRESENT)
    await _tick(hass, freezer, SAVE_DELAY + 1)

    assert _key(entry) not in hass_storage
    assert cache.payload("rooms") != (dt_util.utcnow(), [])


async def test_cache_load_ignores_unreadable_parts(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Unreadable items of a stored cache are skipped one by one."""
    hass_storage["tado_x.entry"] = {
        "version": 1,
        "minor_version": 1,
        "key": "tado_x.entry",
        "data": {
            "schema": 1,
            "home_id": HOME_B,
            "payloads": {
                "rooms": {"at": CACHED_AT.isoformat(), "raw": []},
                "weather": {"at": "2026-10-30T09:30:00", "raw": {}},
                "home_state": {"at": "yesterday", "raw": {}},
                "air_comfort": {"at": 42, "raw": {}},
                "topology": {"at": CACHED_AT.isoformat()},
                "home_info": "broken",
                "mobile_devices": {"at": CACHED_AT.isoformat(), "raw": []},
            },
            "quota": "broken",
            "calls": [1, "2"],
            "flow_status": "sometimes",
        },
    }
    cache = SnapshotCache(hass, "entry", HOME_B)

    seed = await cache.async_load()

    assert seed is not None
    assert set(seed.payloads) == {"rooms"}
    assert seed.quota == {}
    assert seed.calls == []
    assert seed.flow_status is None

    hass_storage["tado_x.entry"]["data"]["payloads"] = []
    hass_storage["tado_x.entry"]["data"]["flow_status"] = 1
    other = SnapshotCache(hass, "entry", HOME_B)
    seed = await other.async_load()
    assert seed is not None
    assert seed.payloads == {}
    assert seed.flow_status is None


async def test_cache_records_only_its_own_home(hass: HomeAssistant) -> None:
    """Payloads of another home or of phones are ignored."""
    cache = SnapshotCache(hass, "entry", HOME_B)

    cache.record(HOME_C, "rooms", [], CACHED_AT)
    cache.record(HOME_B, "mobile_devices", [], CACHED_AT)
    cache.set_flow_status(FeatureStatus.ABSENT)
    cache.set_flow_status(FeatureStatus.ABSENT)

    assert cache.payload("rooms") is None
    assert cache.metadata() == {
        "schema": 1,
        "slices": {},
        "flow_status": FeatureStatus.ABSENT,
        "unsaved_changes": True,
    }

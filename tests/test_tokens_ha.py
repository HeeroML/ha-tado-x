"""Tests for token rotation through the config entry (#75, #79, #80, #89)."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    CONF_USER_ID,
)
from custom_components.tado_x.token_store import (
    EXPIRED,
    ConfigEntryTokenStore,
    parse_expiry,
)
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from tests.tado_mock import HOME_B, HOME_C, TadoCloudMock, entry_tokens
from tests.test_climate import entity_id
from tests.test_init import make_entry, setup_entry, state

TOKEN_PATH = "/oauth2/token"
EXPIRED_ISO = "2026-01-01T00:00:00+00:00"


async def _until(condition: Any, rounds: int = 200) -> None:
    """Let the event loop run until condition() is true."""
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition not reached")


def _bearer(call: Any) -> str:
    return str(call.headers["Authorization"]).removeprefix("Bearer ")


async def _press(hass: HomeAssistant, button: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )


async def test_rotation_is_persisted_before_the_new_token_is_used(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> None:
    """The rotated tokens are in entry.data before any request carries them."""
    entry = make_entry(token_expiry=EXPIRED_ISO)
    release = mock_tado.hold("GET", "/homes/1000002/roomsAndDevices")
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))

    await _until(lambda: mock_tado.calls("GET", "/homes/1000002/roomsAndDevices"))
    first_use = mock_tado.calls("GET", "/homes/1000002/roomsAndDevices")[0]
    assert entry.data[CONF_ACCESS_TOKEN] == _bearer(first_use)
    assert entry.data[CONF_REFRESH_TOKEN] == "fake-refresh-token-0001"
    expiry = datetime.fromisoformat(entry.data[CONF_TOKEN_EXPIRY])
    assert expiry.tzinfo is not None
    assert expiry.utcoffset() == timedelta(0)

    release.set()
    await setup
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 1


async def test_fresh_token_needs_no_refresh(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A token that is still valid at startup is used as it is."""
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert mock_tado.calls("POST", TOKEN_PATH) == []


async def test_naive_expiry_of_1_8_refreshes_once(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> None:
    """A naive token_expiry written by 1.8 means one lazy refresh, then aware UTC."""
    entry = make_entry(token_expiry="2026-10-30T12:00:00")

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 1
    assert datetime.fromisoformat(entry.data[CONF_TOKEN_EXPIRY]).tzinfo is not None


async def _start_scheduled_refresh(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> asyncio.Event:
    """Let the poll timers fire with a stale token; hold the refresh they send."""
    release = mock_tado.hold("POST", TOKEN_PATH)
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await _until(lambda: mock_tado.calls("POST", TOKEN_PATH))
    return release


async def _spin(rounds: int = 100) -> None:
    """Let the event loop run for a while without a condition to wait for."""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_reload_during_a_scheduled_refresh_keeps_the_rotation(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A reload waits for the refresh a poll started, then reuses its tokens."""
    entry = make_entry()
    await setup_entry(hass, entry)
    release = await _start_scheduled_refresh(hass, mock_tado, freezer)

    reload = hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    await _until(lambda: entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS)
    await _spin()
    assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
    release.set()
    assert await reload
    await hass.async_block_till_done()

    assert state(entry) is ConfigEntryState.LOADED
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 1
    assert entry.data[CONF_REFRESH_TOKEN] == "fake-refresh-token-0001"
    rotated = entry.data[CONF_ACCESS_TOKEN]
    assert rotated == "fake-access-token-0001"
    last_reads = mock_tado.calls("GET", "/homes/1000002/rooms")[-1]
    assert _bearer(last_reads) == rotated
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_stop_during_a_scheduled_refresh_keeps_the_rotation(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Stopping Home Assistant waits for the refresh and writes the rotation to disk."""
    entry = make_entry()
    await setup_entry(hass, entry)
    release = await _start_scheduled_refresh(hass, mock_tado, freezer)

    stop = asyncio.create_task(hass.async_stop())
    await _spin()
    assert hass.state is CoreState.running
    release.set()
    await stop

    assert entry.data[CONF_REFRESH_TOKEN] == "fake-refresh-token-0001"
    [stored] = [
        stored
        for stored in hass_storage["core.config_entries"]["data"]["entries"]
        if stored["entry_id"] == entry.entry_id
    ]
    assert stored["data"][CONF_REFRESH_TOKEN] == "fake-refresh-token-0001"
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 1


async def test_login_stored_during_a_refresh_survives_it(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Tokens a reconfigure stores while a poll refreshes the old grant are kept."""
    entry = make_entry()
    await setup_entry(hass, entry)
    release = await _start_scheduled_refresh(hass, mock_tado, freezer)
    access, refresh = entry_tokens(HOME_C)

    # What async_update_reload_and_abort writes when the other account signed in;
    # the answer to the refresh of the old grant arrives before the reload.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_USER_ID: "00000000-0000-4000-8000-000000000002",
            CONF_ACCESS_TOKEN: access,
            CONF_REFRESH_TOKEN: refresh,
            CONF_TOKEN_EXPIRY: (dt_util.utcnow() + timedelta(minutes=10)).isoformat(),
        },
    )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data[CONF_REFRESH_TOKEN] == refresh
    assert entry.data[CONF_ACCESS_TOKEN] == access

    hass.config_entries.async_schedule_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_REFRESH_TOKEN] == refresh
    last_reads = mock_tado.calls("GET", "/homes/1000002/rooms")[-1]
    assert _bearer(last_reads) == access


@pytest.mark.parametrize("old_grant_valid", [True, False], ids=["rotated", "rejected"])
async def test_login_used_during_a_cancelled_press_survives_its_refresh(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    old_grant_valid: bool,
) -> None:
    """A new login a later press used outlives the refresh of a cancelled press.

    An automation that restarts while its button press waits for the token
    leaves the refresh running. A reauth stores a new login, and the press of
    the restarted run uses it before tado answers the refresh of the old grant.
    """
    expiry = dt_util.utcnow() + timedelta(seconds=61)
    entry = make_entry(token_expiry=expiry.isoformat())
    await setup_entry(hass, entry)
    access, refresh = entry_tokens(HOME_C)
    if not old_grant_valid:
        mock_tado.valid_refresh_tokens.discard(entry.data[CONF_REFRESH_TOKEN])
    # The token needs a refresh now, but no poll is due: only the press waits.
    freezer.tick(timedelta(seconds=2))
    release = mock_tado.hold("POST", TOKEN_PATH)
    button = entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_resume_schedules")
    cancelled = hass.async_create_task(_press(hass, button))
    await _until(lambda: mock_tado.calls("POST", TOKEN_PATH))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    # The tokens a reauth writes; the reload it schedules comes after the answer.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_ACCESS_TOKEN: access,
            CONF_REFRESH_TOKEN: refresh,
            CONF_TOKEN_EXPIRY: (dt_util.utcnow() + timedelta(minutes=10)).isoformat(),
        },
    )
    await _press(hass, button)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.data[CONF_ACCESS_TOKEN] == access
    assert entry.data[CONF_REFRESH_TOKEN] == refresh
    await _press(hass, button)
    await hass.async_block_till_done()
    presses = mock_tado.calls("POST", f"/homes/{HOME_B}/quickActions/resumeSchedule")
    assert [_bearer(call) for call in presses] == [access, access]
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 1
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_network_error_during_refresh_is_transient(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A refresh without connection fails the poll but never asks to sign in."""
    entry = make_entry()
    await setup_entry(hass, entry)
    rooms = entry.runtime_data.rooms
    freezer.tick(timedelta(minutes=10))
    mock_tado.fail("POST", TOKEN_PATH, exc=ClientConnectionError(), times=1)

    await rooms.async_refresh()

    assert not rooms.last_update_success
    assert entry.state is ConfigEntryState.LOADED
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))

    freezer.tick(timedelta(seconds=10))
    await rooms.async_refresh()

    assert rooms.last_update_success
    assert entry.data[CONF_ACCESS_TOKEN] == "fake-access-token-0001"


async def test_server_error_during_refresh_keeps_a_valid_token(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A 503 of the login server inside the refresh margin keeps the old token."""
    entry = make_entry()
    await setup_entry(hass, entry)
    rooms = entry.runtime_data.rooms
    old = entry.data[CONF_ACCESS_TOKEN]
    freezer.tick(timedelta(minutes=9, seconds=30))
    mock_tado.fail("POST", TOKEN_PATH, status=503, times=1)

    await rooms.async_refresh()

    assert rooms.last_update_success
    assert entry.data[CONF_ACCESS_TOKEN] == old
    assert _bearer(mock_tado.calls("GET", "/homes/1000002/rooms")[-1]) == old


async def test_invalid_grant_starts_reauth_while_loaded(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Tado rejecting the refresh token makes the entry ask for a new sign-in."""
    entry = make_entry()
    await setup_entry(hass, entry)
    freezer.tick(timedelta(minutes=10))
    mock_tado.valid_refresh_tokens.clear()

    await entry.runtime_data.rooms.async_refresh()
    await hass.async_block_till_done()

    assert not entry.runtime_data.rooms.last_update_success
    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_token_store_reads_flat_keys(hass: HomeAssistant) -> None:
    """Missing tokens load as None; the expiry is aware or expired."""
    entry = make_entry(access_token=None)
    entry.add_to_hass(hass)
    store = ConfigEntryTokenStore(hass, entry)

    assert store.load() is None

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: "fake-access"}
    )
    tokens = store.load()

    assert tokens is not None
    assert tokens.access_token == "fake-access"
    assert tokens.expires_at.tzinfo is not None


@pytest.mark.parametrize(
    "raw",
    [None, 42, "", "not a time", "2026-10-30T12:00:00"],
    ids=["missing", "number", "empty", "garbage", "naive"],
)
def test_unreadable_expiry_is_expired(raw: object) -> None:
    """The host time zone is never guessed for a naive expiry."""
    assert parse_expiry(raw) == EXPIRED

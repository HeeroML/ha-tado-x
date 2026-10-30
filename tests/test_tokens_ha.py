"""Tests for token rotation through the config entry (#75, #79, #80, #89)."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
)
from custom_components.tado_x.token_store import (
    EXPIRED,
    ConfigEntryTokenStore,
    parse_expiry,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from tests.tado_mock import TadoCloudMock
from tests.test_init import make_entry, setup_entry

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


async def test_reload_during_refresh_uses_the_rotated_token(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A reload waits for an in-flight refresh and then reuses its tokens."""
    entry = make_entry()
    await setup_entry(hass, entry)
    freezer.tick(timedelta(minutes=10))
    release = mock_tado.hold("POST", TOKEN_PATH)
    refresh = hass.async_create_task(entry.runtime_data.rooms.async_refresh())
    await _until(lambda: mock_tado.calls("POST", TOKEN_PATH))

    reload = hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    await _until(lambda: entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS)
    release.set()
    await refresh
    assert await reload
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    posts = mock_tado.calls("POST", TOKEN_PATH)
    assert len(posts) == 1
    rotated = entry.data[CONF_ACCESS_TOKEN]
    assert rotated == "fake-access-token-0001"
    last_reads = mock_tado.calls("GET", "/homes/1000002/rooms")[-1]
    assert _bearer(last_reads) == rotated


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

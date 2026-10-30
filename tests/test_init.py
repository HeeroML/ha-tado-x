"""Tests for setting up, unloading and removing a tado X entry."""

import ast
from collections.abc import Generator
from datetime import timedelta
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import ClientConnectionError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tado_x import PLATFORMS, async_remove_config_entry_device
from custom_components.tado_x.cache import SnapshotCache
from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.tado_x.coordinator import (
    DevicesCoordinator,
    HomeCoordinator,
    RoomsCoordinator,
)
from custom_components.tado_x.issues import (
    async_create_breaking_changes_issue,
    async_create_duplicate_home_issue,
)
from custom_components.tado_x.quota import QuotaMonitor
from custom_components.tado_x.runtime import TadoXRuntimeData
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util
from tests.tado_mock import HOME_A, HOME_B, USER_ID, TadoCloudMock, entry_tokens

PACKAGE = Path(__file__).parent.parent / "custom_components" / "tado_x"
FORBIDDEN_LOG_WORDS = ("deprecated", "via_device", "location_name", "config_entry")


def is_stub(module: str) -> bool:
    """Return whether a module is still the interface stub of an unmerged package."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Name)
        and node.exc.id == "NotImplementedError"
        for node in ast.walk(tree)
    )


PLATFORMS_STUBBED = any(is_stub(platform.value) for platform in PLATFORMS)
requires_config_flow = pytest.mark.skipif(
    is_stub("config_flow"),
    reason="a reauth flow needs config_flow.py, still the WP3 interface stub",
)
requires_services = pytest.mark.skipif(
    is_stub("services"), reason="services.py is still the WP4 interface stub"
)


@pytest.fixture(autouse=True)
def skip_service_stub() -> Generator[None]:
    """Set up without services while services.py is the WP4 interface stub.

    The guard turns itself off once WP4 replaces the stub.
    """
    if not is_stub("services"):
        yield
        return
    with patch("custom_components.tado_x.async_setup_services"):
        yield


def entry_data(home_id: int = HOME_B, **changes: Any) -> dict[str, Any]:
    """Return the data of a 1.2 entry using the fake tokens of home 1000002."""
    access, refresh = entry_tokens(HOME_B)
    data = {
        CONF_HOME_ID: home_id,
        CONF_HOME_NAME: f"Haus {home_id}",
        CONF_USER_ID: USER_ID,
        CONF_ACCESS_TOKEN: access,
        CONF_REFRESH_TOKEN: refresh,
        CONF_TOKEN_EXPIRY: (dt_util.utcnow() + timedelta(minutes=10)).isoformat(),
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def make_entry(home_id: int = HOME_B, **changes: Any) -> MockConfigEntry:
    """Return a 1.2 entry for a home; not added to hass."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Haus {home_id}",
        unique_id=str(home_id),
        version=1,
        minor_version=2,
        data=entry_data(home_id, **changes),
        options=dict(DEFAULT_OPTIONS),
    )


def state(entry: ConfigEntry[Any]) -> ConfigEntryState:
    """Return the state of an entry, read anew after an await."""
    return entry.state


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add an entry to hass and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A loaded entry owns typed runtime data; unloading flushes the cache."""
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    data = config_entry.runtime_data
    assert isinstance(data, TadoXRuntimeData)
    assert isinstance(data.rooms, RoomsCoordinator)
    assert isinstance(data.home, HomeCoordinator)
    assert isinstance(data.devices, DevicesCoordinator)
    assert isinstance(data.monitor, QuotaMonitor)
    assert isinstance(data.cache, SnapshotCache)
    assert data.api.home_id == HOME_B
    assert data.monitor.plan.calls_per_day == 2521
    assert f"{DOMAIN}.{config_entry.entry_id}" not in hass_storage

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(config_entry) is ConfigEntryState.NOT_LOADED
    stored = hass_storage[f"{DOMAIN}.{config_entry.entry_id}"]["data"]
    assert stored["home_id"] == HOME_B
    assert "rooms" in stored["payloads"]


async def test_first_setup_reads_each_enabled_endpoint_once(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A fresh install reads every enabled endpoint once and no token refresh."""
    await setup_entry(hass, config_entry)

    assert sorted(call.url.path for call in mock_tado.calls()) == sorted(
        [
            "/homes/1000002/roomsAndDevices",
            "/api/v2/homes/1000002",
            "/v1/homes/1000002/runningTimes",
            "/homes/1000002/settings/flowTemperatureOptimization",
            "/homes/1000002/rooms",
            "/api/v2/homes/1000002/state",
            "/api/v2/homes/1000002/weather",
            "/homes/1000002/airComfort",
        ]
    )


async def test_no_deprecation_warnings(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full setup and unload log no deprecation of Home Assistant 2026.10."""
    caplog.set_level(logging.WARNING)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "mobile_devices": True, "holiday_mode": True},
    )

    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    offending = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and any(word in record.getMessage() for word in FORBIDDEN_LOG_WORDS)
    ]
    assert offending == []


async def test_network_error_retries_setup(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """No connection to tado means a setup retry, never a reauth."""
    mock_tado.fail(None, "/homes/1000002/roomsAndDevices", exc=ClientConnectionError())

    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not list(config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_server_error_retries_setup(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A 5xx of a required endpoint means a setup retry."""
    mock_tado.fail("GET", "/homes/1000002/rooms", status=503)

    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@requires_config_flow
async def test_invalid_grant_starts_reauth(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> None:
    """A refresh token tado no longer accepts fails setup and starts a reauth."""
    entry = make_entry(token_expiry="2026-01-01T00:00:00+00:00")
    mock_tado.valid_refresh_tokens.clear()

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_home_of_the_previous_generation(
    hass: HomeAssistant, mock_tado: TadoCloudMock, issue_registry: ir.IssueRegistry
) -> None:
    """A migrated entry of a V3+ home fails for good and raises an issue (#40)."""
    entry = make_entry(HOME_A)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue = issue_registry.async_get_issue(DOMAIN, f"not_tado_x_home_{entry.entry_id}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"home": "Haus 1000001"}


async def test_migrated_entry_learns_its_account(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> None:
    """An entry without user_id reads /me once and keeps the account id."""
    entry = make_entry(user_id=None)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_USER_ID] == USER_ID
    assert len(mock_tado.calls("GET", "/api/v2/me")) == 1


async def test_migrated_entry_without_account_access_still_loads(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> None:
    """Reading /me is best effort."""
    entry = make_entry(user_id=None)
    mock_tado.fail("GET", "/api/v2/me", status=500)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert CONF_USER_ID not in entry.data


async def test_remove_entry_deletes_cache_and_issues(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    issue_registry: ir.IssueRegistry,
) -> None:
    """Removing an entry deletes its Store and its issues."""
    mock_tado.set_quota(limit=500)
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    key = f"{DOMAIN}.{config_entry.entry_id}"
    assert key in hass_storage
    assert issue_registry.async_get_issue(
        DOMAIN, f"low_quota_tier_{config_entry.entry_id}"
    )

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage
    assert not issue_registry.async_get_issue(
        DOMAIN, f"low_quota_tier_{config_entry.entry_id}"
    )


async def test_remove_config_entry_device(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Only devices tado no longer reports can be removed."""
    await setup_entry(hass, config_entry)
    valve = device_registry.async_get_device_by_identifier(
        (DOMAIN, "VA0000000101"), config_entry.entry_id
    )
    gone = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "VA0000009999")},
        name="Old valve",
    )
    assert valve is not None

    assert not await async_remove_config_entry_device(hass, config_entry, valve)
    assert await async_remove_config_entry_device(hass, config_entry, gone)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await async_remove_config_entry_device(hass, config_entry, valve)


async def test_entries_of_two_homes_load_independently(
    hass: HomeAssistant,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
    mock_tado: TadoCloudMock,
) -> None:
    """Each home has its own client; unloading one leaves the other running."""
    first, second = two_home_entries

    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED
    assert first.runtime_data.client is not second.runtime_data.client
    assert mock_tado.calls("GET", "/homes/1000003/rooms")

    assert await hass.config_entries.async_unload(first.entry_id)

    assert second.state is ConfigEntryState.LOADED


@requires_services
async def test_services_registered_once_for_two_entries(
    hass: HomeAssistant,
    two_home_entries: tuple[MockConfigEntry, MockConfigEntry],
) -> None:
    """Services belong to the integration, not to an entry."""
    services = set(hass.services.async_services_for_domain(DOMAIN))
    assert services

    assert await hass.config_entries.async_unload(two_home_entries[0].entry_id)

    assert set(hass.services.async_services_for_domain(DOMAIN)) == services


async def test_issues_of_the_migration(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """The migration reports a duplicate home and the breaking changes of 1.9."""
    entry = make_entry()
    entry.add_to_hass(hass)

    async_create_duplicate_home_issue(hass, entry)
    async_create_breaking_changes_issue(hass)

    duplicate = issue_registry.async_get_issue(
        DOMAIN, f"duplicate_home_{entry.entry_id}"
    )
    assert duplicate is not None
    assert duplicate.translation_placeholders == {"home": "Haus 1000002"}
    assert not duplicate.is_fixable
    breaking = issue_registry.async_get_issue(DOMAIN, "breaking_changes_1_9")
    assert breaking is not None
    assert breaking.is_persistent
    assert breaking.severity is ir.IssueSeverity.WARNING

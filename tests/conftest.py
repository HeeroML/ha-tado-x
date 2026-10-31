"""Fixtures for tado X tests."""

from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from syrupy.assertion import SnapshotAssertion

# Importing the integration at collection time makes this repository's
# custom_components package win over the stub in HA's testing_config.
from custom_components.tado_x.client import WARN_ONCE
from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    LEGACY_API_CALLS_TODAY,
    LEGACY_API_RESET_TIME,
    LEGACY_ENABLE_AIR_COMFORT,
    LEGACY_ENABLE_FLOW_TEMP,
    LEGACY_ENABLE_MOBILE_DEVICES,
    LEGACY_ENABLE_RUNNING_TIMES,
    LEGACY_ENABLE_WEATHER,
    LEGACY_HAS_AUTO_ASSIST,
    LEGACY_SCAN_INTERVAL,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.tado_mock import HOME_A, HOME_B, HOME_C, USER_ID, TadoCloudMock, entry_tokens

HOME_TITLES = {HOME_B: "Haus B", HOME_C: "Haus C"}


def _entry_data(home_id: int) -> dict[str, Any]:
    access, refresh = entry_tokens(home_id)
    return {
        CONF_HOME_ID: home_id,
        CONF_HOME_NAME: HOME_TITLES[home_id],
        CONF_USER_ID: USER_ID,
        CONF_ACCESS_TOKEN: access,
        CONF_REFRESH_TOKEN: refresh,
        CONF_TOKEN_EXPIRY: (dt_util.utcnow() + timedelta(minutes=10)).isoformat(),
    }


def _config_entry(home_id: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=HOME_TITLES[home_id],
        unique_id=str(home_id),
        version=1,
        minor_version=2,
        data=_entry_data(home_id),
        options=dict(DEFAULT_OPTIONS),
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in every test."""


@pytest.fixture(autouse=True)
def fresh_warn_once() -> None:
    """Let every test see the first warning of each field again."""
    WARN_ONCE.clear()


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Store snapshots in tests/snapshots like Home Assistant does."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def mock_tado(aioclient_mock: AiohttpClientMocker) -> TadoCloudMock:
    """Return the fake tado cloud."""
    return TadoCloudMock(aioclient_mock)


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a 1.2 entry of home 1000002 with a fresh token; not added to hass."""
    return _config_entry(HOME_B)


@pytest.fixture
def legacy_v1_entry() -> MockConfigEntry:
    """Return a 1.1 entry with every data key v1.8.11 wrote; not added to hass."""
    access, refresh = entry_tokens(HOME_B)
    naive_local_expiry = (dt_util.now() + timedelta(minutes=10)).replace(tzinfo=None)
    return MockConfigEntry(
        domain=DOMAIN,
        title=HOME_TITLES[HOME_B],
        unique_id=None,
        version=1,
        minor_version=1,
        data={
            CONF_HOME_ID: HOME_B,
            CONF_HOME_NAME: HOME_TITLES[HOME_B],
            CONF_ACCESS_TOKEN: access,
            CONF_REFRESH_TOKEN: refresh,
            CONF_TOKEN_EXPIRY: naive_local_expiry.isoformat(),
            LEGACY_API_CALLS_TODAY: 1234,
            LEGACY_API_RESET_TIME: "2026-10-31T12:00:00+00:00",
            LEGACY_HAS_AUTO_ASSIST: True,
            LEGACY_SCAN_INTERVAL: 30,
            LEGACY_ENABLE_WEATHER: True,
            LEGACY_ENABLE_MOBILE_DEVICES: True,
            LEGACY_ENABLE_AIR_COMFORT: True,
            LEGACY_ENABLE_RUNNING_TIMES: True,
            LEGACY_ENABLE_FLOW_TEMP: False,
        },
        options={},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the config_entry fixture against the fake cloud."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


@pytest.fixture
async def two_home_entries(
    hass: HomeAssistant, mock_tado: TadoCloudMock
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Set up entries for homes 1000002 and 1000003 of the same account."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    entries = (_config_entry(HOME_B), _config_entry(HOME_C))
    for entry in entries:
        entry.add_to_hass(hass)
    # Setting up the first entry sets up the integration and with it both entries.
    await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    return entries


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Register every entity enabled, including those disabled by default."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
        new_callable=PropertyMock,
    ):
        yield

"""Tests for the device tracker platform."""

import logging

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_sensor import START, setup_entry, snapshot_entities, state_of, tick

PHONE = "device_tracker.phone"
PHONE_2 = "device_tracker.phone_2"
TABLET = "device_tracker.tablet"


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Start every test at the time the fixtures describe."""
    freezer.move_to(START)


async def test_trackers(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Every phone of home B with the phone feature turned on."""
    await setup_entry(hass, config_entry, mobile_devices=True)

    snapshot_entities(hass, entity_registry, snapshot, config_entry, "device_tracker")


async def test_tracker_states(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home, not_home, and unknown for outdated locations and without geofencing."""
    caplog.set_level(logging.WARNING)
    phones = mock_tado.home(HOME_B).mobile_devices
    await setup_entry(hass, config_entry, mobile_devices=True)

    assert state_of(hass, PHONE) == STATE_HOME
    assert state_of(hass, PHONE_2) == STATE_UNKNOWN
    assert state_of(hass, TABLET) == STATE_UNKNOWN
    tablet = hass.states.get(TABLET)
    assert tablet is not None
    assert tablet.attributes["geofencing_enabled"] is False
    assert tablet.attributes["location_stale"] is None

    phones[1]["location"]["stale"] = False
    phones[0]["location"]["atHome"] = False
    await tick(hass, freezer, 300)

    assert state_of(hass, PHONE_2) == STATE_NOT_HOME
    assert state_of(hass, PHONE) == STATE_NOT_HOME
    assert "location_name" not in caplog.text
    assert "deprecated" not in caplog.text


async def test_custom_presence_zone(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A phone at home is in the zone of the tado home, not necessarily zone.home."""
    assert await async_setup_component(
        hass,
        "zone",
        {
            "zone": {
                "name": "Haus B",
                "latitude": 50.0,
                "longitude": 8.0,
                "radius": 150,
            }
        },
    )

    await setup_entry(
        hass, config_entry, mobile_devices=True, presence_zone="zone.haus_b"
    )

    phone = hass.states.get(PHONE)
    assert phone is not None
    assert phone.state == "Haus B"
    assert phone.attributes["in_zones"] == ["zone.haus_b"]


async def test_phones_come_and_go(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new phone gets a tracker; a phone tado no longer lists loses it."""
    phones = mock_tado.home(HOME_B).mobile_devices
    await setup_entry(hass, config_entry, mobile_devices=True)

    phones.append({**phones[0], "id": 5000004, "name": "Watch"})
    await tick(hass, freezer, 300)

    assert state_of(hass, "device_tracker.watch") == STATE_HOME

    del phones[0]
    await tick(hass, freezer, 300)

    assert hass.states.get(PHONE) is None
    assert state_of(hass, "device_tracker.watch") == STATE_HOME


async def test_no_trackers_without_the_phone_feature(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Phones are neither read nor tracked while the feature is off."""
    await setup_entry(hass, config_entry)

    assert not [
        entity_entry
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, config_entry.entry_id
        )
        if entity_entry.domain == "device_tracker"
    ]
    assert not mock_tado.calls("GET", "/api/v2/homes/1000002/mobileDevices")

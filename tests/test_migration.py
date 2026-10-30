"""Tests of the config entry migration from 1.1 (v1.8.x) to 1.2."""

from datetime import datetime
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DEFAULT_OPTIONS,
    DOMAIN,
    LEGACY_DATA_KEYS,
    LEGACY_ENABLE_AIR_COMFORT,
    LEGACY_ENABLE_FLOW_TEMP,
    LEGACY_ENABLE_MOBILE_DEVICES,
    LEGACY_ENABLE_RUNNING_TIMES,
    LEGACY_ENABLE_WEATHER,
    LEGACY_HAS_AUTO_ASSIST,
    LEGACY_SCAN_INTERVAL,
    OPT_AIR_COMFORT,
    OPT_DAILY_CALL_BUDGET,
    OPT_HOLIDAY_MODE,
    OPT_MANUAL_TERMINATION,
    OPT_MOBILE_DEVICES,
    OPT_RUNNING_TIMES,
    OPT_WEATHER,
)
from custom_components.tado_x.migration import (
    MigrationResult,
    async_migrate_entry,
    migrate_v1_1,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.loader import async_get_integration
from tests.tado_mock import HOME_B, TadoCloudMock

FEATURES = (OPT_WEATHER, OPT_AIR_COMFORT, OPT_RUNNING_TIMES, OPT_MOBILE_DEVICES)
LEGACY_FEATURES = (
    LEGACY_ENABLE_WEATHER,
    LEGACY_ENABLE_AIR_COMFORT,
    LEGACY_ENABLE_RUNNING_TIMES,
    LEGACY_ENABLE_MOBILE_DEVICES,
)


@pytest.fixture(autouse=True)
async def load_flow_handler(hass: HomeAssistant) -> None:
    """Load the config flow platform, as Home Assistant does before a migration."""
    integration = await async_get_integration(hass, DOMAIN)
    await integration.async_get_platform("config_flow")


def _data(**changes: Any) -> dict[str, Any]:
    """Return 1.1 entry data with only home_id plus the given keys."""
    return {CONF_HOME_ID: HOME_B, **changes}


def _issues(issue_registry: ir.IssueRegistry, translation_key: str) -> list[str]:
    return [
        issue.issue_id
        for issue in issue_registry.issues.values()
        if issue.domain == DOMAIN and issue.translation_key == translation_key
    ]


def test_every_v1_key_is_handled(legacy_v1_entry: MockConfigEntry) -> None:
    """Options come from the v1 keys, and every data key stays for a rollback."""
    result = migrate_v1_1(legacy_v1_entry.data, legacy_v1_entry.options)

    assert result == MigrationResult(
        data=dict(legacy_v1_entry.data),
        options={
            **DEFAULT_OPTIONS,
            OPT_WEATHER: True,
            OPT_AIR_COMFORT: True,
            OPT_RUNNING_TIMES: True,
            OPT_MOBILE_DEVICES: True,
            OPT_DAILY_CALL_BUDGET: 4200,
        },
    )
    assert result.data.keys() >= LEGACY_DATA_KEYS
    assert OPT_HOLIDAY_MODE in result.options
    assert LEGACY_ENABLE_FLOW_TEMP not in result.options


@pytest.mark.parametrize(
    ("scan_interval", "features", "budget"),
    [
        # 2,880 room calls + 889 for the other endpoints = 3,769, / 0.9 → 4,188.
        (30, True, 4200),
        # 32 room calls + 889 = 921, / 0.9 → 1,024.
        (2700, True, 1100),
        # 32 + 385 for home state, topology and home info = 417 → 464, raised to 500.
        (2700, False, 500),
        (1, True, 20000),
        (None, True, 3000),
        (0, True, 3000),
        (-30, True, 3000),
        ("30", True, 3000),
        (True, True, 3000),
        # 1,899 + 385 = 2,284 → 2,538.
        (45.5, False, 2600),
    ],
)
def test_scan_interval_becomes_budget(
    scan_interval: object, features: bool, budget: int
) -> None:
    """The budget keeps the room interval of 1.8.x; without one it is the default."""
    data = _data(**dict.fromkeys(LEGACY_FEATURES, features))
    if scan_interval is not None:
        data[LEGACY_SCAN_INTERVAL] = scan_interval

    assert migrate_v1_1(data, {}).options[OPT_DAILY_CALL_BUDGET] == budget


@pytest.mark.parametrize(
    ("auto_assist", "enabled"),
    [(True, True), (False, False), (None, False)],
)
def test_absent_feature_keys_follow_auto_assist(
    auto_assist: bool | None, enabled: bool
) -> None:
    """An enable_* key that v1 never wrote takes the v1 default, has_auto_assist."""
    data = _data() if auto_assist is None else _data(has_auto_assist=auto_assist)

    options = migrate_v1_1(data, {}).options

    assert {key: options[key] for key in FEATURES} == dict.fromkeys(FEATURES, enabled)


def test_feature_keys_win_over_auto_assist() -> None:
    """A stored enable_* key wins over the tier default."""
    data = _data(
        **{
            LEGACY_HAS_AUTO_ASSIST: False,
            LEGACY_ENABLE_WEATHER: True,
            LEGACY_ENABLE_MOBILE_DEVICES: False,
        }
    )

    options = migrate_v1_1(data, {}).options

    assert options[OPT_WEATHER] is True
    assert options[OPT_AIR_COMFORT] is False
    assert options[OPT_MOBILE_DEVICES] is False


def test_existing_options_are_kept() -> None:
    """Options an entry already has are not replaced."""
    options = migrate_v1_1(_data(), {OPT_MANUAL_TERMINATION: "manual"}).options

    assert options[OPT_MANUAL_TERMINATION] == "manual"


def test_home_id_is_coerced_to_int() -> None:
    """A home id stored as text becomes an int."""
    assert migrate_v1_1(_data(home_id="1000002"), {}).data[CONF_HOME_ID] == HOME_B


@pytest.mark.parametrize("home_id", [None, "home", True, 1000002.0])
def test_unusable_home_id_raises(home_id: object) -> None:
    """Without a usable home id there is nothing to migrate to."""
    data = {} if home_id is None else {CONF_HOME_ID: home_id}

    with pytest.raises(ValueError):
        migrate_v1_1(data, {})


async def test_migrate_entry(
    hass: HomeAssistant,
    legacy_v1_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The entry becomes 1.2 with a unique_id, options and every v1 key kept."""
    legacy_v1_entry.add_to_hass(hass)
    data_before = dict(legacy_v1_entry.data)

    assert await legacy_v1_entry.async_migrate(hass)

    assert (legacy_v1_entry.version, legacy_v1_entry.minor_version) == (1, 2)
    assert legacy_v1_entry.unique_id == str(HOME_B)
    assert dict(legacy_v1_entry.data) == data_before
    assert legacy_v1_entry.options == migrate_v1_1(data_before, {}).options
    assert _issues(issue_registry, "breaking_changes_1_9") == ["breaking_changes_1_9"]
    assert _issues(issue_registry, "duplicate_home") == []


async def test_rollback_keeps_flat_tokens(
    hass: HomeAssistant, legacy_v1_entry: MockConfigEntry
) -> None:
    """1.8.11 reads the same flat token keys after a rollback."""
    legacy_v1_entry.add_to_hass(hass)
    tokens_before = {
        key: legacy_v1_entry.data[key]
        for key in (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRY)
    }

    assert await legacy_v1_entry.async_migrate(hass)

    assert {key: legacy_v1_entry.data[key] for key in tokens_before} == tokens_before
    assert (
        datetime.fromisoformat(legacy_v1_entry.data[CONF_TOKEN_EXPIRY]).tzinfo is None
    )


async def test_duplicate_home_keeps_no_unique_id(
    hass: HomeAssistant,
    legacy_v1_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A second v1 entry of the same home gets no unique_id and a repair issue."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Haus B (2)",
        version=1,
        minor_version=1,
        data=dict(legacy_v1_entry.data),
    )
    legacy_v1_entry.add_to_hass(hass)
    second.add_to_hass(hass)

    assert await legacy_v1_entry.async_migrate(hass)
    assert await second.async_migrate(hass)

    assert legacy_v1_entry.unique_id == str(HOME_B)
    assert second.unique_id is None
    assert second.minor_version == 2
    assert len(_issues(issue_registry, "duplicate_home")) == 1


async def test_entry_that_has_its_unique_id_keeps_it(
    hass: HomeAssistant, legacy_v1_entry: MockConfigEntry
) -> None:
    """An entry that already owns the unique_id is not its own duplicate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(HOME_B),
        version=1,
        minor_version=1,
        data=dict(legacy_v1_entry.data),
    )
    entry.add_to_hass(hass)

    assert await entry.async_migrate(hass)

    assert entry.unique_id == str(HOME_B)


async def test_legacy_climate_unique_id_is_migrated(
    hass: HomeAssistant,
    legacy_v1_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A climate "{home}_{room}" becomes "{home}_{room}_climate" unless taken."""
    legacy_v1_entry.add_to_hass(hass)

    def register(domain: str, unique_id: str) -> str:
        return entity_registry.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=legacy_v1_entry
        ).entity_id

    legacy = register(Platform.CLIMATE, f"{HOME_B}_1")
    blocked = register(Platform.CLIMATE, f"{HOME_B}_2")
    current = register(Platform.CLIMATE, f"{HOME_B}_2_climate")
    sensor = register(Platform.SENSOR, f"{HOME_B}_3")
    other_home = register(Platform.CLIMATE, "1000003_4")

    assert await legacy_v1_entry.async_migrate(hass)

    def unique_id(entity_id: str) -> str:
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        return entry.unique_id

    assert unique_id(legacy) == f"{HOME_B}_1_climate"
    assert unique_id(blocked) == f"{HOME_B}_2"
    assert unique_id(current) == f"{HOME_B}_2_climate"
    assert unique_id(sensor) == f"{HOME_B}_3"
    assert unique_id(other_home) == "1000003_4"


async def test_migration_is_idempotent_after_rollback(
    hass: HomeAssistant, legacy_v1_entry: MockConfigEntry
) -> None:
    """After a rollback and a re-upgrade the entry is 1.2 already; nothing runs."""
    legacy_v1_entry.add_to_hass(hass)
    assert await legacy_v1_entry.async_migrate(hass)
    options = dict(legacy_v1_entry.options)
    # 1.8.11 writes its keys into data and leaves the minor version alone.
    hass.config_entries.async_update_entry(
        legacy_v1_entry,
        data={**legacy_v1_entry.data, LEGACY_SCAN_INTERVAL: 2700},
    )

    assert await legacy_v1_entry.async_migrate(hass)
    assert await async_migrate_entry(hass, legacy_v1_entry)

    assert legacy_v1_entry.minor_version == 2
    assert legacy_v1_entry.options == options


async def test_future_minor_version_is_accepted(hass: HomeAssistant) -> None:
    """A newer minor version of the same major version loads unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=3, data=_data(), options={}
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert (entry.minor_version, entry.options) == (3, {})


async def test_future_major_version_is_refused(hass: HomeAssistant) -> None:
    """A downgrade from a future major version fails instead of guessing."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1, data=_data())
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)
    assert not await entry.async_migrate(hass)
    assert entry.version == 2


async def test_entry_without_home_id_is_refused(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An entry without a home id cannot be migrated and is left as it is."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data={})
    entry.add_to_hass(hass)

    assert not await entry.async_migrate(hass)

    assert entry.minor_version == 1
    assert "without a home id" in caplog.text


async def test_naive_expiry_costs_one_lazy_refresh(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    legacy_v1_entry: MockConfigEntry,
) -> None:
    """The naive local token_expiry of v1 counts as expired: one refresh, then aware."""
    legacy_v1_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(legacy_v1_entry.entry_id)
    await hass.async_block_till_done()

    assert legacy_v1_entry.state is ConfigEntryState.LOADED
    assert len(mock_tado.calls("POST", "/oauth2/token")) == 1
    expiry = datetime.fromisoformat(legacy_v1_entry.data[CONF_TOKEN_EXPIRY])
    assert expiry.tzinfo is not None
    assert legacy_v1_entry.data.keys() >= LEGACY_DATA_KEYS

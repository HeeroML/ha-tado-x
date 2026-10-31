"""Config entry migration from 1.1 (v1.8.x) to 1.2.

The migration is additive: legacy keys stay in the entry data, so a rollback
to 1.8.11 still works. Home Assistant migrates an entry only once, so the
parts later changes can undo are checked again at every setup.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
import math
import re
from typing import Any, Final

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_HOME_ID,
    DEFAULT_DAILY_CALL_BUDGET,
    DEFAULT_OPTIONS,
    DOMAIN,
    LEGACY_ENABLE_AIR_COMFORT,
    LEGACY_ENABLE_MOBILE_DEVICES,
    LEGACY_ENABLE_RUNNING_TIMES,
    LEGACY_ENABLE_WEATHER,
    LEGACY_HAS_AUTO_ASSIST,
    LEGACY_SCAN_INTERVAL,
    LOGGER,
    MAX_DAILY_CALL_BUDGET,
    OPT_AIR_COMFORT,
    OPT_DAILY_CALL_BUDGET,
    OPT_MOBILE_DEVICES,
    OPT_RUNNING_TIMES,
    OPT_WEATHER,
)
from .issues import (
    async_create_breaking_changes_issue,
    async_create_duplicate_home_issue,
    async_delete_duplicate_home_issue,
)
from .runtime import TadoXConfigEntry, entry_label

_DAY_SECONDS: Final = 86400
_MIN_MIGRATED_BUDGET: Final = 500

# 1.9.0 base periods in seconds of the endpoints polled besides rooms.
_REQUIRED_PERIODS: Final = (
    300,  # home_state
    900,  # topology
    86400,  # home_info
)
_FEATURE_PERIODS: Final[Mapping[str, int]] = {
    OPT_WEATHER: 900,
    OPT_AIR_COMFORT: 900,
    OPT_RUNNING_TIMES: 3600,
    OPT_MOBILE_DEVICES: 300,
}
_LEGACY_FEATURES: Final[Mapping[str, str]] = {
    LEGACY_ENABLE_WEATHER: OPT_WEATHER,
    LEGACY_ENABLE_AIR_COMFORT: OPT_AIR_COMFORT,
    LEGACY_ENABLE_RUNNING_TIMES: OPT_RUNNING_TIMES,
    LEGACY_ENABLE_MOBILE_DEVICES: OPT_MOBILE_DEVICES,
}


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """New data and options of a migrated entry."""

    data: dict[str, Any]
    options: dict[str, Any]


def _home_id(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdecimal():
        return int(raw)
    raise ValueError("the entry has no tado home id")


def _daily_call_budget(scan_interval: object, features: Mapping[str, bool]) -> int:
    """Return the daily call budget that keeps the room interval of 1.8.x.

    The planner polls with 90 % of the budget, so the budget is the calls the
    old interval and the enabled features need, divided by 0.9 and rounded up
    to 100, within 500 and 20,000. Without a usable interval it is the default.
    """
    if (
        isinstance(scan_interval, bool)
        or not isinstance(scan_interval, int | float)
        or scan_interval <= 0
    ):
        return DEFAULT_DAILY_CALL_BUDGET
    periods = [
        *_REQUIRED_PERIODS,
        *(period for key, period in _FEATURE_PERIODS.items() if features[key]),
    ]
    calls = math.ceil(_DAY_SECONDS / scan_interval) + sum(
        math.ceil(_DAY_SECONDS / period) for period in periods
    )
    budget = math.ceil(Fraction(calls, 90)) * 100
    return min(max(budget, _MIN_MIGRATED_BUDGET), MAX_DAILY_CALL_BUDGET)


def migrate_v1_1(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> MigrationResult:
    """Derive the 1.2 options from 1.1 entry data. Pure function.

    Every data key is kept; only home_id is coerced to int. A feature without
    its enable_* key takes the 1.8.x default, has_auto_assist. Options the
    entry already has win over derived ones.

    Raises:
        ValueError: The data has no usable home id.
    """
    auto_assist = bool(data.get(LEGACY_HAS_AUTO_ASSIST, False))
    features = {
        option: bool(data.get(legacy, auto_assist))
        for legacy, option in _LEGACY_FEATURES.items()
    }
    derived = {
        **DEFAULT_OPTIONS,
        **features,
        OPT_DAILY_CALL_BUDGET: _daily_call_budget(
            data.get(LEGACY_SCAN_INTERVAL), features
        ),
    }
    return MigrationResult(
        data={**data, CONF_HOME_ID: _home_id(data.get(CONF_HOME_ID))},
        options={**derived, **options},
    )


@callback
def _async_unique_id(
    hass: HomeAssistant, entry: TadoXConfigEntry, unique_id: str
) -> str | None:
    """Return the home id as unique_id, unless another entry already has it.

    1.8.x never set a unique_id, so one home may have two entries; the second
    keeps none and gets a repair issue.
    """
    owner = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if owner is None or owner.entry_id == entry.entry_id:
        return unique_id
    async_create_duplicate_home_issue(hass, entry)
    return entry.unique_id


@callback
def async_finish_migration(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Redo the parts of the migration that later changes undid.

    Saving the options dialog of 1.8.11 after a rollback empties the options,
    and a duplicate entry may own its home once the user removed the other
    entry. The duplicate issue is not persistent, so it is reported again.
    """
    if OPT_DAILY_CALL_BUDGET not in entry.options:
        options = migrate_v1_1(entry.data, entry.options).options
        hass.config_entries.async_update_entry(entry, options=options)
        LOGGER.debug("Derived the emptied options of %s again", entry_label(entry))
    if entry.unique_id is None:
        unique_id = _async_unique_id(hass, entry, str(entry.data[CONF_HOME_ID]))
        if unique_id is not None:
            hass.config_entries.async_update_entry(entry, unique_id=unique_id)
            async_delete_duplicate_home_issue(hass, entry)


async def _async_migrate_climate_unique_ids(
    hass: HomeAssistant, entry: TadoXConfigEntry, home_id: int
) -> None:
    """Rename the climate unique_id "{home}_{room}" of old versions."""
    registry = er.async_get(hass)
    legacy = re.compile(rf"{home_id}_\d+")

    @callback
    def migrate(entity: er.RegistryEntry) -> dict[str, Any] | None:
        if entity.domain != Platform.CLIMATE or not legacy.fullmatch(entity.unique_id):
            return None
        unique_id = f"{entity.unique_id}_climate"
        if registry.async_get_entity_id(Platform.CLIMATE, DOMAIN, unique_id):
            return None
        return {"new_unique_id": unique_id}

    await er.async_migrate_entries(hass, entry.entry_id, migrate)


async def async_migrate_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Migrate a config entry; refuse a future major version."""
    if entry.version > 1:
        return False
    if entry.minor_version >= 2:
        return True
    try:
        result = migrate_v1_1(entry.data, entry.options)
    except ValueError:
        LOGGER.error(
            "Cannot migrate the tado X entry %s without a home id", entry.entry_id
        )
        return False
    home_id: int = result.data[CONF_HOME_ID]
    unique_id = _async_unique_id(hass, entry, str(home_id))
    await _async_migrate_climate_unique_ids(hass, entry, home_id)
    async_create_breaking_changes_issue(hass)
    hass.config_entries.async_update_entry(
        entry,
        data=result.data,
        options=result.options,
        unique_id=unique_id,
        minor_version=2,
    )
    LOGGER.debug(
        "Migrated the tado X entry %s to 1.2 with a budget of %s calls a day",
        entry_label(entry),
        result.options[OPT_DAILY_CALL_BUDGET],
    )
    return True

"""Config entry migration from 1.1 (v1.8.x) to 1.2.

The migration is additive: legacy keys stay in the entry data, so a rollback
to 1.8.11 still works.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .runtime import TadoXConfigEntry


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """New data and options of a migrated entry."""

    data: dict[str, Any]
    options: dict[str, Any]


def migrate_v1_1(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> MigrationResult:
    """Derive the 1.2 options from 1.1 entry data. Pure function."""
    raise NotImplementedError


async def async_migrate_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Migrate a config entry; refuse a future major version."""
    raise NotImplementedError

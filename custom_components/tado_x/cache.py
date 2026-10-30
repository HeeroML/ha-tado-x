"""Persistent cache of raw API payloads and quota state (#62)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .runtime import FeatureStatus


@dataclass(frozen=True, slots=True)
class CacheSeed:
    """What a restart can start from: raw payloads by key, quota and call counter."""

    payloads: Mapping[str, tuple[datetime, Any]]
    quota: Mapping[str, Any]
    calls: Sequence[int]
    flow_status: FeatureStatus | None


class SnapshotCache:
    """Raw payloads of one entry in a private Store, saved at most every 600 s.

    Phone data is never stored.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, home_id: int) -> None:
        """Initialize the Store of one entry."""
        raise NotImplementedError

    async def async_load(self) -> CacheSeed | None:
        """Return the seed, or None when missing, of another schema or another home."""
        raise NotImplementedError

    @callback
    def record(self, home_id: int, key: str, raw: Any, at: datetime) -> None:
        """Remember the raw payload of a cached read; phone data is ignored."""
        raise NotImplementedError

    @callback
    def set_quota(self, data: Mapping[str, Any], calls: Sequence[int]) -> None:
        """Remember the quota state and the call counter."""
        raise NotImplementedError

    @callback
    def set_flow_status(self, status: FeatureStatus) -> None:
        """Remember whether the home has flow temperature control."""
        raise NotImplementedError

    async def async_flush(self) -> None:
        """Write pending changes now."""
        raise NotImplementedError

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        """Delete the Store of a removed entry."""
        raise NotImplementedError

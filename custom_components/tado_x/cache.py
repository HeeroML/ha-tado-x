"""Persistent cache of raw API payloads and quota state (#62)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .runtime import FeatureStatus

SCHEMA: Final = 1
SAVE_DELAY: Final = 600
NEVER_CACHED: Final = frozenset({"mobile_devices"})

_STORE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class CacheSeed:
    """What a restart can start from: raw payloads by key, quota and call counter."""

    payloads: Mapping[str, tuple[datetime, Any]]
    quota: Mapping[str, Any]
    calls: Sequence[int]
    flow_status: FeatureStatus | None


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(
        hass, _STORE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True
    )


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _payloads(raw: object) -> dict[str, tuple[datetime, Any]]:
    """Return the readable payloads of a stored payload map."""
    if not isinstance(raw, Mapping):
        return {}
    payloads: dict[str, tuple[datetime, Any]] = {}
    for key, item in raw.items():
        if key in NEVER_CACHED or not isinstance(item, Mapping):
            continue
        if (at := _parse_time(item.get("at"))) is not None and "raw" in item:
            payloads[key] = (at, item["raw"])
    return payloads


def _calls(raw: object) -> list[int]:
    if not isinstance(raw, list) or not all(isinstance(item, int) for item in raw):
        return []
    return list(raw)


def _flow_status(raw: object) -> FeatureStatus | None:
    try:
        return FeatureStatus(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


class SnapshotCache:
    """Raw payloads of one entry in a private Store, saved at most every 600 s.

    Phone data is never stored.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, home_id: int) -> None:
        """Initialize the Store of one entry."""
        self._store = _store(hass, entry_id)
        self._home_id = home_id
        self._payloads: dict[str, tuple[datetime, Any]] = {}
        self._quota: Mapping[str, Any] = {}
        self._calls: Sequence[int] = []
        self._flow_status: FeatureStatus | None = None
        self._save_scheduled = False
        self._dirty = False
        self._closed = False

    async def async_load(self) -> CacheSeed | None:
        """Return the seed, or None when missing, of another schema or another home."""
        stored = await self._store.async_load()
        if (
            not isinstance(stored, Mapping)
            or stored.get("schema") != SCHEMA
            or stored.get("home_id") != self._home_id
        ):
            return None
        self._payloads = _payloads(stored.get("payloads"))
        quota = stored.get("quota")
        self._quota = dict(quota) if isinstance(quota, Mapping) else {}
        self._calls = _calls(stored.get("calls"))
        self._flow_status = _flow_status(stored.get("flow_status"))
        return CacheSeed(
            payloads=dict(self._payloads),
            quota=self._quota,
            calls=self._calls,
            flow_status=self._flow_status,
        )

    @callback
    def record(self, home_id: int, key: str, raw: Any, at: datetime) -> None:
        """Remember the raw payload of a cached read; phone data is ignored."""
        if self._closed or home_id != self._home_id or key in NEVER_CACHED:
            return
        self._payloads[key] = (at, raw)
        self._async_schedule_save()

    @callback
    def set_quota(self, data: Mapping[str, Any], calls: Sequence[int]) -> None:
        """Remember the quota state and the call counter."""
        if self._closed:
            return
        self._quota = data
        self._calls = calls
        self._async_schedule_save()

    @callback
    def set_flow_status(self, status: FeatureStatus) -> None:
        """Remember whether the home has flow temperature control."""
        if self._closed or status is self._flow_status:
            return
        self._flow_status = status
        self._async_schedule_save()

    def payload(self, key: str) -> tuple[datetime, Any] | None:
        """Return the newest raw payload of a key and when it was read."""
        return self._payloads.get(key)

    def metadata(self) -> dict[str, Any]:
        """Return what the cache holds, without the payloads."""
        return {
            "schema": SCHEMA,
            "slices": {key: at.isoformat() for key, (at, _) in self._payloads.items()},
            "flow_status": self._flow_status,
            "unsaved_changes": self._dirty,
        }

    async def async_flush(self) -> None:
        """Write pending changes now."""
        if self._dirty:
            await self._store.async_save(self._data())

    async def async_close(self) -> None:
        """Write pending changes and ignore later ones; the entry is unloading.

        A request still running during the unload must not schedule a write
        that would outlive the entry, or recreate the Store of a removed one.
        """
        self._closed = True
        await self.async_flush()

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        """Delete the Store of a removed entry."""
        await _store(hass, entry_id).async_remove()

    @callback
    def _async_schedule_save(self) -> None:
        # Store.async_delay_save postpones a pending write on every call, so
        # it is called once per save to keep the 600 s maximum delay.
        self._dirty = True
        if not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._data, SAVE_DELAY)

    @callback
    def _data(self) -> dict[str, Any]:
        """Return the Store content; called when the Store writes."""
        self._save_scheduled = False
        self._dirty = False
        return {
            "schema": SCHEMA,
            "home_id": self._home_id,
            "payloads": {
                key: {"at": at.astimezone(UTC).isoformat(), "raw": raw}
                for key, (at, raw) in self._payloads.items()
            },
            "quota": dict(self._quota),
            "calls": list(self._calls),
            "flow_status": self._flow_status,
        }

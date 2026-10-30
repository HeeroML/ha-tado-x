"""Diagnostics of the tado X integration."""

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Final

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.util import dt as dt_util

from .client import Device, RoomTopology
from .const import CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRY, DOMAIN
from .coordinator import TadoXCoordinator
from .runtime import TadoXConfigEntry
from .token_store import parse_expiry

TO_REDACT: Final = {
    "access_token",
    "refresh_token",
    "token_expiry",
    "user_id",
    "email",
    "username",
    "address",
    "geolocation",
    "latitude",
    "longitude",
    "contactDetails",
    "deviceMetadata",
    "device_code",
    "user_code",
}
SERIAL_KEYS: Final = frozenset({"serialNumber", "serial"})
PAYLOADS: Final = ("rooms", "topology", "home_state", "weather", "air_comfort")


def mask_serial(serial: str) -> str:
    """Keep the type prefix and the last four characters of a serial number."""
    return f"{serial[:2]}…{serial[-4:]}"


def _plain(value: Any) -> Any:
    """Return a JSON-friendly copy with serials masked."""
    if isinstance(value, Mapping):
        return {
            str(key): mask_serial(item)
            if key in SERIAL_KEYS and isinstance(item, str)
            else _plain(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    return value


def _coordinator(coordinator: TadoXCoordinator[Any]) -> dict[str, Any]:
    exception = coordinator.last_exception
    return {
        "last_update_success": coordinator.last_update_success,
        "last_exception": None if exception is None else type(exception).__name__,
        "update_interval": coordinator.update_interval,
        "last_fetched": coordinator.last_fetched,
        "last_live_update": coordinator.last_live_update,
        "stale_since": coordinator.stale_since,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TadoXConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics of a config entry; phone data is never included."""
    data = entry.runtime_data
    monitor = data.monitor
    coordinators: dict[str, TadoXCoordinator[Any]] = {
        "rooms": data.rooms,
        "home": data.home,
        "devices": data.devices,
    }
    info = monitor.info
    plan = monitor.plan
    payloads = {key: data.cache.payload(key) for key in PAYLOADS}
    diagnostics: dict[str, Any] = _plain(
        {
            "entry": {
                "title": entry.title,
                "version": f"{entry.version}.{entry.minor_version}",
                "data": async_redact_data(dict(entry.data), TO_REDACT),
                "options": dict(entry.options),
            },
            "tokens": {
                "expires_at": parse_expiry(entry.data.get(CONF_TOKEN_EXPIRY)),
                "has_refresh_token": bool(entry.data.get(CONF_REFRESH_TOKEN)),
                "auth_failed": any(
                    isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
                    for coordinator in coordinators.values()
                ),
            },
            "quota": {
                "info": None if info is None else asdict(info),
                "reset_at": None if info is None else info.reset_at,
                "blocked_until": monitor.blocked_until,
                "calls_24h": monitor.calls_24h(dt_util.utcnow()),
                "status": monitor.status,
                "mode": monitor.mode,
                "factor": monitor.factor,
                "plan": {
                    "budget": plan.budget,
                    "calls_per_day": plan.calls_per_day,
                    "periods": plan.periods,
                },
            },
            "coordinators": {
                kind: _coordinator(coordinator)
                for kind, coordinator in coordinators.items()
            },
            "flow_status": data.devices.data.flow_status,
            "cache": data.cache.metadata(),
            "payloads": {
                key: None
                if item is None
                else async_redact_data({"at": item[0], "raw": item[1]}, TO_REDACT)
                for key, item in payloads.items()
            },
        }
    )
    return diagnostics


def _topology_entry(
    entry: TadoXConfigEntry, device: DeviceEntry
) -> RoomTopology | Device | None:
    home_id = entry.runtime_data.api.home_id
    topology = entry.runtime_data.devices.data.topology
    for value in (value for domain, value in device.identifiers if domain == DOMAIN):
        if value in topology.devices:
            return topology.devices[value]
        room_id = value.removeprefix(f"{home_id}_")
        if room_id.isdecimal() and int(room_id) in topology.rooms:
            return topology.rooms[int(room_id)]
    return None


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: TadoXConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return the entry diagnostics plus the topology entry of one device."""
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    found = _topology_entry(entry, device)
    diagnostics["device"] = None if found is None else _plain(asdict(found))
    return diagnostics

"""Diagnostics of the tado X integration."""

from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .runtime import TadoXConfigEntry

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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TadoXConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics of a config entry; phone data is never included."""
    raise NotImplementedError


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: TadoXConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return the entry diagnostics plus the topology entry of one device."""
    raise NotImplementedError

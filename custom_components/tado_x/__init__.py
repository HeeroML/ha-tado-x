"""The tado X integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .migration import async_migrate_entry
from .runtime import TadoXConfigEntry

__all__ = ["async_migrate_entry"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once."""
    raise NotImplementedError


async def async_setup_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Set up a tado X home from a config entry."""
    raise NotImplementedError


async def async_unload_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Unload a config entry and flush its cache."""
    raise NotImplementedError


async def async_remove_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Delete the cache of a removed config entry."""
    raise NotImplementedError


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TadoXConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing a device only when tado no longer reports it."""
    raise NotImplementedError

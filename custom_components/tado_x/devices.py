"""Device registry of the tado X integration.

Devices are registered centrally before the platforms load, in the order
home, bridge, rooms, physical devices, phones; entities carry identifiers
only.
"""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntry

from .runtime import TadoXConfigEntry


def home_identifier(home_id: int) -> tuple[str, str]:
    """Return the identifier of the home device."""
    raise NotImplementedError


def room_identifier(home_id: int, room_id: int) -> tuple[str, str]:
    """Return the identifier of a room device."""
    raise NotImplementedError


def device_identifier(serial: str) -> tuple[str, str]:
    """Return the identifier of a physical device."""
    raise NotImplementedError


def mobile_identifier(mobile_id: int) -> tuple[str, str]:
    """Return the identifier of a phone."""
    raise NotImplementedError


@callback
def async_setup_device_registry(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Register every device now and keep the registry in sync with the data."""
    raise NotImplementedError


@callback
def async_is_current_device(entry: TadoXConfigEntry, device: DeviceEntry) -> bool:
    """Return True if the device is still reported by tado."""
    raise NotImplementedError

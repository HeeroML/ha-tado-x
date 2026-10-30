"""Services of the tado X integration, registered once per Home Assistant start.

Room services are platform entity services on climate; home services resolve
their config entry per call, so they always reach the entry's current client.
"""

from homeassistant.core import HomeAssistant, callback


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register every tado X service."""
    raise NotImplementedError

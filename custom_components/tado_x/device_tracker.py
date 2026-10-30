"""Device tracker platform: phones with geofencing."""

from collections.abc import Iterator
from typing import Any, override

from homeassistant.components.device_tracker.entity import (
    TrackerEntity,
    TrackerEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import MobileDevice
from .const import DEFAULT_PRESENCE_ZONE, OPT_PRESENCE_ZONE
from .coordinator import HomeCoordinator
from .devices import mobile_identifier
from .entity import TadoXEntity, async_add_dynamic_entities
from .runtime import TadoXConfigEntry

PARALLEL_UPDATES = 0

PHONE = TrackerEntityDescription(key="phone", translation_key="phone", name=None)


class TadoXPhoneTracker(TadoXEntity[HomeCoordinator], TrackerEntity):
    """Phone in the presence zone or away, as tado's geofencing sees it.

    The state is unknown while geofencing is off for the phone or its
    location is unknown or outdated.
    """

    def __init__(self, coordinator: HomeCoordinator, mobile_id: int, zone: str) -> None:
        """Initialize the tracker of a phone device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            PHONE,
            unique_id=f"{home_id}_mobile_{mobile_id}",
            device=mobile_identifier(mobile_id),
        )
        self._mobile_id = mobile_id
        self._zone = zone
        self._attr_in_zones = self._zones()

    def _phone(self) -> MobileDevice | None:
        return (self.coordinator.data.mobile_devices or {}).get(self._mobile_id)

    @override
    def _present(self) -> bool:
        return self._phone() is not None

    def _zones(self) -> list[str] | None:
        phone = self._phone()
        if (
            phone is None
            or not phone.geo_tracking
            or phone.stale
            or phone.at_home is None
        ):
            return None
        return [self._zone] if phone.at_home else []

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update the zones, then write the state."""
        self._attr_in_zones = self._zones()
        super()._handle_coordinator_update()

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether geofencing is on and whether the location is outdated."""
        phone = (self.coordinator.data.mobile_devices or {})[self._mobile_id]
        return {"geofencing_enabled": phone.geo_tracking, "location_stale": phone.stale}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the phone trackers of a tado X home."""
    data = entry.runtime_data
    if not data.monitor.enabled("mobile_devices"):
        return
    zone: str = entry.options.get(OPT_PRESENCE_ZONE, DEFAULT_PRESENCE_ZONE)

    def build() -> Iterator[TadoXEntity[Any]]:
        for mobile_id in data.home.data.mobile_devices or {}:
            yield TadoXPhoneTracker(data.home, mobile_id, zone)

    async_add_dynamic_entities(entry, data.home, async_add_entities, build)

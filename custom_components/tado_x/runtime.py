"""Runtime data of a config entry and the coordinator snapshots.

Snapshots are immutable and carry no fetch timestamps, so equal data compares
equal and an unchanged poll writes no states.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

from .client import (
    AirComfort,
    Device,
    FlowTemperature,
    HolidayMode,
    HomeInfo,
    HomeState,
    MobileDevice,
    Presence,
    PresenceSetting,
    RoomState,
    RunningTimes,
    TadoXClient,
    TadoXHome,
    Telemetry,
    Termination,
    Topology,
    Weather,
)

if TYPE_CHECKING:
    from .cache import SnapshotCache
    from .coordinator import DevicesCoordinator, HomeCoordinator, RoomsCoordinator
    from .quota import QuotaMonitor


class FeatureStatus(StrEnum):
    """Whether an optional feature of the home exists."""

    UNKNOWN = "unknown"
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomsSnapshot:
    """Data of the rooms coordinator."""

    rooms: Mapping[int, RoomState]

    def with_room(self, room_id: int, **changes: Any) -> RoomsSnapshot:
        """Return a copy with fields of one room replaced."""
        if (room := self.rooms.get(room_id)) is None:
            return self
        return replace(self, rooms={**self.rooms, room_id: replace(room, **changes)})


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeSnapshot:
    """Data of the home coordinator.

    mobile_devices_complete is False when the last read of the phones
    skipped a malformed one.
    """

    state: HomeState
    weather: Weather | None = None
    air_comfort: AirComfort | None = None
    mobile_devices: Mapping[int, MobileDevice] | None = None
    mobile_devices_complete: bool = True

    def with_presence(self, setting: PresenceSetting) -> HomeSnapshot:
        """Return a copy with the presence setting applied."""
        if setting is PresenceSetting.AUTO:
            state = replace(self.state, presence_locked=False)
        else:
            state = replace(
                self.state, presence=Presence(setting.value), presence_locked=True
            )
        return replace(self, state=state)


def _replace_device(
    devices: tuple[Device, ...], serial: str, changes: Mapping[str, Any]
) -> tuple[Device, ...] | None:
    for index, device in enumerate(devices):
        if device.serial == serial:
            return (
                *devices[:index],
                replace(device, **changes),
                *devices[index + 1 :],
            )
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class DevicesSnapshot:
    """Data of the devices coordinator."""

    topology: Topology
    home_info: HomeInfo | None = None
    running_times: RunningTimes | None = None
    flow_temperature: FlowTemperature | None = None
    flow_status: FeatureStatus = FeatureStatus.UNKNOWN
    holiday_mode: HolidayMode | None = None

    def with_device(self, serial: str, **changes: Any) -> DevicesSnapshot:
        """Return a copy with fields of one device replaced."""
        topology = self.topology
        for room_id, room in topology.rooms.items():
            if (devices := _replace_device(room.devices, serial, changes)) is not None:
                rooms = {**topology.rooms, room_id: replace(room, devices=devices)}
                return replace(self, topology=replace(topology, rooms=rooms))
        if (
            others := _replace_device(topology.other_devices, serial, changes)
        ) is not None:
            return replace(self, topology=replace(topology, other_devices=others))
        return self

    def with_room_default(
        self, room_id: int, termination: Termination
    ) -> DevicesSnapshot:
        """Return a copy with the app's termination default of a room replaced."""
        topology = self.topology
        if (room := topology.rooms.get(room_id)) is None:
            return self
        rooms = {
            **topology.rooms,
            room_id: replace(room, manual_termination_default=termination),
        }
        return replace(self, topology=replace(topology, rooms=rooms))

    def with_flow_temperature(self, **changes: Any) -> DevicesSnapshot:
        """Return a copy with flow temperature fields replaced."""
        if self.flow_temperature is None:
            return self
        return replace(self, flow_temperature=replace(self.flow_temperature, **changes))


@dataclass(slots=True, kw_only=True)
class TadoXRuntimeData:
    """Everything a loaded config entry owns.

    telemetry is the one Telemetry of the entry, shared by the client, its
    token manager and its quota tracker.
    """

    client: TadoXClient
    telemetry: Telemetry
    api: TadoXHome
    rooms: RoomsCoordinator
    home: HomeCoordinator
    devices: DevicesCoordinator
    monitor: QuotaMonitor
    cache: SnapshotCache


type TadoXConfigEntry = ConfigEntry[TadoXRuntimeData]

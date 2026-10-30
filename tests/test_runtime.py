"""Tests for the coordinator snapshots and their optimistic patch helpers."""

from datetime import timedelta

from custom_components.tado_x.client import (
    FlowTemperature,
    HomeState,
    Power,
    Presence,
    PresenceSetting,
    RoomState,
    Setting,
    Termination,
    Topology,
)
from custom_components.tado_x.runtime import (
    DevicesSnapshot,
    FeatureStatus,
    HomeSnapshot,
    RoomsSnapshot,
)
from tests.tado_mock import load_fixture


def _rooms() -> RoomsSnapshot:
    return RoomsSnapshot(rooms=RoomState.list_from_api(load_fixture("rooms.json")))


def _devices(
    *,
    flow_temperature: FlowTemperature | None = None,
    flow_status: FeatureStatus = FeatureStatus.UNKNOWN,
) -> DevicesSnapshot:
    return DevicesSnapshot(
        topology=Topology.from_api(load_fixture("rooms_and_devices.json")),
        flow_temperature=flow_temperature,
        flow_status=flow_status,
    )


def test_snapshots_compare_by_value() -> None:
    """Equal payloads give equal snapshots, so unchanged polls write no states."""
    assert _rooms() == _rooms()
    assert _devices() == _devices()
    assert _devices().flow_status is FeatureStatus.UNKNOWN


def test_with_room() -> None:
    """A room patch replaces fields of one room and leaves the original alone."""
    rooms = _rooms()
    off = Setting(power=Power.OFF, temperature=None)
    patched = rooms.with_room(1, setting=off)
    assert patched.rooms[1].setting == off
    assert patched.rooms[2] is rooms.rooms[2]
    assert rooms.rooms[1].setting.power is Power.ON
    assert rooms.with_room(99, setting=off) is rooms


def test_with_presence() -> None:
    """HOME and AWAY lock the presence, AUTO unlocks it."""
    home = HomeSnapshot(state=HomeState(presence=Presence.HOME, presence_locked=False))
    away = home.with_presence(PresenceSetting.AWAY)
    assert away.state == HomeState(presence=Presence.AWAY, presence_locked=True)
    assert away.state.setting is PresenceSetting.AWAY
    auto = away.with_presence(PresenceSetting.AUTO)
    assert auto.state == HomeState(presence=Presence.AWAY, presence_locked=False)
    assert auto.state.setting is PresenceSetting.AUTO
    assert home.with_presence(PresenceSetting.HOME).state.setting is (
        PresenceSetting.HOME
    )


def test_with_device() -> None:
    """Device patches reach room devices and other devices."""
    devices = _devices()
    patched = devices.with_device("VA0000000101", temperature_offset=0.5)
    assert patched.topology.devices["VA0000000101"].temperature_offset == 0.5
    assert devices.topology.devices["VA0000000101"].temperature_offset == -1.5
    assert patched.topology.rooms[1].devices[1] is devices.topology.rooms[1].devices[1]

    bridge = devices.with_device("IB0000000001", firmware="300.1")
    assert bridge.topology.devices["IB0000000001"].firmware == "300.1"
    assert devices.with_device("XX0000000000", child_lock=True) is devices


def test_with_room_default() -> None:
    """The app's termination default of a room is replaced."""
    devices = _devices()
    timer = Termination.timer(timedelta(minutes=20))
    patched = devices.with_room_default(1, timer)
    assert patched.topology.rooms[1].manual_termination_default == timer
    assert patched.topology.rooms[2] is devices.topology.rooms[2]
    assert devices.with_room_default(99, timer) is devices


def test_with_flow_temperature() -> None:
    """Flow temperature patches apply only when the feature exists."""
    absent = _devices(flow_status=FeatureStatus.ABSENT)
    assert absent.with_flow_temperature(max_flow_temperature=45) is absent

    flow = FlowTemperature(
        max_flow_temperature=50, minimum=30, maximum=80, auto_adaptation=False
    )
    present = _devices(flow_temperature=flow, flow_status=FeatureStatus.PRESENT)
    patched = present.with_flow_temperature(max_flow_temperature=45)
    assert patched.flow_temperature is not None
    assert patched.flow_temperature.max_flow_temperature == 45
    assert present.flow_temperature is flow

"""The tado X integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .cache import SnapshotCache
from .client import QuotaTracker, TadoXClient, TadoXError, TokenManager
from .const import CONF_HOME_ID, CONF_USER_ID, DOMAIN, LOGGER
from .coordinator import DevicesCoordinator, HomeCoordinator, RoomsCoordinator
from .devices import async_is_current_device, async_setup_device_registry
from .issues import async_delete_entry_issues
from .migration import async_migrate_entry
from .quota import QuotaMonitor
from .runtime import TadoXConfigEntry, TadoXRuntimeData
from .services import async_setup_services
from .token_store import ConfigEntryTokenStore

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
    async_setup_services(hass)
    return True


async def _async_store_user_id(
    hass: HomeAssistant, entry: TadoXConfigEntry, client: TadoXClient
) -> None:
    """Remember the account of an entry migrated from 1.8, once and best effort."""
    try:
        account = await client.async_get_account()
    except TadoXError as err:
        LOGGER.debug("Reading the tado account failed: %s", err)
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_ID: account.user_id}
    )


async def async_setup_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Set up a tado X home from a config entry."""
    home_id = int(entry.data[CONF_HOME_ID])
    session = async_get_clientsession(hass)
    cache = SnapshotCache(hass, entry.entry_id, home_id)
    entry.async_on_unload(cache.async_close)
    seed = await cache.async_load()
    quota = QuotaTracker(clock=dt_util.utcnow)
    if seed is not None:
        # A persisted closed gate makes setup retries during a quota hold free.
        quota.restore(seed.quota)
        quota.calls.restore(seed.calls, dt_util.utcnow())
    tokens = TokenManager(
        session, ConfigEntryTokenStore(hass, entry), clock=dt_util.utcnow
    )
    client = TadoXClient(session, tokens, quota, clock=dt_util.utcnow)
    entry.async_on_unload(client.async_close)
    client.set_payload_observer(cache.record)
    api = client.home(home_id)
    monitor = QuotaMonitor(hass, entry, quota, cache)
    devices = DevicesCoordinator(hass, entry, api, monitor, seed, client=client)
    rooms = RoomsCoordinator(hass, entry, api, monitor, seed)
    home = HomeCoordinator(hass, entry, api, monitor, seed)

    if CONF_USER_ID not in entry.data:
        await _async_store_user_id(hass, entry, client)
    # One after the other: a quota error closes the gate, so the rest sends nothing.
    for coordinator in (devices, rooms, home):
        await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = TadoXRuntimeData(
        client=client,
        api=api,
        rooms=rooms,
        home=home,
        devices=devices,
        monitor=monitor,
        cache=cache,
    )
    monitor.async_start((rooms, home, devices))
    async_setup_device_registry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.info(
        "Set up %s: %s rooms, %s devices, %s planned API calls a day",
        entry.title,
        len(devices.data.topology.rooms),
        len(devices.data.topology.devices),
        monitor.plan.calls_per_day,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> bool:
    """Unload a config entry and write its cache for the last time."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.cache.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Delete the cache of a removed config entry."""
    await SnapshotCache.async_remove(hass, entry.entry_id)
    async_delete_entry_issues(hass, entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TadoXConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing a device only when tado no longer reports it."""
    return not async_is_current_device(entry, device)

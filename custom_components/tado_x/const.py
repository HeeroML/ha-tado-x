"""Constants of the tado X integration."""

from collections.abc import Mapping
from datetime import timedelta
import logging
from types import MappingProxyType
from typing import Any, Final

from .termination import TerminationPolicy

DOMAIN: Final = "tado_x"
LOGGER: Final = logging.getLogger(__package__)

MANUFACTURER: Final = "Tado"
CONFIGURATION_URL: Final = "https://app.tado.com"
DOCUMENTATION_URL: Final = "https://github.com/HeeroML/ha-tado-x"

# Config entry data (flat keys, readable by 1.8.11 after a rollback).
CONF_HOME_ID: Final = "home_id"
CONF_HOME_NAME: Final = "home_name"
CONF_USER_ID: Final = "user_id"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_TOKEN_EXPIRY: Final = "token_expiry"

# Config entry options, stored flat.
OPT_DAILY_CALL_BUDGET: Final = "daily_call_budget"
OPT_QUOTA_RESERVE: Final = "quota_reserve"
OPT_WEATHER: Final = "weather"
OPT_AIR_COMFORT: Final = "air_comfort"
OPT_RUNNING_TIMES: Final = "running_times"
OPT_MOBILE_DEVICES: Final = "mobile_devices"
OPT_HOLIDAY_MODE: Final = "holiday_mode"
OPT_MANUAL_TERMINATION: Final = "manual_termination"
OPT_TIMER_MINUTES: Final = "timer_minutes"
OPT_OFF_FOLLOWS_POLICY: Final = "off_follows_policy"
OPT_PRESENCE_ZONE: Final = "presence_zone"

DEFAULT_DAILY_CALL_BUDGET: Final = 3000
MIN_DAILY_CALL_BUDGET: Final = 100
MAX_DAILY_CALL_BUDGET: Final = 20000
DEFAULT_QUOTA_RESERVE: Final = 1000
MIN_QUOTA_RESERVE: Final = 0
MAX_QUOTA_RESERVE: Final = 10000
DEFAULT_TIMER_MINUTES: Final = 60
MIN_TIMER_MINUTES: Final = 5
MAX_TIMER_MINUTES: Final = 1440
DEFAULT_PRESENCE_ZONE: Final = "zone.home"

DEFAULT_OPTIONS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        OPT_DAILY_CALL_BUDGET: DEFAULT_DAILY_CALL_BUDGET,
        OPT_QUOTA_RESERVE: DEFAULT_QUOTA_RESERVE,
        OPT_WEATHER: True,
        OPT_AIR_COMFORT: True,
        OPT_RUNNING_TIMES: True,
        OPT_MOBILE_DEVICES: False,
        OPT_HOLIDAY_MODE: False,
        OPT_MANUAL_TERMINATION: TerminationPolicy.FOLLOW_APP.value,
        OPT_TIMER_MINUTES: DEFAULT_TIMER_MINUTES,
        OPT_OFF_FOLLOWS_POLICY: False,
        OPT_PRESENCE_ZONE: DEFAULT_PRESENCE_ZONE,
    }
)

# Entry data keys of 1.8.x. 1.9.0 ignores and never writes them; they stay in
# the entry so that a rollback to 1.8.11 keeps its settings.
LEGACY_API_CALLS_TODAY: Final = "api_calls_today"
LEGACY_API_RESET_TIME: Final = "api_reset_time"
LEGACY_HAS_AUTO_ASSIST: Final = "has_auto_assist"
LEGACY_SCAN_INTERVAL: Final = "scan_interval"
LEGACY_ENABLE_WEATHER: Final = "enable_weather"
LEGACY_ENABLE_MOBILE_DEVICES: Final = "enable_mobile_devices"
LEGACY_ENABLE_AIR_COMFORT: Final = "enable_air_comfort"
LEGACY_ENABLE_RUNNING_TIMES: Final = "enable_running_times"
LEGACY_ENABLE_FLOW_TEMP: Final = "enable_flow_temp"
LEGACY_DATA_KEYS: Final = frozenset(
    {
        LEGACY_API_CALLS_TODAY,
        LEGACY_API_RESET_TIME,
        LEGACY_HAS_AUTO_ASSIST,
        LEGACY_SCAN_INTERVAL,
        LEGACY_ENABLE_WEATHER,
        LEGACY_ENABLE_MOBILE_DEVICES,
        LEGACY_ENABLE_AIR_COMFORT,
        LEGACY_ENABLE_RUNNING_TIMES,
        LEGACY_ENABLE_FLOW_TEMP,
    }
)

# Limits of the tado X API.
MIN_TEMP: Final = 5.0
MAX_TEMP: Final = 30.0
TEMP_STEP: Final = 0.5
DEFAULT_TARGET_TEMP: Final = 21.0
OFFSET_MIN: Final = -9.9
OFFSET_MAX: Final = 9.9
OFFSET_STEP: Final = 0.1
MIN_TERMINATION_TIMER_MINUTES: Final = 1
MAX_TERMINATION_TIMER_MINUTES: Final = 1440

# How long a snapshot is served during a quota hold without a reset time from
# tado before entities go unavailable; with one, it is served until the reset.
MAX_STALE_ROOMS: Final = timedelta(hours=6)
MAX_STALE_HOME: Final = timedelta(hours=12)
MAX_STALE_DEVICES: Final = timedelta(hours=48)

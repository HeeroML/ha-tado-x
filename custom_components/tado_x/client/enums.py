"""Enumerations of the tado X API.

Every enum is open: tado adds values without notice, so parsing maps an
unknown value to None instead of failing the whole payload.
"""

from enum import StrEnum
import logging

from .telemetry import WARN_ONCE, describe_value

_LOGGER = logging.getLogger(__package__)


class Power(StrEnum):
    """Power of a room setting."""

    ON = "ON"
    OFF = "OFF"


class Presence(StrEnum):
    """Presence of the home."""

    HOME = "HOME"
    AWAY = "AWAY"


class PresenceSetting(StrEnum):
    """Presence as chosen by the user; AUTO means geofencing decides."""

    HOME = "HOME"
    AWAY = "AWAY"
    AUTO = "AUTO"


class TerminationType(StrEnum):
    """How a manual control ends."""

    MANUAL = "MANUAL"
    NEXT_TIME_BLOCK = "NEXT_TIME_BLOCK"
    TIMER = "TIMER"


class BatteryState(StrEnum):
    """Battery state of a battery powered device."""

    NORMAL = "NORMAL"
    LOW = "LOW"
    DEPLETED = "DEPLETED"

    @property
    def is_low(self) -> bool:
        """Return True when the battery needs replacing."""
        return self is not BatteryState.NORMAL


class ConnectionState(StrEnum):
    """Connection of a room or device to the bridge."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


class HomeGeneration(StrEnum):
    """Product generation of a home; only LINE_X is a tado X home."""

    LINE_X = "LINE_X"
    PRE_LINE_X = "PRE_LINE_X"
    UNKNOWN = "UNKNOWN"


class DeviceType(StrEnum):
    """Known tado X device types."""

    VA04 = "VA04"
    SU04 = "SU04"
    IB02 = "IB02"
    TR04 = "TR04"
    RU04 = "RU04"


class ControlMode(StrEnum):
    """Heat source control mode of a thermostat."""

    GAS_BOILER = "GAS_BOILER"
    UNDERFLOOR_HEATING = "UNDERFLOOR_HEATING"
    HEAT_PUMP = "HEAT_PUMP"
    NO_TEMPERATURE_CONTROL = "NO_TEMPERATURE_CONTROL"


class TemperatureLevel(StrEnum):
    """Air comfort temperature level of a room."""

    COLD = "COLD"
    COMFY = "COMFY"
    WARM = "WARM"
    HOT = "HOT"


class HumidityLevel(StrEnum):
    """Air comfort humidity level of a room."""

    DRY = "DRY"
    COMFY = "COMFY"
    HUMID = "HUMID"


class WeatherState(StrEnum):
    """Weather condition at the home."""

    SUN = "SUN"
    SUNNY = "SUNNY"
    CLOUDY = "CLOUDY"
    CLOUDY_PARTLY = "CLOUDY_PARTLY"
    CLOUDY_MOSTLY = "CLOUDY_MOSTLY"
    SCATTERED_RAIN = "SCATTERED_RAIN"
    NIGHT_CLEAR = "NIGHT_CLEAR"
    NIGHT_CLOUDY = "NIGHT_CLOUDY"
    RAIN = "RAIN"
    DRIZZLE = "DRIZZLE"
    SNOW = "SNOW"
    SCATTERED_SNOW = "SCATTERED_SNOW"
    FOGGY = "FOGGY"
    THUNDERSTORM = "THUNDERSTORM"
    THUNDERSTORMS = "THUNDERSTORMS"
    WINDY = "WINDY"
    HAIL = "HAIL"
    RAIN_HAIL = "RAIN_HAIL"
    RAIN_SNOW = "RAIN_SNOW"
    SCATTERED_RAIN_SNOW = "SCATTERED_RAIN_SNOW"
    FREEZING = "FREEZING"


class DayType(StrEnum):
    """Weekday of a schedule block.

    tado X schedules use single weekdays only; aggregate day types are not
    known to work on X homes.
    """

    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


class QuickAction(StrEnum):
    """Home-wide quick action; the value is the URL path segment."""

    BOOST = "boost"
    ALL_OFF = "allOff"
    RESUME_SCHEDULE = "resumeSchedule"


class TariffUnit(StrEnum):
    """Unit of an Energy IQ tariff."""

    M3 = "m3"
    KWH = "kWh"


def parse_enum[E: StrEnum](cls: type[E], raw: object, *, field: str) -> E | None:
    """Parse an open enum value from the API.

    Return None for null and for unknown values. An unknown value logs one
    warning per field and describe_value() of the value, which keeps only
    enum-like constants, so a value that is no constant is never logged.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return cls(raw)
        except ValueError:
            pass
    found = describe_value(raw)
    if WARN_ONCE.should_warn(("enum", field, found)):
        _LOGGER.warning(
            "Unknown value %s for %s from the tado API; it is treated as unknown",
            found,
            field,
        )
    return None

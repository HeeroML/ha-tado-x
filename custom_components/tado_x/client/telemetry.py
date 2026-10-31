"""Telemetry of the tado X client: what happened, in a form safe to share.

The helpers turn URLs, payloads, request bodies and errors into short texts
without tokens, personal data or raw bodies, so they can be logged and put
into diagnostics. Telemetry keeps the last exchanges and errors in memory and
counts requests and errors over a rolling 24 hours.
"""

from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import logging
from typing import Any, Final

from yarl import URL

from .const import (
    ENERGY_IQ_API_URL,
    HOPS_API_URL,
    LOGIN_URL,
    MINDER_API_URL,
    MY_API_URL,
)
from .errors import (
    TadoXAuthError,
    TadoXConnectionError,
    TadoXError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
)
from .redact import (
    LONG_NUMBER,
    SAFE_TITLES,
    SERIAL_NUMBER,
    describe_tado_error,
    describe_value,
    home_label,
    mask_serial,
    redact_payload,
    redact_text,
    safe_code,
    safe_title,
)

__all__ = [
    "ENUM_KEYS",
    "SAFE_TITLES",
    "SENSITIVE_KEYS",
    "WARN_ONCE",
    "ErrorRecord",
    "Exchange",
    "Telemetry",
    "WarnOnce",
    "describe_command",
    "describe_error",
    "describe_tado_error",
    "describe_value",
    "endpoint_template",
    "error_details",
    "home_label",
    "mask_serial",
    "redact_payload",
    "redact_text",
    "safe_code",
    "safe_title",
    "shape",
]

_LOGGER = logging.getLogger(__package__)

# Templates drop each host's base path, so they read like the API catalogue.
_HOSTS: Final[Mapping[str | None, tuple[str, str]]] = {
    URL(HOPS_API_URL).host: ("hops", ""),
    URL(MY_API_URL).host: ("my", URL(MY_API_URL).path),
    URL(MINDER_API_URL).host: ("minder", URL(MINDER_API_URL).path),
    URL(ENERGY_IQ_API_URL).host: ("eiq", URL(ENERGY_IQ_API_URL).path),
    URL(LOGIN_URL).host: ("login", ""),
}
_ID_SEGMENTS: Final = {
    "homes": "home",
    "rooms": "room",
    "zones": "zone",
    "devices": "device",
    "mobileDevices": "mobile",
}
_REDACTED: Final = "<redacted>"
_BUCKETS: Final = 1440

SENSITIVE_KEYS: Final = frozenset(
    {
        "access_token",
        "address",
        "contactDetails",
        "device_code",
        "deviceMetadata",
        "email",
        "firstName",
        "geolocation",
        "homeName",
        "id_token",
        "lastName",
        "latitude",
        "location",
        "longitude",
        "name",
        "phone",
        "refresh_token",
        "roomName",
        "token",
        "user_code",
        "user_id",
        "userId",
        "username",
        "verification_uri",
        "verification_uri_complete",
    }
)
"""Keys whose values shape() never describes: tokens, codes and personal data."""

ENUM_KEYS: Final = frozenset(
    {
        "batteryState",
        "controlMode",
        "dayType",
        "generation",
        "homePresence",
        "humidityLevel",
        "mountingState",
        "percentageUnit",
        "power",
        "presence",
        "skills",
        "state",
        "stripeType",
        "temperatureLevel",
        "temperatureUnit",
        "type",
        "zoneType",
        "dataIntervals.value",
        "freshness.value",
        "weatherState.value",
    }
)
"""Keys, as "key" or "parent.key", whose enum-like values shape() keeps."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _template_segment(previous: str, segment: str) -> str | None:
    """Return the placeholder name of a path segment, or None to keep it."""
    if segment.isdecimal():
        return _ID_SEGMENTS.get(previous, "id")
    if SERIAL_NUMBER.fullmatch(segment):
        return "serial"
    if LONG_NUMBER.search(segment):
        return "id"
    return None


def _walk(url: URL) -> tuple[str, str, dict[str, str]]:
    """Return the short host, the templated path and the raw value per placeholder."""
    name, base = _HOSTS.get(url.host, (url.host or "?", ""))
    path = url.path
    if base and (path == base or path.startswith(f"{base}/")):
        path = path[len(base) :] or "/"
    parts: list[str] = []
    values: dict[str, str] = {}
    previous = ""
    for segment in path.split("/"):
        if (placeholder := _template_segment(previous, segment)) is None:
            parts.append(segment)
        else:
            parts.append(f"{{{placeholder}}}")
            values[placeholder] = segment
        previous = segment
    return name, "/".join(parts), values


def endpoint_template(method: str, url: URL) -> str:
    """Return a low-cardinality name of a request without ids or query values.

    For example "GET hops:/homes/{home}/rooms/{room}/schedule". Ids after
    homes, rooms, zones, devices and mobileDevices become {home}, {room},
    {zone}, {device} and {mobile}, serial numbers {serial}, and any other
    segment with a number {id}. Query keys are kept and their values dropped,
    as in "GET my:/homes/{home}/zones/{zone}/dayReport?date".
    """
    name, path, _ = _walk(url)
    query = "&".join(dict.fromkeys(url.query))
    return f"{method.upper()} {name}:{path}{f'?{query}' if query else ''}"


def _shape_key(key: object) -> str:
    text = str(key)
    if SERIAL_NUMBER.fullmatch(text):
        return mask_serial(text)
    return redact_text(text, 40) or ""


def shape(payload: object, *, depth: int = 3, max_keys: int = 25) -> object:
    """Return the structure of a payload without its values.

    A dict keeps its keys and shapes their values; the value of a key in
    SENSITIVE_KEYS becomes "<redacted>", and keys beyond max_keys are
    counted under "…". A list becomes {"list": length, "item": shape of the
    first item}. Scalars become describe_value(), which keeps enum-like
    constants only under the keys in ENUM_KEYS. Below depth a container is
    only described, as in "dict[4]".
    """
    return _shape(payload, ("", ""), depth, max_keys)


def _shape(
    payload: object, where: tuple[str, str], depth: int, max_keys: int
) -> object:
    """Shape a payload found at where, its parent key and its own key.

    The items of a list are found where the list is.
    """
    if isinstance(payload, Mapping):
        if depth <= 0:
            return describe_value(payload)
        result: dict[str, object] = {}
        for index, (key, value) in enumerate(payload.items()):
            if index == max_keys:
                result["…"] = f"+{len(payload) - max_keys} keys"
                break
            result[_shape_key(key)] = (
                _REDACTED
                if key in SENSITIVE_KEYS
                else _shape(value, (where[1], str(key)), depth - 1, max_keys)
            )
        return result
    if isinstance(payload, list | tuple):
        if depth <= 0:
            return describe_value(payload)
        if not payload:
            return {"list": 0}
        return {
            "list": len(payload),
            "item": _shape(payload[0], where, depth - 1, max_keys),
        }
    parent, key = where
    return describe_value(
        payload, constants=key in ENUM_KEYS or f"{parent}.{key}" in ENUM_KEYS
    )


def _get(data: object, *keys: str) -> object:
    """Return the value at a key path of nested dicts, or None."""
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _scalar(value: object) -> str:
    """Format an allow-listed field of a request body."""
    if isinstance(value, bool | int | float):
        return str(value)
    return describe_value(value)


def _termination(raw: object) -> str:
    kind = _scalar(_get(raw, "type"))
    seconds = _get(raw, "durationInSeconds")
    return kind if seconds is None else f"{kind} {_scalar(seconds)} s"


def _setting(raw: object) -> str:
    power = _scalar(_get(raw, "power"))
    temperature = _get(raw, "temperature", "value")
    return power if temperature is None else f"{power} {_scalar(temperature)} °C"


def _fields(data: object, names: Mapping[str, tuple[str, ...]]) -> str:
    """Format the allow-listed fields present in a body as "name=value, ..."."""
    return ", ".join(
        f"{name}={_scalar(_get(data, *path))}"
        for name, path in names.items()
        if _get(data, *path) is not None
    )


def describe_command(method: str, url: URL, body: object) -> str:
    """Return a short audit text of a write, built from allow-listed fields only.

    For example "manualControl room 3: ON 21.0 °C until TIMER 3600 s",
    "device VA…0101: childLockEnabled=True" or "tariff (value hidden)". An
    unknown write is named by its endpoint template.
    """
    _, path, ids = _walk(url)
    room = f"room {ids.get('room')}"
    serial = mask_serial(ids.get("serial", ""))
    match method.upper(), path.removeprefix("/homes/{home}"):
        case "POST", "/rooms/{room}/manualControl":
            setting = _setting(_get(body, "setting"))
            termination = _termination(_get(body, "termination"))
            return f"manualControl {room}: {setting} until {termination}"
        case "DELETE", "/rooms/{room}/manualControl":
            return f"resume schedule {room}"
        case ("POST" | "DELETE") as verb, "/rooms/{room}/openWindow":
            return f"open window {room}: {'on' if verb == 'POST' else 'off'}"
        case "POST", "/rooms/{room}/schedule":
            day = _scalar(_get(body, "dayType"))
            blocks = _get(body, "daySchedule")
            count = len(blocks) if isinstance(blocks, list) else 0
            return f"schedule {room} {day}: {count} blocks"
        case "POST", action if action.startswith("/quickActions/"):
            return f"quick action {action.removeprefix('/quickActions/')}"
        case "PUT", "/presenceLock":
            return f"presence {_scalar(_get(body, 'homePresence'))}"
        case "DELETE", "/presenceLock":
            return "presence AUTO"
        case "PATCH", "/roomsAndDevices/devices/{serial}":
            fields = _fields(
                body,
                {
                    "temperatureOffset": ("temperatureOffset",),
                    "childLockEnabled": ("childLockEnabled",),
                },
            )
            return f"device {serial}: {fields}"
        case "POST", "/roomsAndDevices/devices/{serial}/identify":
            return f"identify {serial}"
        case "PATCH", "/roomsAndDevices/rooms/{room}":
            default = _termination(_get(body, "deviceManualControlTermination"))
            return f"{room} termination default {default}"
        case "PATCH", "/settings/holidayMode":
            return f"holiday mode enabled={_scalar(_get(body, 'enabled'))}"
        case "PATCH", "/settings/flowTemperatureOptimization":
            fields = _fields(
                body,
                {
                    "max": ("maxFlowTemperature",),
                    "autoAdaptation": ("autoAdaptation", "enabled"),
                },
            )
            return f"flow temperature {fields}"
        case "POST", "/meterReadings":
            return "meter reading (value hidden)"
        case "POST", "/tariffs":
            return "tariff (value hidden)"
    return endpoint_template(method, url)


def error_details(err: TadoXError) -> tuple[int | None, str | None]:
    """Return the HTTP status and a log-safe message of a client error."""
    match err:
        case TadoXRequestError():
            return err.status, describe_tado_error(err.code, err.title)
        case TadoXServerError():
            return err.status, None
        case TadoXRateLimitError():
            return None if err.local else 429, str(err)
        case TadoXAuthError():
            return err.status, safe_code(err.reason)
        case TadoXParseError() if err.found is not None:
            return None, redact_text(f"{err} ({err.found})")
        case TadoXConnectionError(cause=str() as cause):
            return None, redact_text(f"{err} ({cause})")
    return None, redact_text(str(err))


def describe_error(err: TadoXError) -> str:
    """Return a short log-safe text of a client error.

    For example "HTTP 503", "HTTP 422 code=… title=…" or
    "TadoXConnectionError: cannot reach hops.tado.com (TimeoutError)".
    """
    status, message = error_details(err)
    if status is not None:
        return f"HTTP {status}" if message is None else f"HTTP {status} {message}"
    name = type(err).__name__
    return name if message is None else f"{name}: {message}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Exchange:
    """One HTTP attempt.

    attempt is 2 for the retry after a 401. outcome is "ok", "retried" for a
    401 that is retried with a new token, or the class name of the error the
    attempt raised. shape is the shape() of a decoded 2xx body, and error a
    short safe text such as "HTTP 503", "code=… title=…" or "TimeoutError".
    """

    at: datetime
    method: str
    endpoint: str
    status: int | None
    duration_ms: int
    attempt: int = 1
    limit: int | None = None
    remaining: int | None = None
    reset_in_s: int | None = None
    outcome: str = "ok"
    shape: object | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorRecord:
    """One error, with the context it happened in, such as "poll rooms".

    An error that repeats a kept record is counted there: count is the
    number of occurrences, at the first and last_at the latest of them,
    None until the error repeats.
    """

    at: datetime
    kind: str
    context: str
    endpoint: str | None = None
    status: int | None = None
    message: str | None = None
    count: int = 1
    last_at: datetime | None = None


def _error_key(record: ErrorRecord) -> tuple[object, ...]:
    """Return what makes two records the same error in the same context."""
    return (
        record.kind,
        record.context,
        record.endpoint,
        record.status,
        record.message,
    )


class WarnOnce:
    """Keys of the warnings already logged, so that each is logged once.

    The set is bounded: when it is full, the oldest key is forgotten and its
    warning may be logged again.
    """

    MAX_KEYS: Final = 512

    def __init__(self, max_keys: int = MAX_KEYS) -> None:
        """Initialize an empty set that holds up to max_keys keys."""
        self._keys: dict[tuple[str, ...], None] = {}
        self._max_keys = max_keys

    def should_warn(self, key: tuple[str, ...]) -> bool:
        """Return True the first time a key is seen, and remember it."""
        if key in self._keys:
            return False
        self._keys[key] = None
        if len(self._keys) > self._max_keys:
            del self._keys[next(iter(self._keys))]
        return True

    def clear(self) -> None:
        """Forget every key, so that each warning may be logged again."""
        self._keys.clear()


WARN_ONCE: Final = WarnOnce()
"""The process-wide WarnOnce of the client's field and item warnings."""


class _RollingCounts:
    """Counts per key over a rolling 24 hours in one-minute buckets.

    Only minutes with counts are kept, at most 1,440 of them.
    """

    def __init__(self) -> None:
        self._minutes: deque[tuple[int, Counter[str]]] = deque()

    def add(self, key: str, at: datetime) -> None:
        minute = int(at.timestamp()) // 60
        # A clock that went backwards counts into the newest bucket.
        if self._minutes and self._minutes[-1][0] >= minute:
            self._minutes[-1][1][key] += 1
        else:
            self._minutes.append((minute, Counter({key: 1})))
        self._expire(minute)

    def totals(self, now: datetime) -> dict[str, int]:
        self._expire(int(now.timestamp()) // 60)
        totals: Counter[str] = Counter()
        for _, counts in self._minutes:
            totals.update(counts)
        return dict(sorted(totals.items()))

    def _expire(self, minute: int) -> None:
        while self._minutes and self._minutes[0][0] <= minute - _BUCKETS:
            self._minutes.popleft()


def _quota_text(exchange: Exchange) -> str:
    if exchange.remaining is None:
        return ""
    left = (
        f"{exchange.remaining}"
        if exchange.limit is None
        else f"{exchange.remaining}/{exchange.limit}"
    )
    reset = "" if exchange.reset_in_s is None else f", reset in {exchange.reset_in_s} s"
    return f", quota {left} left{reset}"


def _exchange_suffix(exchange: Exchange) -> str:
    """Return the quota, retry and error part of an exchange's log line."""
    text = _quota_text(exchange)
    if exchange.attempt > 1:
        text += f", attempt {exchange.attempt} after 401"
    if exchange.outcome != "ok":
        text += f": {exchange.outcome}"
    if exchange.error is not None:
        text += f" ({exchange.error})"
    return text


def _plain(record: Exchange | ErrorRecord) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in asdict(record).items()
    }


class Telemetry:
    """Recent exchanges and errors of one config entry, in memory only.

    The last EXCHANGES HTTP attempts and ERRORS errors are kept, and requests
    per endpoint and errors per kind are counted over a rolling 24 hours.
    Every exchange is logged at debug level. An error that repeats a kept
    record is counted in it, and the record moves to the end, so an outage
    does not flush the others, even when several polls fail in turn.
    An error object is recorded once: when a caller records an error the
    client recorded already, for example a failed token refresh that also
    fails a poll, nothing changes. The error is marked, not kept, so its
    traceback and frames are freed.
    """

    EXCHANGES: Final = 50
    ERRORS: Final = 20

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        """Initialize empty telemetry with the clock that stamps errors."""
        self._clock = clock
        self._exchanges: deque[Exchange] = deque(maxlen=self.EXCHANGES)
        self._errors: dict[tuple[object, ...], ErrorRecord] = {}
        self._requests = _RollingCounts()
        self._kinds = _RollingCounts()
        self._listeners: list[Callable[[], None]] = []

    def record_exchange(self, exchange: Exchange) -> None:
        """Keep and count one HTTP attempt, and log it at debug level."""
        self._exchanges.append(exchange)
        self._requests.add(exchange.endpoint, exchange.at)
        _LOGGER.debug(
            "%s -> %s in %s ms%s",
            exchange.endpoint,
            "no answer" if exchange.status is None else exchange.status,
            exchange.duration_ms,
            _exchange_suffix(exchange),
        )

    def record_error(
        self,
        *,
        kind: str,
        context: str,
        endpoint: str | None = None,
        status: int | None = None,
        message: str | None = None,
    ) -> None:
        """Keep and count one error; the message passes redact_text().

        An error that repeats a kept record increments its count and moves it
        to the end; otherwise the oldest record goes when ERRORS are kept.
        """
        at = self._clock()
        record = ErrorRecord(
            at=at,
            kind=kind,
            context=context,
            endpoint=endpoint,
            status=status,
            message=redact_text(message),
        )
        key = _error_key(record)
        if (kept := self._errors.pop(key, None)) is not None:
            record = replace(kept, count=kept.count + 1, last_at=at)
        self._errors[key] = record
        if len(self._errors) > self.ERRORS:
            del self._errors[next(iter(self._errors))]
        self._kinds.add(kind, at)
        for listener in list(self._listeners):
            listener()

    def record_tado_error(self, err: TadoXError, *, context: str) -> None:
        """Record a client error by its class, its request, status and message."""
        if err.recorded:
            return
        err.recorded = True
        status, message = error_details(err)
        self.record_error(
            kind=type(err).__name__,
            context=context,
            endpoint=err.request,
            status=status,
            message=message,
        )

    @property
    def last_error(self) -> ErrorRecord | None:
        """Return the newest error, if any."""
        return next(reversed(self._errors.values()), None)

    def requests_by_endpoint(self, now: datetime | None = None) -> dict[str, int]:
        """Return the HTTP attempts of the 24 hours before now per endpoint."""
        return self._requests.totals(self._clock() if now is None else now)

    def errors_by_kind(self, now: datetime | None = None) -> dict[str, int]:
        """Return the errors of the 24 hours before now per kind."""
        return self._kinds.totals(self._clock() if now is None else now)

    def exchanges(self) -> list[Exchange]:
        """Return the kept exchanges, oldest first."""
        return list(self._exchanges)

    def errors(self) -> list[ErrorRecord]:
        """Return the kept errors, oldest first by their latest occurrence."""
        return list(self._errors.values())

    def as_diagnostics(self, now: datetime | None = None) -> dict[str, Any]:
        """Return everything as JSON-friendly data, newest last."""
        now = self._clock() if now is None else now
        last = self.last_error
        return {
            "exchanges": [_plain(exchange) for exchange in self._exchanges],
            "errors": [_plain(error) for error in self._errors.values()],
            "requests_by_endpoint_24h": self.requests_by_endpoint(now),
            "errors_by_kind_24h": self.errors_by_kind(now),
            "last_error": None if last is None else _plain(last),
        }

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call cb after every recorded error; return a remover."""
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

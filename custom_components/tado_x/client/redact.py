"""Text helpers that keep tokens and personal data out of logs and errors.

This module imports nothing of the client, so that the errors can build
their messages with it; telemetry.py re-exports every helper.
"""

from collections.abc import Mapping
import re
from typing import Final

SERIAL_NUMBER: Final = re.compile(r"[A-Z]{2}\d{6,}")
"""A tado serial number, such as "VA0000000101"."""
LONG_NUMBER: Final = re.compile(r"\d{4,}")
"""A run of four or more digits, such as a home id or a postal code."""

_ENUM_LIKE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
_CODE: Final = re.compile(r"[A-Za-z][A-Za-z_]{0,40}(?:\.[A-Za-z][A-Za-z_]{0,40}){0,4}")
_URL_TEXT: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_EMAIL: Final = re.compile(r"[^\s@]+@[^\s@]+")
_BEARER: Final = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
# "key=value" or "key": "value" of a token or code; a bare "code" only in a
# query, so that the "code=not.found" of describe_tado_error() stays.
_SECRET_FIELD: Final = re.compile(
    r"(\b(?:access_token|refresh_token|id_token|device_code|user_code|token)"
    r"|(?<=[?&])code)"
    r"(\"?\s*[:=]\s*\"?)[^\s\"&,;}]+",
    re.IGNORECASE,
)
_USER_CODE: Final = re.compile(r"(?<![\w-])[A-Z0-9]{4}-[A-Z0-9]{4}(?![\w-])")
_TIMESTAMP: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})?"
)
_CLOCK: Final = re.compile(r"\d{2}:\d{2}")
_NUMBER_TEXT: Final = re.compile(r"-?\d+(?:\.\d+)*")
_MASKED_SERIAL: Final = re.compile(r"[A-Z]{2}…[A-Za-z0-9]{4}")
_PAYLOAD_TEXT: Final = (_TIMESTAMP, _CLOCK, _NUMBER_TEXT, _MASKED_SERIAL)
_LONG_RUN: Final = re.compile(r"[A-Za-z0-9_+/=.-]{20,}")
_RUN_PARTS: Final = re.compile(r"[/.]")
_IDENTIFIER: Final = re.compile(r"[a-z]{1,16}(?:[A-Z][a-z]{1,15}|_[a-z]{1,16})+")
_WORD_LIMIT: Final = 32
_INPUT_LIMIT: Final = 2000
_REDACTED: Final = "<redacted>"

SAFE_TITLES: Final = frozenset({"Timezone of home # not found"})
"""tado error titles, after redact_text(), that hold no personal data."""


def mask_serial(serial: str) -> str:
    """Keep the type prefix and the last four characters of a serial number."""
    return f"{serial[:2]}…{serial[-4:]}"


def home_label(home_id: int) -> str:
    """Return the neutral name of a home in logs, such as "home …0002".

    It keeps the last four digits of the home id. It stands in for the entry
    title, which is the home's name from tado unless the user renamed it.
    """
    return f"home …{str(home_id)[-4:]}"


def _secret_run(match: re.Match[str]) -> str:
    """Blank a long run that looks like a token or a UUID, not like a word.

    A run with a digit or a dash is a secret. So is a run with a part of 32
    or more characters between "/" and "." unless that part is a camelCase
    or snake_case name of short words, such as "supportsFlowTemperature";
    a long part of letters without humps or underscores, or with a word of
    17 or more letters, is a token. Paths and JSON paths of long key names
    stay.
    """
    run = match.group()
    if "-" in run or any(char.isdigit() for char in run):
        return _REDACTED
    if any(
        len(part) >= _WORD_LIMIT and not _IDENTIFIER.fullmatch(part)
        for part in _RUN_PARTS.split(run)
    ):
        return _REDACTED
    return run


def redact_text(text: str | None, limit: int = 120) -> str | None:
    """Return text from tado that is safe to log, or None for None.

    Every run of four or more digits becomes "#". URLs, e-mail addresses,
    bearer tokens, the values of token and code fields such as
    "refresh_token=…", device-login user codes such as "WDJB-MJHT" and long
    token-like runs are blanked as well, whitespace and control characters
    collapse to single spaces so the text cannot fake a log line, and the
    result is cut to limit characters. Only the first 2,000 characters are
    read, up to the last whole word, and "…" stands for the rest.
    """
    if text is None:
        return None
    printable = "".join(char if char.isprintable() else " " for char in text)
    cleaned = " ".join(printable.split())
    if len(cleaned) > _INPUT_LIMIT:
        # Some patterns take quadratic time on a long run. The word the cut
        # splits is dropped: its start may be too short to look like a token.
        cleaned = f"{cleaned[:_INPUT_LIMIT].rpartition(' ')[0]}…"
    cleaned = _URL_TEXT.sub("<url>", cleaned)
    cleaned = _EMAIL.sub("<email>", cleaned)
    cleaned = _BEARER.sub(_REDACTED, cleaned)
    cleaned = _SECRET_FIELD.sub(rf"\1\2{_REDACTED}", cleaned)
    cleaned = _USER_CODE.sub(_REDACTED, cleaned)
    cleaned = _LONG_RUN.sub(_secret_run, cleaned)
    cleaned = LONG_NUMBER.sub("#", cleaned)
    if len(cleaned) > limit:
        cleaned = f"{cleaned[: limit - 1]}…"
    return cleaned


def describe_value(value: object, *, constants: bool = True) -> str:
    """Describe a value from the API without revealing it.

    Numbers and booleans give their type, a container its kind and length,
    as in "list[3]", and a string its length, as in "<str len=7>". Only
    enum-like constants such as "DEPLETED" are kept, unless constants is
    False, and serial numbers are masked to "VA…0101".
    """
    match value:
        case None:
            return "null"
        case bool():
            return "bool"
        case int():
            return "int"
        case float():
            return "float"
        case str() if SERIAL_NUMBER.fullmatch(value):
            return mask_serial(value)
        case str() if (
            constants and _ENUM_LIKE.fullmatch(value) and not LONG_NUMBER.search(value)
        ):
            return value
        case str():
            return f"<str len={len(value)}>"
        case Mapping():
            return f"dict[{len(value)}]"
        case list() | tuple():
            return f"list[{len(value)}]"
    return type(value).__name__


def redact_payload(payload: object) -> object:
    """Return a copy of a decoded payload with only safe strings in it.

    Dicts, lists, numbers, booleans and null are kept. A string is kept if
    it is a timestamp, a clock time such as "06:30", a number or version
    such as "287.1", or a masked serial; any other string becomes
    describe_value(), so only enum-like constants stay readable and serials
    are masked. The keys stay.
    """
    match payload:
        case str() if any(pattern.fullmatch(payload) for pattern in _PAYLOAD_TEXT):
            return payload
        case str():
            return describe_value(payload)
        case Mapping():
            return {key: redact_payload(value) for key, value in payload.items()}
        case list() | tuple():
            return [redact_payload(item) for item in payload]
    return payload


def safe_code(code: str) -> str:
    """Return a tado or OAuth error code, or describe_value() if it is no code.

    A code is dotted words such as "not.found" or "invalid_grant", without
    digits, so a token or an id never passes as one.
    """
    return code if _CODE.fullmatch(code) else describe_value(code)


def safe_title(title: str) -> str:
    """Return a tado error title if it is a known template, else "<str len=N>".

    A title is free text that can name a room, a home or an address, so
    only the templates in SAFE_TITLES pass, after redact_text().
    """
    redacted = redact_text(title)
    return redacted if redacted in SAFE_TITLES else f"<str len={len(title)}>"


def describe_tado_error(code: str | None, title: str | None) -> str | None:
    """Return "code=… title=…" of a tado error answer, or None without either.

    The code passes safe_code() and the title safe_title().
    """
    parts: list[str] = []
    if code:
        parts.append(f"code={safe_code(code)}")
    if title:
        parts.append(f"title={safe_title(title)}")
    return " ".join(parts) or None

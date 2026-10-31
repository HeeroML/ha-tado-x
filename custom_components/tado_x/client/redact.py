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
_LONG_RUN: Final = re.compile(r"[A-Za-z0-9_+/=.-]{20,}")
_REDACTED: Final = "<redacted>"


def mask_serial(serial: str) -> str:
    """Keep the type prefix and the last four characters of a serial number."""
    return f"{serial[:2]}…{serial[-4:]}"


def _secret_run(match: re.Match[str]) -> str:
    """Blank a long run that looks like a token or a UUID, not like a word."""
    run = match.group()
    if "-" in run or any(char.isdigit() for char in run):
        return _REDACTED
    return run


def redact_text(text: str | None, limit: int = 120) -> str | None:
    """Return text from tado that is safe to log, or None for None.

    Every run of four or more digits becomes "#". URLs, e-mail addresses,
    bearer tokens and long token-like runs are blanked as well, whitespace
    and control characters collapse to single spaces so the text cannot fake
    a log line, and the result is cut to limit characters.
    """
    if text is None:
        return None
    printable = "".join(char if char.isprintable() else " " for char in text)
    cleaned = " ".join(printable.split())
    cleaned = _URL_TEXT.sub("<url>", cleaned)
    cleaned = _EMAIL.sub("<email>", cleaned)
    cleaned = _BEARER.sub(_REDACTED, cleaned)
    cleaned = _LONG_RUN.sub(_secret_run, cleaned)
    cleaned = LONG_NUMBER.sub("#", cleaned)
    if len(cleaned) > limit:
        cleaned = f"{cleaned[: limit - 1]}…"
    return cleaned


def describe_value(value: object) -> str:
    """Describe a value from the API without revealing it.

    Numbers and booleans give their type, a container its kind and length,
    as in "list[3]", and a string its length, as in "<str len=7>". Only
    enum-like constants such as "DEPLETED" are kept, and serial numbers are
    masked to "VA…0101".
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
        case str() if _ENUM_LIKE.fullmatch(value) and not LONG_NUMBER.search(value):
            return value
        case str():
            return f"<str len={len(value)}>"
        case Mapping():
            return f"dict[{len(value)}]"
        case list() | tuple():
            return f"list[{len(value)}]"
    return type(value).__name__


def safe_code(code: str) -> str:
    """Return a tado or OAuth error code, or describe_value() if it is no code.

    A code is dotted words such as "not.found" or "invalid_grant", without
    digits, so a token or an id never passes as one.
    """
    return code if _CODE.fullmatch(code) else describe_value(code)


def describe_tado_error(code: str | None, title: str | None) -> str | None:
    """Return "code=… title=…" of a tado error answer, or None without either.

    The code passes safe_code() and the title redact_text().
    """
    parts: list[str] = []
    if code:
        parts.append(f"code={safe_code(code)}")
    if title:
        parts.append(f"title={redact_text(title)}")
    return " ".join(parts) or None


def home_label(home_id: int) -> str:
    """Name a home in a log line by the last four digits of its id, "home …0002".

    tado's name of a home is personal and is the default title of a config
    entry, so neither is ever logged.
    """
    return f"home …{str(home_id)[-4:]}"

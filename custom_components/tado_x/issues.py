"""Repair issues of the tado X integration; none of them is fixable."""

from datetime import datetime, timedelta
from typing import Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .client import RateLimitInfo
from .const import DOCUMENTATION_URL, DOMAIN
from .runtime import TadoXConfigEntry

QUOTA_EXHAUSTED: Final = "quota_exhausted"
QUOTA_LOW: Final = "quota_low"
LOW_QUOTA_TIER: Final = "low_quota_tier"
NOT_TADO_X_HOME: Final = "not_tado_x_home"
DUPLICATE_HOME: Final = "duplicate_home"
BREAKING_CHANGES: Final = "breaking_changes_1_9"

ENTRY_ISSUES: Final = (
    QUOTA_EXHAUSTED,
    QUOTA_LOW,
    LOW_QUOTA_TIER,
    NOT_TADO_X_HOME,
    DUPLICATE_HOME,
)
LOW_TIER_LIMIT: Final = 1000
CONSERVING_GRACE: Final = timedelta(minutes=10)

QUOTA_URL: Final = f"{DOCUMENTATION_URL}#api-rate-limits--smart-polling"
TROUBLESHOOTING_URL: Final = f"{DOCUMENTATION_URL}#troubleshooting"
RELEASES_URL: Final = f"{DOCUMENTATION_URL}/releases"


def issue_id(key: str, entry_id: str) -> str:
    """Return the id of an issue of one entry."""
    return f"{key}_{entry_id}"


def format_time(value: datetime) -> str:
    """Format a time for an issue text, in the local time of Home Assistant."""
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M")


@callback
def _async_set(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    key: str,
    placeholders: dict[str, str] | None,
    *,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    url: str = QUOTA_URL,
) -> None:
    """Create an issue of an entry with these placeholders, or delete it for None."""
    if placeholders is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry.entry_id))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(key, entry.entry_id),
        is_fixable=False,
        severity=severity,
        translation_key=key,
        translation_placeholders={"home": entry.title, **placeholders},
        learn_more_url=url,
    )


@callback
def async_create_not_tado_x_issue(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Report that the entry points at a home that is not a tado X home (#40)."""
    _async_set(
        hass,
        entry,
        NOT_TADO_X_HOME,
        {},
        severity=ir.IssueSeverity.ERROR,
        url=TROUBLESHOOTING_URL,
    )


@callback
def async_create_duplicate_home_issue(
    hass: HomeAssistant, entry: TadoXConfigEntry
) -> None:
    """Report a second entry for the same home found by the migration."""
    _async_set(hass, entry, DUPLICATE_HOME, {}, url=TROUBLESHOOTING_URL)


@callback
def async_create_breaking_changes_issue(hass: HomeAssistant) -> None:
    """Report the breaking changes of 1.9 once; the user can dismiss it."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        BREAKING_CHANGES,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=BREAKING_CHANGES,
        learn_more_url=RELEASES_URL,
    )


@callback
def async_sync_quota_issues(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    *,
    info: RateLimitInfo | None,
    blocked_until: datetime | None,
    conserving_since: datetime | None,
    reserve: int,
    now: datetime,
    rooms_period: timedelta,
) -> None:
    """Create or delete the quota_exhausted, quota_low and low_quota_tier issues.

    quota_exhausted lasts from the closing of the quota gate to the first
    successful response, quota_low from 10 minutes of conserving to its end,
    and low_quota_tier as long as the account allows 1,000 calls a day or fewer.
    """
    exhausted = None if blocked_until is None else {"reset": format_time(blocked_until)}
    _async_set(hass, entry, QUOTA_EXHAUSTED, exhausted)
    if conserving_since is None or now - conserving_since >= CONSERVING_GRACE:
        remaining = None if info is None else info.remaining
        low = {"remaining": str(remaining), "reserve": str(reserve)}
        _async_set(hass, entry, QUOTA_LOW, None if conserving_since is None else low)
    if info is not None and info.limit is not None:
        minutes = round(rooms_period.total_seconds() / 60)
        tier = {"limit": str(info.limit), "rooms_minutes": str(minutes)}
        _async_set(
            hass, entry, LOW_QUOTA_TIER, tier if info.limit <= LOW_TIER_LIMIT else None
        )


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every issue of a removed entry."""
    for key in ENTRY_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry_id))

"""Repair issues of the tado X integration; none of them is fixable."""

from datetime import datetime

from homeassistant.core import HomeAssistant, callback

from .client import RateLimitInfo
from .runtime import TadoXConfigEntry


@callback
def async_create_not_tado_x_issue(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Report that the entry points at a home that is not a tado X home (#40)."""
    raise NotImplementedError


@callback
def async_create_duplicate_home_issue(
    hass: HomeAssistant, entry: TadoXConfigEntry
) -> None:
    """Report a second entry for the same home found by the migration."""
    raise NotImplementedError


@callback
def async_create_breaking_changes_issue(hass: HomeAssistant) -> None:
    """Report the breaking changes of 1.9 once; the user can dismiss it."""
    raise NotImplementedError


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
) -> None:
    """Create or delete the quota_exhausted, quota_low and low_quota_tier issues."""
    raise NotImplementedError


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every issue of a removed entry."""
    raise NotImplementedError

"""Errors raised by the tado X cloud client.

The taxonomy decides what the caller does: an auth error needs a new login,
a transient error is retried later and never starts a reauth, and every other
error is a definitive answer from tado for this one request.

The message of an error is safe to log: it never holds a token, personal
data or a raw body, while the attributes keep what tado sent.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from .redact import describe_tado_error, safe_code

if TYPE_CHECKING:
    from .models import RateLimitInfo


class TadoXError(Exception):
    """Base class of every error raised by the tado X client.

    request names the HTTP request that failed, as an endpoint template such
    as "GET hops:/homes/{home}/rooms"; it is None when no request was made.
    """

    request: str | None = None


class TadoXAuthError(TadoXError):
    """The credentials were rejected for good; a new login is required."""

    def __init__(self, reason: str) -> None:
        """Initialize with the OAuth error code or a short internal reason."""
        super().__init__(safe_code(reason))
        self.reason = reason


class TadoXTransientError(TadoXError):
    """A temporary failure; retry later and never start a reauth."""


class TadoXConnectionError(TadoXTransientError):
    """No HTTP response arrived: network, DNS, TLS or timeout."""


class TadoXServerError(TadoXTransientError):
    """A 5xx from any host, or from login.tado.com a 429 or a 4xx without an OAuth error."""

    def __init__(self, status: int) -> None:
        """Initialize with the HTTP status."""
        super().__init__(f"tado answered HTTP {status}")
        self.status = status


class TadoXRateLimitError(TadoXTransientError):
    """The API quota is used up, or the local quota gate refused the request."""

    def __init__(
        self,
        info: RateLimitInfo | None,
        reset_at: datetime | None,
        *,
        local: bool,
    ) -> None:
        """Initialize with the last rate limit headers and the known reset time."""
        source = "local quota gate" if local else "tado API"
        super().__init__(f"rate limited by the {source}")
        self.info = info
        self.reset_at = reset_at
        self.local = local


class TadoXRequestError(TadoXError):
    """tado refused the request with a 4xx answer."""

    def __init__(
        self, status: int, code: str | None = None, title: str | None = None
    ) -> None:
        """Initialize with the status and the first entry of the error body.

        The message carries the code and the title only as describe_tado_error()
        gives them.
        """
        details = describe_tado_error(code, title)
        super().__init__(
            f"tado answered HTTP {status}"
            if details is None
            else f"tado answered HTTP {status}: {details}"
        )
        self.status = status
        self.code = code
        self.title = title


class TadoXForbiddenError(TadoXRequestError):
    """HTTP 403, for example a past day report without Auto-Assist."""


class TadoXNotFoundError(TadoXRequestError):
    """HTTP 404, usually a feature this home does not have."""


class TadoXHomeNotXError(TadoXNotFoundError):
    """The home has no tado X (LINE_X) representation on hops."""

    def __init__(
        self, home_id: int, code: str | None = None, title: str | None = None
    ) -> None:
        """Initialize with the home id the hops API does not know."""
        super().__init__(404, code, title)
        self.home_id = home_id


class TadoXParseError(TadoXError):
    """A 2xx payload failed boundary validation."""

    def __init__(self, endpoint: str, path: str, *, found: str | None = None) -> None:
        """Initialize with the endpoint key, the JSON path that failed and what was there.

        found describes the value without revealing it, such as "missing",
        "null" or "<str len=3>".
        """
        super().__init__(f"unexpected {endpoint} payload at {path}")
        self.endpoint = endpoint
        self.path = path
        self.found = found


type DeviceFlowFailure = Literal["expired", "denied"]


class TadoXDeviceFlowError(TadoXError):
    """The device-code login ended without a token."""

    def __init__(self, reason: DeviceFlowFailure) -> None:
        """Initialize with the reason, "expired" or "denied"."""
        super().__init__(reason)
        self.reason: DeviceFlowFailure = reason

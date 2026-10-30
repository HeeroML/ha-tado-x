"""Tests for the error taxonomy of the tado X client."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.tado_x.client import (
    RateLimitInfo,
    TadoXAuthError,
    TadoXConnectionError,
    TadoXDeviceFlowError,
    TadoXError,
    TadoXForbiddenError,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    TadoXTransientError,
)

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("error", "parents"),
    [
        (TadoXAuthError("invalid_grant"), (TadoXError,)),
        (TadoXConnectionError("hops.tado.com"), (TadoXTransientError,)),
        (TadoXServerError(503), (TadoXTransientError,)),
        (TadoXRateLimitError(None, None, local=True), (TadoXTransientError,)),
        (TadoXForbiddenError(403, "accessDenied"), (TadoXRequestError,)),
        (TadoXNotFoundError(404), (TadoXRequestError,)),
        (TadoXHomeNotXError(1000001), (TadoXNotFoundError, TadoXRequestError)),
        (TadoXParseError("rooms", "$.id"), (TadoXError,)),
        (TadoXDeviceFlowError("expired"), (TadoXError,)),
    ],
)
def test_taxonomy(error: TadoXError, parents: tuple[type[TadoXError], ...]) -> None:
    """Auth, transient and request errors are separate branches."""
    assert all(isinstance(error, parent) for parent in parents)
    assert isinstance(error, TadoXError)
    transient = isinstance(error, TadoXTransientError)
    assert not (transient and isinstance(error, TadoXAuthError))
    assert not (transient and isinstance(error, TadoXRequestError))


def test_error_fields() -> None:
    """Every error keeps the data its handler needs."""
    assert TadoXAuthError("invalid_grant").reason == "invalid_grant"
    assert TadoXServerError(503).status == 503

    info = RateLimitInfo(
        limit=20000,
        window=timedelta(days=1),
        remaining=0,
        reset_in=timedelta(seconds=7082),
        observed_at=NOW,
    )
    limited = TadoXRateLimitError(info, info.reset_at, local=False)
    assert limited.info is info
    assert limited.reset_at == NOW + timedelta(seconds=7082)
    assert not limited.local
    assert "tado API" in str(limited)
    assert "local quota gate" in str(TadoXRateLimitError(None, None, local=True))

    rejected = TadoXRequestError(422, "invalid", "Temperature out of range")
    assert (rejected.status, rejected.code, rejected.title) == (
        422,
        "invalid",
        "Temperature out of range",
    )
    assert "422" in str(rejected)

    not_x = TadoXHomeNotXError(
        1000001, "not.found", "Timezone of home 1000001 not found"
    )
    assert not_x.home_id == 1000001
    assert not_x.status == 404
    assert not_x.title == "Timezone of home 1000001 not found"

    parse = TadoXParseError("rooms", "$[2].setting")
    assert (parse.endpoint, parse.path) == ("rooms", "$[2].setting")
    assert TadoXDeviceFlowError("denied").reason == "denied"

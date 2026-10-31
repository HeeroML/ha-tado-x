"""No secret reaches the log, even at DEBUG (design §10).

The fake cloud hands out tokens and codes with known prefixes. Each test runs
a part of the integration with every logger at DEBUG and then searches the
captured log for those prefixes and the other values the logging policy
forbids.
"""

from collections.abc import Iterator
from datetime import timedelta
import logging

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.tado_x.const import DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from tests.tado_mock import TadoCloudMock, load_fixture
from tests.test_init import make_entry, setup_entry

TOKEN_PATH = "/oauth2/token"
EXPIRED_ISO = "2026-01-01T00:00:00+00:00"
POLL_INTERVAL = timedelta(seconds=5)
DEVICE_AUTHORIZATION = load_fixture("device_authorize.json")
ME = load_fixture("me.json")

SECRETS = (
    "fake-access-token",
    "fake-refresh-token",
    "fake.jwt",
    DEVICE_AUTHORIZATION["device_code"],
    DEVICE_AUTHORIZATION["user_code"],
    "user_code=",
    "Bearer ",
    ME["email"],
    ME["name"],
)
LOGIN_SECRET = "fake-refresh-token-in-a-login-body"
# The test harness's mock Store logs every payload it writes; Home Assistant's
# real Store logs only the key and the file path.
TEST_STORAGE_LOGGER = "pytest_homeassistant_custom_component.common"


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Return every record of the setup and call phases, with tracebacks."""
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(
        formatter.format(record)
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if record.name != TEST_STORAGE_LOGGER
    )


@pytest.fixture(autouse=True)
def debug_logging(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Capture every logger at DEBUG and check the log after the test."""
    caplog.set_level(logging.DEBUG)
    yield
    text = _log_text(caplog)
    assert [secret for secret in SECRETS if secret in text] == []


def _assert_requests_were_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The DEBUG request log ran, so the secret check above is not vacuous."""
    assert "GET hops:/homes/{home}/rooms -> 200 in " in caplog.text


async def test_sign_in_and_setup(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The device-code sign-in and the setup of the new entry log no secret."""
    mock_tado.device_flow_pending = 1
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    for login_finishes in (False, True):
        freezer.tick(POLL_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=login_finishes)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].state is ConfigEntryState.LOADED
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 2
    _assert_requests_were_logged(caplog)


async def test_rotation_and_401_cycle(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rotation at setup, a 401 with refresh and retry, and a failed refresh."""
    entry = make_entry(token_expiry=EXPIRED_ISO)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    rooms = entry.runtime_data.rooms

    mock_tado.revoke_access_tokens()
    freezer.tick(timedelta(minutes=1))
    await rooms.async_refresh()
    assert rooms.last_update_success
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 2
    assert [call.url.path for call in mock_tado.calls("GET")][-2:] == [
        "/homes/1000002/rooms",
        "/homes/1000002/rooms",
    ]

    freezer.tick(timedelta(minutes=10))
    mock_tado.fail("POST", TOKEN_PATH, exc=ClientConnectionError(), times=1)
    await rooms.async_refresh()
    assert not rooms.last_update_success
    assert "Refreshing the tado access token failed" in caplog.text

    freezer.tick(timedelta(seconds=10))
    await rooms.async_refresh()
    assert rooms.last_update_success
    _assert_requests_were_logged(caplog)


async def test_rejected_refresh_logs_no_login_body(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An invalid_grant answer is logged by its error code, never by its body."""
    entry = make_entry()
    await setup_entry(hass, entry)
    freezer.tick(timedelta(minutes=10))
    mock_tado.fail(
        "POST",
        TOKEN_PATH,
        status=400,
        body={"error": "invalid_grant", "error_description": LOGIN_SECRET},
    )

    await entry.runtime_data.rooms.async_refresh()
    await hass.async_block_till_done()

    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert "tado rejected the login (invalid_grant)" in caplog.text
    assert LOGIN_SECRET not in caplog.text

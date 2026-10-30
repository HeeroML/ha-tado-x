"""Tests of the tado X config flow and options flow."""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import probatio
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from yarl import URL

from custom_components.tado_x.client import CLIENT_ID
from custom_components.tado_x.config_flow import OPTIONS_SCHEMA
from custom_components.tado_x.const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    OPT_DAILY_CALL_BUDGET,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import config_validation as cv
from tests.tado_mock import HOME_A, HOME_B, HOME_C, USER_ID, TadoCloudMock

STRINGS = json.loads(
    (
        Path(__file__).parent.parent / "custom_components" / DOMAIN / "strings.json"
    ).read_text(encoding="utf-8")
)
POLL_INTERVAL = timedelta(seconds=5)
CODE_LIFETIME = timedelta(seconds=300)
OTHER_USER_ID = "00000000-0000-4000-8000-000000000002"

OPTIONS_INPUT: dict[str, dict[str, Any]] = {
    "polling": {"daily_call_budget": 2000.0, "quota_reserve": 500.0},
    "features": {
        "weather": False,
        "air_comfort": True,
        "running_times": True,
        "mobile_devices": True,
        "holiday_mode": False,
    },
    "control": {
        "manual_termination": "timer",
        "timer_minutes": 45.0,
        "off_follows_policy": True,
        "presence_zone": "zone.haus_b",
    },
}
OPTIONS_STORED = {
    "daily_call_budget": 2000,
    "quota_reserve": 500,
    "weather": False,
    "air_comfort": True,
    "running_times": True,
    "mobile_devices": True,
    "holiday_mode": False,
    "manual_termination": "timer",
    "timer_minutes": 45,
    "off_follows_policy": True,
    "presence_zone": "zone.haus_b",
}


@pytest.fixture(autouse=True)
async def finish_scheduled_reloads(hass: HomeAssistant) -> AsyncGenerator[None]:
    """Let the reload a finished flow schedules complete before hass stops."""
    yield
    await hass.async_block_till_done()


def _assert_serializes(schema: probatio.Schema | None) -> probatio.Schema:
    """Assert that the frontend can render a form schema (#73)."""
    assert schema is not None
    assert probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)
    return schema


def _abort(result: ConfigFlowResult, reason: str) -> None:
    """Assert an abort with a reason that strings.json defines."""
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert reason in STRINGS["config"]["abort"]


async def _start(hass: HomeAssistant) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def _poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, finished: bool = True
) -> None:
    """Let the device-code login poll the token endpoint once."""
    freezer.tick(POLL_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=finished)


async def _expire(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the device code expire."""
    freezer.tick(CODE_LIFETIME)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _sign_in(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, flow_id: str
) -> ConfigFlowResult:
    """Confirm the login in tado and continue the flow."""
    await _poll(hass, freezer)
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_user_flow_adds_the_x_home(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Home A (V3+) is skipped, home B becomes the entry, with three API calls (#40)."""
    result = await _start(hass)

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "user"
    assert result["progress_action"] == "wait_for_device"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["code"] == "ABC123"
    assert URL(placeholders["url"]).query == {
        "user_code": "ABC123",
        "client_id": CLIENT_ID,
    }

    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Haus B"
    assert result["result"].unique_id == str(HOME_B)
    data = result["data"]
    assert data[CONF_HOME_ID] == HOME_B
    assert data[CONF_HOME_NAME] == "Haus B"
    assert data[CONF_USER_ID] == USER_ID
    assert data[CONF_ACCESS_TOKEN] in mock_tado.valid_access_tokens
    assert data[CONF_REFRESH_TOKEN] in mock_tado.valid_refresh_tokens
    expiry = datetime.fromisoformat(data[CONF_TOKEN_EXPIRY])
    assert expiry.utcoffset() == timedelta(0)
    assert result["options"] == dict(DEFAULT_OPTIONS)
    # The new entry is set up right after the flow; its requests come later.
    flow_calls = [call.url.path for call in mock_tado.calls("GET")][:3]
    assert flow_calls == [
        "/api/v2/me",
        f"/api/v2/homes/{HOME_A}",
        f"/api/v2/homes/{HOME_B}",
    ]


async def test_two_x_homes_ask_which_one(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With two tado X homes the user picks one; the V3+ home is named as skipped."""
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    result = await _start(hass)
    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_home"
    assert result["description_placeholders"] == {"hidden": "Haus A"}
    schema = _assert_serializes(result["data_schema"])
    field = schema.schema[CONF_HOME_ID]
    assert [option["value"] for option in field.config["options"]] == [
        str(HOME_B),
        str(HOME_C),
    ]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOME_ID: str(HOME_C)}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Haus C"
    assert result["data"][CONF_HOME_ID] == HOME_C


async def test_configured_home_is_not_offered(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """With home B configured, the only other tado X home is added directly."""
    config_entry.add_to_hass(hass)
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]

    result = await _start(hass)
    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOME_ID] == HOME_C


async def test_already_configured(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """Every tado X home of the account is configured already."""
    config_entry.add_to_hass(hass)

    result = await _start(hass)
    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "already_configured")


@pytest.mark.parametrize(
    ("home_ids", "hidden"), [([HOME_A], "Haus A"), ([], "–")], ids=["v3", "none"]
)
async def test_no_tado_x_homes(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    home_ids: list[int],
    hidden: str,
) -> None:
    """An account without a tado X home aborts and names the skipped homes."""
    mock_tado.account_home_ids = home_ids

    result = await _start(hass)
    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "no_tado_x_homes")
    assert result["description_placeholders"] == {"hidden": hidden}
    assert len(mock_tado.calls("GET")) == 1 + len(home_ids)


async def test_pending_login_keeps_waiting(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The flow waits while tado reports the login as pending."""
    mock_tado.device_flow_pending = 2
    result = await _start(hass)

    await _poll(hass, freezer, finished=False)
    await _poll(hass, freezer, finished=False)
    progress = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert progress["type"] is FlowResultType.SHOW_PROGRESS

    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(mock_tado.calls("POST", "/oauth2/token")) == 3


async def test_expired_code_offers_a_new_one(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An expired code shows the timeout step, which starts over with a new code."""
    result = await _start(hass)
    await _expire(hass, freezer)

    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "timeout"
    assert result["data_schema"] is None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert len(mock_tado.calls("POST", "/oauth2/device_authorize")) == 2
    assert mock_tado.calls("POST", "/oauth2/token") == []

    result = await _sign_in(hass, freezer, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_access_denied(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A login declined in tado aborts."""
    mock_tado.device_flow_error = "access_denied"
    result = await _start(hass)

    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "access_denied")


async def test_closing_the_flow_stops_the_login(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Closing the dialog cancels the background login; tado is not polled again."""
    result = await _start(hass)

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    await _poll(hass, freezer)

    assert mock_tado.calls("POST", "/oauth2/token") == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ({"status": 429}, "rate_limited"),
        ({"status": 500}, "cannot_connect"),
        ({"status": 400, "body": {"error": "invalid_client"}}, "cannot_connect"),
        ({"exc": ClientConnectionError()}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"status": 200, "body": {"user_code": "ABC123"}}, "unknown"),
        ({"exc": RuntimeError("boom")}, "unknown"),
    ],
    ids=["429", "500", "400", "network", "timeout", "malformed", "unexpected"],
)
async def test_device_authorize_errors_abort(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    failure: dict[str, Any],
    reason: str,
) -> None:
    """Every failure to get a code aborts the flow (#73)."""
    mock_tado.fail("POST", "/oauth2/device_authorize", **failure)

    result = await _start(hass)

    _abort(result, reason)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ({"status": 429}, "rate_limited"),
        ({"status": 503}, "cannot_connect"),
        ({"exc": ClientConnectionError()}, "cannot_connect"),
    ],
    ids=["429", "503", "network"],
)
async def test_login_failing_until_expiry_aborts(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    failure: dict[str, Any],
    reason: str,
) -> None:
    """A token endpoint that fails until the code expires aborts with the last error."""
    mock_tado.fail("POST", "/oauth2/token", **failure)
    result = await _start(hass)
    await _poll(hass, freezer, finished=False)
    await _expire(hass, freezer)

    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    _abort(result, reason)


@pytest.mark.parametrize(
    ("failure", "log"),
    [
        (
            {"status": 400, "body": {"error": "invalid_client"}},
            "Signing in to tado failed: invalid_client",
        ),
        ({"exc": RuntimeError("boom")}, "Unexpected error while signing in to tado"),
    ],
    ids=["oauth_error", "unexpected"],
)
async def test_login_errors_abort(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    failure: dict[str, Any],
    log: str,
) -> None:
    """A definitive login error aborts as unknown and says why in the log."""
    mock_tado.fail("POST", "/oauth2/token", **failure)
    result = await _start(hass)

    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "unknown")
    assert log in caplog.text


ACCOUNT_FAILURES = [
    ({"status": 429}, "rate_limited"),
    ({"exc": TimeoutError()}, "cannot_connect"),
    ({"exc": ClientConnectionError()}, "cannot_connect"),
    ({"status": 500}, "cannot_connect"),
    ({"status": 403}, "cannot_connect"),
    ({"status": 200, "body": "not an object"}, "unknown"),
    ({"status": 200, "body": {"homes": [], "name": "x"}}, "unknown"),
    ({"exc": RuntimeError("boom")}, "unknown"),
]
ACCOUNT_FAILURE_IDS = [
    "429",
    "timeout",
    "network",
    "500",
    "403",
    "malformed",
    "missing_key",
    "unexpected",
]


@pytest.mark.parametrize("path", ["/api/v2/me", f"/api/v2/homes/{HOME_B}"])
@pytest.mark.parametrize(
    ("failure", "reason"), ACCOUNT_FAILURES, ids=ACCOUNT_FAILURE_IDS
)
async def test_account_errors_abort(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    path: str,
    failure: dict[str, Any],
    reason: str,
) -> None:
    """Any failure of /me or homes/{id} aborts cleanly, never raises (#73)."""
    mock_tado.fail("GET", path, **failure)
    result = await _start(hass)

    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, reason)


async def test_rotated_token_is_stored(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A token refreshed while listing the homes is the one the entry stores."""
    result = await _start(hass)
    await _poll(hass, freezer)
    mock_tado.revoke_access_tokens()

    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCESS_TOKEN] == "fake-access-token-0002"
    assert result["data"][CONF_REFRESH_TOKEN] == "fake-refresh-token-0002"


async def test_reauth_with_the_same_account(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth stores the new tokens and reads no home details."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_ACCESS_TOKEN] == "fake-access-token-0001"
    assert config_entry.data[CONF_REFRESH_TOKEN] == "fake-refresh-token-0001"
    assert config_entry.data[CONF_USER_ID] == USER_ID
    assert len(mock_tado.calls("GET", "/api/v2/me")) == 1
    assert mock_tado.calls("GET", f"/api/v2/homes/{HOME_A}") == []


async def test_reauth_with_another_account(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """Signing in with another tado account than before aborts; nothing changes."""
    config_entry.add_to_hass(hass)
    data = dict(config_entry.data)
    mock_tado.user_id = OTHER_USER_ID
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "reauth_account_mismatch")
    assert dict(config_entry.data) == data


@pytest.mark.parametrize(
    ("home_ids", "reason"),
    [([HOME_A, HOME_B], "reauth_successful"), ([HOME_A], "reauth_account_mismatch")],
    ids=["home_present", "home_missing"],
)
async def test_reauth_of_migrated_entry_checks_the_home(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
    home_ids: list[int],
    reason: str,
) -> None:
    """Without a stored user id, the account must have the home of the entry."""
    config_entry.add_to_hass(hass)
    data = dict(config_entry.data)
    del data[CONF_USER_ID]
    hass.config_entries.async_update_entry(config_entry, data=data)
    mock_tado.account_home_ids = home_ids
    mock_tado.user_id = OTHER_USER_ID
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    if reason == "reauth_successful":
        assert config_entry.data[CONF_USER_ID] == OTHER_USER_ID
    else:
        assert CONF_USER_ID not in config_entry.data


async def test_reconfigure_switches_the_account(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfigure moves the home to another account and reads only its details."""
    config_entry.add_to_hass(hass)
    mock_tado.user_id = OTHER_USER_ID
    mock_tado.account_home_ids = [HOME_A, HOME_B, HOME_C]
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    result = await _sign_in(hass, freezer, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_USER_ID] == OTHER_USER_ID
    assert config_entry.data[CONF_ACCESS_TOKEN] == "fake-access-token-0001"
    assert config_entry.data[CONF_HOME_ID] == HOME_B
    assert config_entry.unique_id == str(HOME_B)
    assert len(mock_tado.calls("GET", "/api/v2/me")) == 1
    assert mock_tado.calls("GET", f"/api/v2/homes/{HOME_A}") == []
    assert mock_tado.calls("GET", f"/api/v2/homes/{HOME_C}") == []


@pytest.mark.parametrize("generation", [None, "PRE_LINE_X"], ids=["absent", "not_x"])
async def test_reconfigure_without_the_home(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
    generation: str | None,
) -> None:
    """The new account must have this home as a tado X home."""
    config_entry.add_to_hass(hass)
    data = dict(config_entry.data)
    if generation is None:
        mock_tado.account_home_ids = [HOME_A]
    else:
        mock_tado.home(HOME_B).info["generation"] = generation
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    result = await _sign_in(hass, freezer, result["flow_id"])

    _abort(result, "home_not_found")
    assert dict(config_entry.data) == data


async def test_reconfigure_unique_id_mismatch(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """An entry whose unique_id is not its home cannot be reconfigured."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, unique_id="1000009")

    result = await config_entry.start_reconfigure_flow(hass)

    _abort(result, "unique_id_mismatch")
    assert mock_tado.calls() == []


async def test_reconfigure_an_entry_without_unique_id(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
) -> None:
    """A duplicate migrated from 1.8 has no unique_id; its home id is checked instead."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, unique_id=None)
    mock_tado.user_id = OTHER_USER_ID

    result = await config_entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _sign_in(hass, freezer, result["flow_id"])
    _abort(result, "reconfigure_successful")
    assert config_entry.data[CONF_USER_ID] == OTHER_USER_ID
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.unique_id == str(HOME_B)


def test_options_schema_serializes() -> None:
    """The options form only uses serializable selectors (#73)."""
    fields = probatio.to_field_list(
        OPTIONS_SCHEMA, custom_serializer=cv.custom_serializer
    )

    assert isinstance(fields, list)
    assert [field["name"] for field in fields] == ["polling", "features", "control"]


async def test_options_of_an_entry_that_is_not_loaded(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
) -> None:
    """Without a loaded entry there is no plan to show; options are stored flat."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["description_placeholders"] == {
        "rooms_interval": "–",
        "calls_per_day": "–",
        "limit": "–",
        "remaining": "–",
    }
    schema = _assert_serializes(result["data_schema"])
    suggested = {
        marker.schema: marker.description["suggested_value"]
        for section in schema.schema.values()
        for marker in section.schema.schema
    }
    assert suggested == dict(DEFAULT_OPTIONS)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], OPTIONS_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == OPTIONS_STORED
    assert isinstance(config_entry.options[OPT_DAILY_CALL_BUDGET], int)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("polling", "daily_call_budget", 99),
        ("polling", "daily_call_budget", 20001),
        ("polling", "quota_reserve", 10001),
        ("control", "timer_minutes", 4),
        ("control", "manual_termination", "forever"),
        ("control", "presence_zone", "person.someone"),
    ],
)
async def test_options_out_of_range_are_rejected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    section: str,
    key: str,
    value: object,
) -> None:
    """Values outside the limits never reach the entry."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    user_input = {name: dict(values) for name, values in OPTIONS_INPUT.items()}
    user_input[section][key] = value

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], user_input)

    assert config_entry.options == dict(DEFAULT_OPTIONS)


async def test_options_keep_unknown_keys(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Keys the form does not show stay in the options."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "future": 1}
    )
    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    await hass.config_entries.options.async_configure(result["flow_id"], OPTIONS_INPUT)

    assert config_entry.options == {**OPTIONS_STORED, "future": 1}


async def test_options_show_the_plan_and_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A loaded entry shows its polling plan and quota, and reloads on a change."""
    entry = setup_integration
    runtime_data = entry.runtime_data
    info = runtime_data.monitor.info
    assert info is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"] == {
        "rooms_interval": "45 s",
        "calls_per_day": "2521",
        "limit": "20000",
        "remaining": str(info.remaining),
    }

    await hass.config_entries.options.async_configure(result["flow_id"], OPTIONS_INPUT)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_data


async def test_options_show_long_room_intervals_in_minutes(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A small budget stretches the room interval to minutes."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, OPT_DAILY_CALL_BUDGET: 500},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["rooms_interval"].endswith(" min")

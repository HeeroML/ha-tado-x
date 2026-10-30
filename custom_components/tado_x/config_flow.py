"""Config flow of the tado X integration.

Sign-in uses tado's device-code flow in a progress step, so no HTTP request
of the frontend waits for the user. Only tado X (LINE_X) homes are offered.
"""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus
from typing import Any, Final, override

import probatio

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import (
    Account,
    DeviceAuthorization,
    DeviceCodeFlow,
    HomeInfo,
    MemoryTokenStore,
    QuotaTracker,
    TadoXClient,
    TadoXDeviceFlowError,
    TadoXError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    TadoXTransientError,
    TokenManager,
    TokenSet,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    MAX_DAILY_CALL_BUDGET,
    MAX_QUOTA_RESERVE,
    MAX_TIMER_MINUTES,
    MIN_DAILY_CALL_BUDGET,
    MIN_QUOTA_RESERVE,
    MIN_TIMER_MINUTES,
    OPT_AIR_COMFORT,
    OPT_DAILY_CALL_BUDGET,
    OPT_HOLIDAY_MODE,
    OPT_MANUAL_TERMINATION,
    OPT_MOBILE_DEVICES,
    OPT_OFF_FOLLOWS_POLICY,
    OPT_PRESENCE_ZONE,
    OPT_QUOTA_RESERVE,
    OPT_RUNNING_TIMES,
    OPT_TIMER_MINUTES,
    OPT_WEATHER,
)
from .runtime import TadoXConfigEntry
from .termination import TerminationPolicy

_NO_VALUE: Final = "–"

SECTION_POLLING: Final = "polling"
SECTION_FEATURES: Final = "features"
SECTION_CONTROL: Final = "control"
OPTION_SECTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    SECTION_POLLING: (OPT_DAILY_CALL_BUDGET, OPT_QUOTA_RESERVE),
    SECTION_FEATURES: (
        OPT_WEATHER,
        OPT_AIR_COMFORT,
        OPT_RUNNING_TIMES,
        OPT_MOBILE_DEVICES,
        OPT_HOLIDAY_MODE,
    ),
    SECTION_CONTROL: (
        OPT_MANUAL_TERMINATION,
        OPT_TIMER_MINUTES,
        OPT_OFF_FOLLOWS_POLICY,
        OPT_PRESENCE_ZONE,
    ),
}
_INTEGER_OPTIONS: Final = (OPT_DAILY_CALL_BUDGET, OPT_QUOTA_RESERVE, OPT_TIMER_MINUTES)


def _number(
    minimum: int, maximum: int, step: int, unit: str | None = None
) -> NumberSelector:
    config = NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


OPTIONS_SCHEMA: Final = probatio.Schema(
    {
        probatio.Required(SECTION_POLLING): section(
            probatio.Schema(
                {
                    probatio.Required(OPT_DAILY_CALL_BUDGET): _number(
                        MIN_DAILY_CALL_BUDGET, MAX_DAILY_CALL_BUDGET, 100
                    ),
                    probatio.Required(OPT_QUOTA_RESERVE): _number(
                        MIN_QUOTA_RESERVE, MAX_QUOTA_RESERVE, 100
                    ),
                }
            ),
            {"collapsed": True},
        ),
        probatio.Required(SECTION_FEATURES): section(
            probatio.Schema(
                {
                    probatio.Required(key): BooleanSelector()
                    for key in OPTION_SECTIONS[SECTION_FEATURES]
                }
            )
        ),
        probatio.Required(SECTION_CONTROL): section(
            probatio.Schema(
                {
                    probatio.Required(OPT_MANUAL_TERMINATION): SelectSelector(
                        SelectSelectorConfig(
                            options=[policy.value for policy in TerminationPolicy],
                            translation_key=OPT_MANUAL_TERMINATION,
                        )
                    ),
                    probatio.Required(OPT_TIMER_MINUTES): _number(
                        MIN_TIMER_MINUTES, MAX_TIMER_MINUTES, 1, "min"
                    ),
                    probatio.Required(OPT_OFF_FOLLOWS_POLICY): BooleanSelector(),
                    probatio.Required(OPT_PRESENCE_ZONE): EntitySelector(
                        EntitySelectorConfig(domain="zone")
                    ),
                }
            )
        ),
    }
)


def _abort_reason(err: TadoXError) -> str:
    """Return the abort reason for an error of the sign-in or the home listing."""
    if isinstance(err, TadoXDeviceFlowError) and err.reason == "denied":
        return "access_denied"
    if isinstance(err, TadoXRateLimitError) or (
        isinstance(err, TadoXServerError) and err.status == HTTPStatus.TOO_MANY_REQUESTS
    ):
        return "rate_limited"
    if isinstance(err, TadoXTransientError | TadoXRequestError):
        return "cannot_connect"
    LOGGER.warning("Signing in to tado failed: %s", err)
    return "unknown"


@contextmanager
def _abort_on_error() -> Iterator[None]:
    """Turn every failure of a tado request into an abort, never an HTTP 500 (#73)."""
    try:
        yield
    except TadoXError as err:
        raise AbortFlow(_abort_reason(err)) from err
    except Exception as err:
        LOGGER.exception("Unexpected error while signing in to tado")
        raise AbortFlow("unknown") from err


def _names(homes: list[HomeInfo]) -> str:
    return ", ".join(home.name for home in homes) or _NO_VALUE


class TadoXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add, reauthenticate and reconfigure a tado X home."""

    VERSION = 1
    MINOR_VERSION = 2

    _device_flow: DeviceCodeFlow | None
    _auth: DeviceAuthorization | None
    _login_task: asyncio.Task[TokenSet] | None
    _tokens: TokenSet | None
    _account: Account | None
    _x_homes: list[HomeInfo]
    _hidden: list[HomeInfo]

    def __init__(self) -> None:
        """Initialize an empty sign-in."""
        super().__init__()
        self._device_flow = None
        self._auth = None
        self._login_task = None
        self._tokens = None
        self._account = None
        self._x_homes = []
        self._hidden = []

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the device code and wait in the background for the sign-in."""
        if self._login_task is None or self._auth is None:
            self._device_flow = DeviceCodeFlow(async_get_clientsession(self.hass))
            with _abort_on_error():
                self._auth = await self._device_flow.async_start()
            self._login_task = self.hass.async_create_background_task(
                self._device_flow.async_wait_for_token(self._auth),
                "tado_x device code login",
            )
        if self._login_task.done():
            error = self._login_task.exception()
            if isinstance(error, TadoXDeviceFlowError) and error.reason == "expired":
                return self.async_show_progress_done(next_step_id="timeout")
            return self.async_show_progress_done(next_step_id="finish_login")
        return self.async_show_progress(
            step_id="user",
            progress_action="wait_for_device",
            description_placeholders={
                "url": self._auth.verification_url,
                "code": self._auth.user_code,
            },
            progress_task=self._login_task,
        )

    async def async_step_finish_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the tado X homes of the account and continue per flow source."""
        assert self._login_task is not None
        with _abort_on_error():
            await self._async_read_account(self._login_task.result())
        if self.source == SOURCE_REAUTH:
            return self._async_finish_reauth()
        if self.source == SOURCE_RECONFIGURE:
            return self._async_finish_reconfigure()
        return await self._async_finish_user()

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of several tado X homes."""
        if user_input is not None:
            home = next(
                home
                for home in self._x_homes
                if str(home.id) == user_input[CONF_HOME_ID]
            )
            return await self._async_create_home_entry(home)
        options = [
            SelectOptionDict(value=str(home.id), label=home.name)
            for home in self._x_homes
        ]
        return self.async_show_form(
            step_id="select_home",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_HOME_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={"hidden": _names(self._hidden)},
        )

    async def async_step_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a new code after the previous one expired."""
        if user_input is None:
            return self.async_show_form(step_id="timeout")
        self._device_flow = None
        self._auth = None
        self._login_task = None
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the reauthentication, then sign in again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start switching the tado account used for this home."""
        entry = self._get_reconfigure_entry()
        await self.async_set_unique_id(str(entry.data[CONF_HOME_ID]))
        self._abort_if_unique_id_mismatch()
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the account switch, then sign in with the other account."""
        if user_input is None:
            return self.async_show_form(step_id="reconfigure_confirm")
        return await self.async_step_user()

    @override
    @callback
    def async_remove(self) -> None:
        """Cancel the sign-in task when the dialog closes."""
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()

    @staticmethod
    @override
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TadoXOptionsFlow:
        """Return the options flow."""
        return TadoXOptionsFlow()

    async def _async_read_account(self, tokens: TokenSet) -> None:
        """Read the account and the details of the homes this flow needs.

        The client lives only for these requests; a token it rotates meanwhile
        is what the entry stores.
        """
        session = async_get_clientsession(self.hass)
        store = MemoryTokenStore(tokens)
        client = TadoXClient(session, TokenManager(session, store), QuotaTracker())
        try:
            account = await client.async_get_account()
            homes = await client.async_list_homes(self._homes_to_read(account))
        finally:
            await client.async_close()
        self._tokens = store.load()
        self._account = account
        self._x_homes = [home for home in homes if home.is_tado_x]
        self._hidden = [home for home in homes if not home.is_tado_x]

    def _homes_to_read(self, account: Account) -> Account:
        """Limit the account to the homes whose details the flow source needs.

        A user flow needs every home, a reconfigure flow the home of the entry
        and a reauth flow none, which saves API calls.
        """
        if self.source == SOURCE_REAUTH:
            return replace(account, homes=())
        if self.source == SOURCE_RECONFIGURE:
            home_id = self._get_reconfigure_entry().data[CONF_HOME_ID]
            homes = tuple(home for home in account.homes if home.id == home_id)
            return replace(account, homes=homes)
        return account

    async def _async_finish_user(self) -> ConfigFlowResult:
        """Create the entry, or ask which home to add."""
        if not self._x_homes:
            return self.async_abort(
                reason="no_tado_x_homes",
                description_placeholders={"hidden": _names(self._hidden)},
            )
        configured = self._async_current_ids()
        self._x_homes = [
            home for home in self._x_homes if str(home.id) not in configured
        ]
        if not self._x_homes:
            return self.async_abort(reason="already_configured")
        if len(self._x_homes) == 1:
            return await self._async_create_home_entry(self._x_homes[0])
        return await self.async_step_select_home()

    async def _async_create_home_entry(self, home: HomeInfo) -> ConfigFlowResult:
        await self.async_set_unique_id(str(home.id))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=home.name,
            data={CONF_HOME_ID: home.id, CONF_HOME_NAME: home.name}
            | self._account_data(),
            options=dict(DEFAULT_OPTIONS),
        )

    @callback
    def _async_finish_reauth(self) -> ConfigFlowResult:
        """Store the new tokens if the account that has the home signed in."""
        entry = self._get_reauth_entry()
        assert self._account is not None
        if (user_id := entry.data.get(CONF_USER_ID)) is not None:
            same_account = user_id == self._account.user_id
        else:
            home_id = entry.data[CONF_HOME_ID]
            same_account = any(home.id == home_id for home in self._account.homes)
        if not same_account:
            return self.async_abort(reason="reauth_account_mismatch")
        return self.async_update_reload_and_abort(
            entry, data_updates=self._account_data()
        )

    @callback
    def _async_finish_reconfigure(self) -> ConfigFlowResult:
        """Switch the entry to the new account if it has this tado X home."""
        entry = self._get_reconfigure_entry()
        home_id = entry.data[CONF_HOME_ID]
        if not any(home.id == home_id for home in self._x_homes):
            return self.async_abort(reason="home_not_found")
        return self.async_update_reload_and_abort(
            entry, data_updates=self._account_data()
        )

    def _account_data(self) -> dict[str, Any]:
        """Return the account and token keys of the entry data."""
        assert self._account is not None
        assert self._tokens is not None
        return {
            CONF_USER_ID: self._account.user_id,
            CONF_ACCESS_TOKEN: self._tokens.access_token,
            CONF_REFRESH_TOKEN: self._tokens.refresh_token,
            CONF_TOKEN_EXPIRY: self._tokens.expires_at.isoformat(),
        }


def _format_period(period: timedelta) -> str:
    seconds = round(period.total_seconds())
    if seconds < 120:
        return f"{seconds} s"
    return f"{round(seconds / 60)} min"


def _format_count(count: int | None) -> str:
    return _NO_VALUE if count is None else str(count)


class TadoXOptionsFlow(OptionsFlowWithReload):
    """Options of a tado X entry: polling budget, optional data and control."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options; values are stored flat."""
        if user_input is not None:
            options = {
                key: value
                for values in user_input.values()
                for key, value in values.items()
            }
            for key in _INTEGER_OPTIONS:
                options[key] = int(options[key])
            return self.async_create_entry(
                data={**self.config_entry.options, **options}
            )
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    name: {key: current[key] for key in keys}
                    for name, keys in OPTION_SECTIONS.items()
                },
            ),
            description_placeholders=self._plan_placeholders(),
        )

    def _plan_placeholders(self) -> dict[str, str]:
        """Describe the polling plan and the quota of the loaded entry."""
        entry: TadoXConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return dict.fromkeys(
                ("rooms_interval", "calls_per_day", "limit", "remaining"), _NO_VALUE
            )
        monitor = entry.runtime_data.monitor
        plan, info = monitor.plan, monitor.info
        return {
            "rooms_interval": _format_period(plan.rooms),
            "calls_per_day": str(plan.calls_per_day),
            "limit": _format_count(None if info is None else info.limit),
            "remaining": _format_count(None if info is None else info.remaining),
        }

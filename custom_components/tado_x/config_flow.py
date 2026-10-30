"""Config flow of the tado X integration.

Sign-in uses tado's device-code flow in a progress step, so no HTTP request
of the frontend waits for the user. Only tado X (LINE_X) homes are offered.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback

from .client import Account, DeviceAuthorization, DeviceCodeFlow, HomeInfo, TokenSet
from .const import DOMAIN


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

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the device code and wait in the background for the sign-in."""
        raise NotImplementedError

    async def async_step_finish_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the tado X homes of the account and continue per flow source."""
        raise NotImplementedError

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of several tado X homes."""
        raise NotImplementedError

    async def async_step_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a new code after the previous one expired."""
        raise NotImplementedError

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauthentication."""
        raise NotImplementedError

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the reauthentication, then sign in again."""
        raise NotImplementedError

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start switching the tado account used for this home."""
        raise NotImplementedError

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the account switch, then sign in with the other account."""
        raise NotImplementedError

    @override
    @callback
    def async_remove(self) -> None:
        """Cancel the sign-in task when the dialog closes."""
        raise NotImplementedError

    @staticmethod
    @override
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TadoXOptionsFlow:
        """Return the options flow."""
        raise NotImplementedError


class TadoXOptionsFlow(OptionsFlowWithReload):
    """Options of a tado X entry: polling budget, optional data and control."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options; values are stored flat."""
        raise NotImplementedError

"""Credential sign in for an SPX account."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SPXApi, SPXAuthError, SPXApiError, jwt_claim
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _validate_account(hass, username: str, password: str) -> tuple[SPXApi, str | None]:
    """Check each cloud stage and return a safe, specific form error."""
    api = SPXApi(async_get_clientsession(hass))
    try:
        await api.login(username, password)
    except SPXAuthError:
        return api, "invalid_auth"
    except SPXApiError as exc:
        _LOGGER.warning("SPX Cognito connection failed: %s", exc)
        return api, "cognito_unreachable"

    try:
        await api.devices()
    except SPXAuthError:
        return api, "invalid_auth"
    except SPXApiError as exc:
        _LOGGER.warning("SPX device list failed: %s", exc)
        return api, "device_api_error"

    try:
        await api.mqtt_policy()
    except SPXAuthError:
        return api, "invalid_auth"
    except SPXApiError as exc:
        _LOGGER.warning("SPX MQTT credential exchange failed: %s", exc)
        return api, "mqtt_token_error"
    return api, None


class SPXConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Authenticate with the Cognito public app client."""

    VERSION = 1

    async def async_step_reauth(self, entry_data):
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            api, error = await _validate_account(
                self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                account_id = jwt_claim(api.access_token or "", "sub")
                if self._reauth_entry.unique_id and account_id != self._reauth_entry.unique_id:
                    errors["base"] = "wrong_account"
                else:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={"refresh_token": api.refresh_token},
                        title=user_input[CONF_USERNAME],
                    )
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            api, error = await _validate_account(
                self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                unique_id = jwt_claim(api.access_token or "", "sub")
                if unique_id:
                    await self.async_set_unique_id(str(unique_id))
                    self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data={"refresh_token": api.refresh_token},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

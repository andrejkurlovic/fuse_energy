"""Config flow for FUSE Energy."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DOMAIN

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_OTP_SCHEMA = vol.Schema({"otp": str})


class FuseEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._auth_flow_token: str = ""
        self._api: FuseEnergyAPI | None = None

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            session = async_get_clientsession(self.hass)
            self._api = FuseEnergyAPI(session)
            try:
                self._auth_flow_token = await self._api.initial_challenge(self._email)
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self.async_step_otp()

        return self.async_show_form(step_id="user", data_schema=_EMAIL_SCHEMA, errors=errors)

    async def async_step_otp(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._api is not None
            try:
                tokens = await self._api.otp_challenge(
                    user_input["otp"], self._auth_flow_token
                )
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._email,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    },
                )

        return self.async_show_form(step_id="otp", data_schema=_OTP_SCHEMA, errors=errors)

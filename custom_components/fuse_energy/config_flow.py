"""Config flow for FUSE Energy."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DOMAIN

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_OTP_SCHEMA = vol.Schema({"code": str})
_MAGIC_LINK_SCHEMA = vol.Schema({"magic_token": str})


class FuseEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._auth_flow_token: str = ""
        self._api: FuseEnergyAPI | None = None
        self._challenge_type: str = "otp"

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            session = async_get_clientsession(self.hass)
            self._api = FuseEnergyAPI(session)
            try:
                result = await self._api.initial_challenge(self._email)
                self._auth_flow_token = result["auth_flow_token"]
                self._challenge_type = result["challenge_type"]
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                if self._challenge_type == "magic_link":
                    return await self.async_step_magic_link()
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="user", data_schema=_EMAIL_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._api is not None
            try:
                tokens = await self._api.otp_challenge(
                    user_input["code"], self._auth_flow_token
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

        return self.async_show_form(
            step_id="otp", data_schema=_OTP_SCHEMA, errors=errors
        )

    async def async_step_magic_link(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._api is not None
            magic_token = user_input.get("magic_token", "").strip()
            if magic_token:
                try:
                    tokens = await self._api.magic_link_check(
                        magic_token, self._auth_flow_token
                    )
                    if tokens:
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
                    errors["base"] = "magic_link_pending"
                except FuseAuthError:
                    errors["base"] = "invalid_auth"
                except FuseError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    errors["base"] = "unknown"
            else:
                errors["base"] = "magic_link_pending"

        return self.async_show_form(
            step_id="magic_link",
            data_schema=_MAGIC_LINK_SCHEMA,
            errors=errors,
        )

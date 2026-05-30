"""Config flow for Fuse Energy+ — two-step: email → OTP.

Auth proven by APK: challenge_type=INITIAL (not "InitialChallenge"),
email_address field (not "email"), challenge_type=PHONE_OTP (not "OtpChallenge"),
code field (not "otp").
"""
from __future__ import annotations

import uuid

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_FLOW_TOKEN,
    CONF_DEVICE_ID,
    CONF_PREMISES_FID,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    DOMAIN,
)
from .coordinator import FuseEnergyCoordinator

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_OTP_SCHEMA = vol.Schema({vol.Required("code"): str})


class FuseEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fuse Energy+."""

    VERSION = 2

    def __init__(self) -> None:
        self._email: str = ""
        self._auth_flow_token: str = ""
        self._session_id: str = str(uuid.uuid4())
        self._device_id: str = str(uuid.uuid4())
        self._api: FuseEnergyAPI | None = None

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip().lower()
            session = async_get_clientsession(self.hass)
            self._api = FuseEnergyAPI(
                session,
                session_id=self._session_id,
                device_id=self._device_id,
            )
            try:
                result = await self._api.initial_challenge(self._email)
                self._auth_flow_token = result["auth_flow_token"]
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="user",
            data_schema=_EMAIL_SCHEMA,
            errors=errors,
        )

    async def async_step_otp(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._api is not None
            try:
                tokens = await self._api.otp_challenge(
                    user_input["code"].strip(), self._auth_flow_token
                )
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                # Discover the premises_fid for use in the coordinator
                premises_fid = ""
                try:
                    premises_list = await self._api.get_premises()
                    if premises_list:
                        first = premises_list[0]
                        prem_obj = first.get("premises") or first
                        premises_fid = (
                            prem_obj.get("fid")
                            or prem_obj.get("premises_fid")
                            or prem_obj.get("id")
                            or ""
                        )
                except FuseError:
                    pass  # Coordinator will handle this on first refresh

                await self.async_set_unique_id(self._email)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._email,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_SESSION_ID: self._session_id,
                        CONF_DEVICE_ID: self._device_id,
                        CONF_PREMISES_FID: premises_fid,
                    },
                )

        return self.async_show_form(
            step_id="otp",
            data_schema=_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def async_step_reauth(
        self, entry_data: dict
    ) -> ConfigFlowResult:
        """Re-authentication when stored token expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._email = entry.data.get(CONF_EMAIL, "")
        self._session_id = entry.data.get(CONF_SESSION_ID, str(uuid.uuid4()))
        self._device_id = entry.data.get(CONF_DEVICE_ID, str(uuid.uuid4()))
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            self._api = FuseEnergyAPI(session, self._session_id, self._device_id)
            try:
                result = await self._api.initial_challenge(self._email)
                self._auth_flow_token = result["auth_flow_token"]
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"email": self._email},
        )

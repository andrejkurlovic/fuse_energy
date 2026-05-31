"""Config flow for Fuse Energy+ — email → OTP or magic link.

Auth proven by APK decompilation (Android v2.0.65):
  challenge_type: "INITIAL" / "PHONE_OTP" / "MAGIC_LINK_CHECK"
  email field: data.data.email_address
  OTP field: data.code
  Magic link field: data.token  (AuthClientData.MagicLinkCheckChallenge @o(name="token"))
"""
from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_PREMISES_FID,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    DOMAIN,
)

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_OTP_SCHEMA = vol.Schema({vol.Required("code"): str})
_MAGIC_LINK_SCHEMA = vol.Schema({vol.Required("magic_link_url"): str})


def _extract_token_from_url(raw: str) -> str:
    """Extract 'token' query param from a URL, or return as-is if no URL structure."""
    raw = raw.strip()
    try:
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https") and parsed.query:
            params = parse_qs(parsed.query)
            if "token" in params:
                return params["token"][0]
    except Exception:
        pass
    return raw  # treat the whole input as the token itself


class FuseEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fuse Energy+."""

    VERSION = 2

    def __init__(self) -> None:
        self._email: str = ""
        self._auth_flow_token: str | None = None
        self._session_id: str = str(uuid.uuid4())
        self._device_id: str = str(uuid.uuid4())
        self._api: FuseEnergyAPI | None = None
        self._is_reauth: bool = False

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
                self._auth_flow_token = result.get("auth_flow_token")
                challenge_type = result.get("challenge_type", "PHONE_OTP")
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                if challenge_type == "MAGIC_LINK_CHECK":
                    return await self.async_step_magic_link()
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
                    user_input["code"].strip(), self._auth_flow_token or ""
                )
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self._finish_auth(tokens)

        return self.async_show_form(
            step_id="otp",
            data_schema=_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def async_step_magic_link(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Magic link step — user pastes the URL from their email link.

        The URL contains ?token=... which maps to AuthClientData.MagicLinkCheckChallenge.token
        (@o(name='token'), proven by APK decompilation).
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._api is not None
            raw = user_input.get("magic_link_url", "").strip()
            token = _extract_token_from_url(raw)
            if not token:
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await self._api.magic_link_challenge(
                        token, self._auth_flow_token
                    )
                except FuseAuthError:
                    errors["base"] = "invalid_auth"
                except FuseError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    errors["base"] = "unknown"
                else:
                    return await self._finish_auth(tokens)

        return self.async_show_form(
            step_id="magic_link",
            data_schema=_MAGIC_LINK_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def _finish_auth(self, tokens: dict[str, str]) -> ConfigFlowResult:
        """Fetch premises_fid then create or update the config entry."""
        assert self._api is not None
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
        except (FuseError, FuseAuthError):
            pass  # premises_fid optional — coordinator will fetch on first refresh

        if self._is_reauth:
            # Update only the auth tokens; preserve session/device IDs so the API
            # accepts the existing session without re-registration.
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_PREMISES_FID: premises_fid or entry.data.get(CONF_PREMISES_FID, ""),
                },
                reason="reauth_successful",
            )

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

    # ------------------------------------------------------------------
    # Re-auth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict) -> ConfigFlowResult:
        """HA triggers this when ConfigEntryAuthFailed is raised by the coordinator."""
        self._is_reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Show a single-click form, then send a new magic link / OTP to the account email."""
        entry = self._get_reauth_entry()
        # Preserve session/device IDs from the existing entry so the Fuse backend
        # recognises this as the same device session.
        self._email = entry.data.get(CONF_EMAIL, "")
        self._session_id = entry.data.get(CONF_SESSION_ID, str(uuid.uuid4()))
        self._device_id = entry.data.get(CONF_DEVICE_ID, str(uuid.uuid4()))
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            self._api = FuseEnergyAPI(session, self._session_id, self._device_id)
            try:
                result = await self._api.initial_challenge(self._email)
                self._auth_flow_token = result.get("auth_flow_token")
                challenge_type = result.get("challenge_type", "PHONE_OTP")
            except FuseAuthError:
                errors["base"] = "invalid_auth"
            except FuseError:
                errors["base"] = "cannot_connect"
            else:
                if challenge_type == "MAGIC_LINK_CHECK":
                    return await self.async_step_magic_link()
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"email": self._email},
        )

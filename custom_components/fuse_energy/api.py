"""Async API client for Fuse Energy — proven from live API testing.

Auth flow (APK + live verified):
  Step 1: POST api/v3/auth  challenge_type=INITIAL  data={method:EMAIL, data:{email_address:...}, auth_flow_type:LOGIN}
  Step 2: POST api/v3/auth  challenge_type=MAGIC_LINK_CHECK  data={token:<jwt_from_email_url>}
  Refresh: POST api/v1/auth/refresh  body={refresh_token:...}  + Authorization: Bearer <old_access_token>

Required headers (proven by live testing — Time-Zone is REQUIRED; without it server
returns an invalid UUID token instead of a JWT):
  User-Agent, Accept-Language:en-GB, Session-Id, X-Request-Id,
  Device-Model, Device-Id, Time-Zone
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import API_BASE_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)

_AUTH_URL = f"{API_BASE_URL}/api/v3/auth"
_REFRESH_URL = f"{API_BASE_URL}/api/v1/auth/refresh"


def _safe_url(url: str) -> str:
    return url.split("?")[0].split("://")[-1]


def _log_failure(
    operation: str,
    url: str,
    *,
    status: int | None = None,
    body: str | None = None,
    exc: BaseException | None = None,
) -> None:
    parts = [f"FuseEnergy API: op={operation}", f"url={_safe_url(url)}"]
    if status is not None:
        parts.append(f"status={status}")
    if exc is not None:
        parts.append(f"exc={type(exc).__name__}: {exc}")
    if body:
        parts.append(f"body={body[:500]}")
    _LOGGER.error(" | ".join(parts))


class FuseAuthError(Exception):
    """Raised when authentication fails or tokens are invalid."""


class FuseError(Exception):
    """Raised on API communication failures."""


class FuseEnergyAPI:
    """Async wrapper around the Fuse Energy private API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        session_id: str | None = None,
        device_id: str | None = None,
        on_token_refresh: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._session_id = session_id or str(uuid.uuid4())
        self._device_id = device_id or str(uuid.uuid4())
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        # Called after a successful silent token refresh so the caller can persist
        # the new tokens to the config entry (prevents daily re-auth on HA restart).
        self._on_token_refresh = on_token_refresh

    def set_tokens(self, access_token: str, refresh_token: str | None) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token

    def _base_headers(self) -> dict[str, str]:
        """Build required headers. Time-Zone is REQUIRED — without it the server
        returns an invalid UUID token instead of a valid JWT access_token."""
        return {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB",
            "Session-Id": self._session_id,
            "X-Request-Id": str(uuid.uuid4()),
            "Device-Model": "Home Assistant",
            "Device-Id": self._device_id,
            "Time-Zone": "Europe/London",
        }

    def _auth_headers(self) -> dict[str, str]:
        h = self._base_headers()
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        operation: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        h = self._base_headers()
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            async with self._session.post(url, headers=h, json=payload) as resp:
                body = await resp.text()
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = int(retry_after) if retry_after.isdigit() else 60
                    _LOGGER.warning(
                        "FuseEnergy: rate limited (429) on %s — Retry-After %ds",
                        _safe_url(url), wait,
                    )
                    raise FuseError(f"Rate limited (429) — retry after {wait}s")
                if resp.status >= 400:
                    _log_failure(operation, url, status=resp.status, body=body)
                    if resp.status == 401:
                        raise FuseAuthError(f"401 on {operation}")
                    raise FuseError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            _log_failure(operation, url, exc=exc)
            raise FuseError(str(exc)) from exc

    async def _get(
        self,
        url: str,
        *,
        operation: str,
        _retried: bool = False,
    ) -> Any:
        headers = self._auth_headers()
        try:
            async with self._session.get(url, headers=headers) as resp:
                body = await resp.text()
                if resp.status == 401 and not _retried and self._refresh_token:
                    if await self._refresh():
                        return await self._get(url, operation=operation, _retried=True)
                    raise FuseAuthError("Token refresh failed — re-authentication required")
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = int(retry_after) if retry_after.isdigit() else 60
                    _LOGGER.warning(
                        "FuseEnergy: rate limited (429) on %s — Retry-After %ds",
                        _safe_url(url), wait,
                    )
                    raise FuseError(f"Rate limited (429) — retry after {wait}s")
                if resp.status >= 400:
                    _log_failure(operation, url, status=resp.status, body=body)
                    if resp.status == 401:
                        raise FuseAuthError("Unauthorized — re-authentication required")
                    raise FuseError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            _log_failure(operation, url, exc=exc)
            raise FuseError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def initial_challenge(self, email: str) -> dict[str, Any]:
        """POST api/v3/auth — step 1: send email, get challenge type back.

        Returns dict with challenge_type and auth_flow_token (None for MAGIC_LINK_CHECK).
        """
        payload: dict[str, Any] = {
            "challenge_type": "INITIAL",
            "data": {
                "method": "EMAIL",
                "data": {"email_address": email},
                "auth_flow_type": "LOGIN",
            },
        }
        data = await self._post(_AUTH_URL, payload, operation="initial_challenge")
        challenge_type = data.get("challenge_type", "PHONE_OTP")
        return {
            "auth_flow_token": data.get("auth_flow_token"),
            "challenge_type": challenge_type,
        }

    async def magic_link_challenge(
        self, token: str, auth_flow_token: str | None
    ) -> dict[str, str]:
        """POST api/v3/auth — magic link step.

        token: full JWT from the magic link URL ?token= parameter.
        auth_flow_token: null for MAGIC_LINK_CHECK (server sends null in INITIAL response).
        Returns {access_token, refresh_token}.

        IMPORTANT: Time-Zone header must be present or server returns invalid UUID token.
        """
        payload: dict[str, Any] = {
            "challenge_type": "MAGIC_LINK_CHECK",
            "data": {"token": token},
        }
        if auth_flow_token:
            payload["auth_flow_token"] = auth_flow_token

        data = await self._post(_AUTH_URL, payload, operation="magic_link_challenge")
        inner = data.get("data") or {}
        access_token = inner.get("access_token") if isinstance(inner, dict) else None
        refresh_token = inner.get("refresh_token") if isinstance(inner, dict) else None

        if not access_token:
            _log_failure("magic_link_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No access_token in magic link response — link may be expired or already used")

        self._access_token = access_token
        self._refresh_token = refresh_token
        return {"access_token": access_token, "refresh_token": refresh_token or ""}

    async def otp_challenge(self, code: str, auth_flow_token: str) -> dict[str, str]:
        """POST api/v3/auth — OTP step (for accounts that use phone OTP)."""
        payload: dict[str, Any] = {
            "challenge_type": "PHONE_OTP",
            "auth_flow_token": auth_flow_token,
            "data": {"code": code},
        }
        data = await self._post(_AUTH_URL, payload, operation="otp_challenge")
        inner = data.get("data") or {}
        access_token = (inner.get("access_token") if isinstance(inner, dict) else None) or data.get("access_token")
        refresh_token = (inner.get("refresh_token") if isinstance(inner, dict) else None) or data.get("refresh_token")

        if not access_token:
            _log_failure("otp_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No access_token in OTP response")

        self._access_token = access_token
        self._refresh_token = refresh_token
        return {"access_token": access_token, "refresh_token": refresh_token or ""}

    async def _refresh(self) -> bool:
        """POST api/v1/auth/refresh — renew access token.

        IMPORTANT (proven by live testing): must send the OLD access_token as
        Authorization: Bearer while posting refresh_token in the body.
        Calling without the Bearer header returns 401 'missing access token'.

        On success, persists new tokens via the on_token_refresh callback so
        HA can store them in the config entry — prevents daily re-auth on restart.
        """
        if not self._refresh_token or not self._access_token:
            return False
        payload = {"refresh_token": self._refresh_token}
        try:
            data = await self._post(
                _REFRESH_URL, payload,
                operation="token_refresh",
                token=self._access_token,  # old token required in Bearer header
            )
            new_at = data.get("access_token") or (data.get("data") or {}).get("access_token")
            if not new_at:
                return False
            self._access_token = new_at
            new_rt = data.get("refresh_token") or (data.get("data") or {}).get("refresh_token")
            if new_rt:
                self._refresh_token = new_rt
            # Persist new tokens so HA doesn't lose them on restart
            if self._on_token_refresh is not None:
                try:
                    await self._on_token_refresh(new_at, self._refresh_token)
                except Exception:
                    pass  # callback failure must not abort the refresh
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Data endpoints (all confirmed from live API testing)
    # ------------------------------------------------------------------

    async def get_premises(self) -> list[dict[str, Any]]:
        """GET api/v2/customer/premises.

        Response structure (live verified):
          [{premises: {id, address: {street_line_1, city, postcode}, premises_name},
            supplies: [{supply_fid, supply_definition: {supply_type, identifier, names},
                        meter_type_and_status: {type, status, serial_number}, ...}],
            default_date_uk}]

        supply_type values: "ELEC_IMPORT", "GAS"  (NOT "ELECTRICITY_IMPORT")
        """
        url = f"{API_BASE_URL}/api/v2/customer/premises"
        result = await self._get(url, operation="get_premises")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []

    async def get_chart(
        self,
        premises_fid: str,
        year: int,
        month: int | None = None,
        day: int | None = None,
    ) -> dict[str, Any]:
        """GET api/v1/premises/{fid}/chart.

        Response structure (live verified):
          {current_index, supplies, total_bars, total_lines, total_realised_money, total_money}

          total_bars[]: {index:{year,month,day}, money:{amount:"5.73",currency:"GBP"},
                         kWh:"25.470" (string!), type:"REALISED"}

          supplies[]: {supply_fid, supply_type:"ELEC_IMPORT",
                       bars:[{bar:{index,money,kWh,type}, breakdown:[...]}]}
          Note: bars are nested under "bar" key inside each element.
        """
        params: list[str] = [f"year={year}"]
        if month is not None:
            params.append(f"month={month}")
        if day is not None:
            params.append(f"day={day}")
        url = f"{API_BASE_URL}/api/v1/premises/{premises_fid}/chart?{'&'.join(params)}"
        result = await self._get(url, operation="get_chart")
        return result if isinstance(result, dict) else {}

    async def get_current_contracts(
        self, premises_fid: str, supply_fid: str
    ) -> dict[str, Any]:
        """GET api/v5/contracts-current.

        Response structure (live verified):
          {supply_fid_to_contracts: {<supply_fid>: {current: {tariff:{tariff_id,title,...}}}}}
        """
        url = (
            f"{API_BASE_URL}/api/v5/contracts-current"
            f"?premises_fid={premises_fid}&supply_fid={supply_fid}"
        )
        result = await self._get(url, operation="get_current_contracts")
        return result if isinstance(result, dict) else {}

    async def get_tariff_details(
        self, supply_fid: str, tariff_id: str
    ) -> dict[str, Any]:
        """GET api/v1/tariff/details — unit rates and standing charges."""
        url = (
            f"{API_BASE_URL}/api/v1/tariff/details"
            f"?supply_fid={supply_fid}&tariff_ids={tariff_id}"
        )
        result = await self._get(url, operation="get_tariff_details")
        return result if isinstance(result, dict) else {}

    async def get_balance(self) -> dict[str, Any]:
        """GET api/v1/balance.

        Response (live verified): {amount: "0" (string), currency: "GBP"}
        """
        url = f"{API_BASE_URL}/api/v1/balance"
        result = await self._get(url, operation="get_balance")
        return result if isinstance(result, dict) else {}

    async def get_individual(self) -> dict[str, Any]:
        """GET api/v1/individual — name, email, phone."""
        url = f"{API_BASE_URL}/api/v1/individual"
        result = await self._get(url, operation="get_individual")
        return result if isinstance(result, dict) else {}

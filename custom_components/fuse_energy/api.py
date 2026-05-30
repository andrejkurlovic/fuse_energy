"""Async API client for Fuse Energy — rebuilt from APK evidence.

Auth flow proven by decompilation of Android app v2.0.65:
  Step 1: POST api/v3/auth  challenge_type=INITIAL  data.method=EMAIL  data.data.email_address=...
  Step 2: POST api/v3/auth  challenge_type=PHONE_OTP  auth_flow_token=...  data.code=...
  Refresh: POST api/v1/auth/refresh  refresh_token=...

Required headers proven by xk/h.java network interceptor (lines 142-186):
  User-Agent, Accept-Language:en-GB, Session-Id, X-Request-Id, Device-Model, Device-Id
"""
from __future__ import annotations

import logging
import uuid
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
    """Async wrapper around the Fuse Energy private API.

    session_id and device_id are persistent UUIDs generated once per integration
    setup and stored in the config entry. They are sent as Session-Id and Device-Id
    headers on every request (xk/h.java interceptor, lines 142-186).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        session_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        self._session = session
        self._session_id = session_id or str(uuid.uuid4())
        self._device_id = device_id or str(uuid.uuid4())
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    def set_tokens(self, access_token: str, refresh_token: str | None) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token

    def _base_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB",
            "Session-Id": self._session_id,
            "X-Request-Id": str(uuid.uuid4()),
            "Device-Model": "Home Assistant",
            "Device-Id": self._device_id,
        }

    def _auth_headers(self) -> dict[str, str]:
        h = self._base_headers()
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        operation: str,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        headers = self._auth_headers() if authenticated else self._base_headers()
        try:
            async with self._session.post(url, headers=headers, json=payload) as resp:
                body = await resp.text()
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
        """POST api/v3/auth — step 1: send email, get next challenge type.

        Payload proven by: ChallengeType.INITIAL, AuthClientData.InitialChallenge.Method.EMAIL,
        AuthClientData.InitialChallenge.Data.EmailData @o(name="email_address"), AuthFlowType.LOGIN.

        Returns dict with challenge_type and auth_flow_token (may be None for MAGIC_LINK_CHECK).
        Raises FuseAuthError only on HTTP 401. Other non-200 raises FuseError (cannot_connect).
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
        auth_flow_token = data.get("auth_flow_token")  # may be None for MAGIC_LINK_CHECK

        if challenge_type not in ("PHONE_OTP", "MAGIC_LINK_CHECK", "AUTHORIZED"):
            _LOGGER.warning(
                "FuseEnergy: unexpected challenge_type=%s after INITIAL", challenge_type
            )

        return {
            "auth_flow_token": auth_flow_token,
            "challenge_type": challenge_type,
        }

    async def magic_link_challenge(
        self, token: str, auth_flow_token: str | None
    ) -> dict[str, str]:
        """POST api/v3/auth — magic link step: submit token from email link.

        Payload proven by:
          ChallengeType.MAGIC_LINK_CHECK (ordinal 3)
          AuthClientData.MagicLinkCheckChallenge: @o(name="token") String token
        auth_flow_token is null for this flow (server returns null in INITIAL response).
        """
        payload: dict[str, Any] = {
            "challenge_type": "MAGIC_LINK_CHECK",
            "data": {"token": token},
        }
        if auth_flow_token:
            payload["auth_flow_token"] = auth_flow_token

        data = await self._post(_AUTH_URL, payload, operation="magic_link_challenge")
        _LOGGER.warning("FuseEnergy magic_link response keys: %s", list(data.keys()))

        inner: dict[str, Any] = {}
        raw_data = data.get("data")
        if isinstance(raw_data, dict):
            inner = raw_data
        access_token = inner.get("access_token") or data.get("access_token")
        refresh_token = inner.get("refresh_token") or data.get("refresh_token")

        if not access_token:
            _log_failure("magic_link_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No access_token in magic link challenge response")

        self._access_token = access_token
        self._refresh_token = refresh_token
        return {
            "access_token": access_token,
            "refresh_token": refresh_token or "",
        }

    async def otp_challenge(self, code: str, auth_flow_token: str) -> dict[str, str]:
        """POST api/v3/auth — step 2: submit OTP, receive access+refresh tokens.

        Payload shape proven by:
          ChallengeType.java: PHONE_OTP (ordinal 2)
          AuthClientData.OtpChallenge: field "code" = @o(name="code")
          AuthResponseTokenPair: @o(name="access_token"), @o(name="refresh_token")
        """
        payload: dict[str, Any] = {
            "challenge_type": "PHONE_OTP",
            "auth_flow_token": auth_flow_token,
            "data": {"code": code},
        }
        data = await self._post(_AUTH_URL, payload, operation="otp_challenge")

        # Successful response: challenge_type=AUTHORIZED, tokens inside data.data
        inner: dict[str, Any] = {}
        raw_data = data.get("data")
        if isinstance(raw_data, dict):
            inner = raw_data
        # Some responses embed tokens at top level
        access_token = inner.get("access_token") or data.get("access_token")
        refresh_token = inner.get("refresh_token") or data.get("refresh_token")

        if not access_token:
            _log_failure("otp_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No access_token in OTP challenge response")

        self._access_token = access_token
        self._refresh_token = refresh_token
        return {
            "access_token": access_token,
            "refresh_token": refresh_token or "",
        }

    async def _refresh(self) -> bool:
        """POST api/v1/auth/refresh — renew access token using refresh token.

        Proven by RefreshTokenRequest: @o(name="refresh_token"), @o(name="original_request_path")
        Response: AuthResponseTokenPair: @o(name="access_token"), @o(name="refresh_token")
        """
        if not self._refresh_token:
            return False
        payload = {"refresh_token": self._refresh_token}
        try:
            headers = self._base_headers()
            async with self._session.post(
                _REFRESH_URL, headers=headers, json=payload
            ) as resp:
                if resp.status >= 400:
                    return False
                data = await resp.json(content_type=None)
            access_token = data.get("access_token")
            if not access_token:
                return False
            self._access_token = access_token
            new_refresh = data.get("refresh_token")
            if new_refresh:
                self._refresh_token = new_refresh
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Data endpoints
    # ------------------------------------------------------------------

    async def get_premises(self) -> list[dict[str, Any]]:
        """GET api/v2/customer/premises — premises with supplies.

        Source: FuseApiService.java:195, PremisesWithSuppliesNetwork.java
        Returns list of {premises: {fid/premises_fid, address}, supplies: [...],
                         default_date_uk: "YYYY-MM-DD"}
        Supply fields: supply_fid, supply_type (ELECTRICITY_IMPORT/GAS),
                       identifier (MPAN or MPRN), serial_number, meter_type
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
        """GET api/v1/premises/{fid}/chart — consumption chart.

        Source: FuseApiService.java:162
        Granularity: year+month = daily bars (primary poll mode).
        Response (ChartResponse.java):
          total_bars: [{index: {year,month,day}, kWh: {decimal_value},
                        money: {amount,currency}, type: ACTUAL|ESTIMATED|FORECAST}]
          supplies: [per-supply breakdown with supply_type]
          total_realised_money, total_money: {amount, currency}
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
        """GET api/v5/contracts-current — current tariff for a supply.

        Source: FuseApiService.java:192, CurrentContracts.java
        Response: {current: {tariff: {tariff_id, supply_type, title}, from_date_uk, to_date_uk}}
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
        """GET api/v1/tariff/details — unit rates and standing charges.

        Source: FuseApiService.java:228, TariffDetailsResponse.java
        Response includes UnitChargesNetwork (rate_name, price_per_kWh) and ApiStandingCharge.
        """
        url = (
            f"{API_BASE_URL}/api/v1/tariff/details"
            f"?supply_fid={supply_fid}&tariff_ids={tariff_id}"
        )
        result = await self._get(url, operation="get_tariff_details")
        return result if isinstance(result, dict) else {}

    async def get_balance(self) -> dict[str, Any]:
        """GET api/v1/balance — account balance.

        Source: FuseApiService.java:240
        Response: Money = {amount: BigDecimal, currency: "GBP"}
        """
        url = f"{API_BASE_URL}/api/v1/balance"
        result = await self._get(url, operation="get_balance")
        return result if isinstance(result, dict) else {}

    async def get_individual(self) -> dict[str, Any]:
        """GET api/v1/individual — account/customer info.

        Source: FuseApiService.java:204, IndividualNetwork.java
        """
        url = f"{API_BASE_URL}/api/v1/individual"
        result = await self._get(url, operation="get_individual")
        return result if isinstance(result, dict) else {}

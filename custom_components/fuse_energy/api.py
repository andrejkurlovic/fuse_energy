"""Async API client for FUSE Energy."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)

_AUTH_URL = f"{API_BASE_URL}/api/v3/auth"
_REFRESH_URL = f"{API_BASE_URL}/api/v1/auth/refresh"


class FuseAuthError(Exception):
    pass


class FuseError(Exception):
    pass


def _log_failure(
    operation: str,
    url: str,
    *,
    status: int | None = None,
    body: str | None = None,
    exc: BaseException | None = None,
) -> None:
    parts = [f"FUSE API failure: operation={operation}", f"url={url}"]
    if status is not None:
        parts.append(f"status={status}")
    if exc is not None:
        parts.append(f"exc={type(exc).__name__}: {exc}")
    if body:
        parts.append(f"body={body[:500]}")
    _LOGGER.error(" | ".join(parts))


def _extract_auth_flow_token(data: Any) -> str | None:
    if isinstance(data, dict):
        token = data.get("auth_flow_token") or data.get("authFlowToken")
        if token:
            return token
        for v in data.values():
            result = _extract_auth_flow_token(v)
            if result:
                return result
    return None


def _extract_tokens(data: Any) -> dict[str, str] | None:
    if isinstance(data, dict):
        at = data.get("access_token")
        rt = data.get("refresh_token")
        if at and rt:
            return {"access_token": at, "refresh_token": rt}
        for v in data.values():
            result = _extract_tokens(v)
            if result:
                return result
    return None


def _detect_challenge_type(data: Any) -> str:
    if isinstance(data, dict):
        ct = str(data.get("challenge_type", ""))
        if "MAGIC_LINK" in ct.upper():
            return "magic_link"
        ct2 = str(data.get("status_string", ""))
        if "MAGIC" in ct2.upper():
            return "magic_link"
        for v in data.values():
            if isinstance(v, dict):
                if _detect_challenge_type(v) == "magic_link":
                    return "magic_link"
    return "otp"


class FuseEnergyAPI:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def set_tokens(self, access_token: str, refresh_token: str) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = False,
        operation: str = "",
        suppress_error_log: bool = False,
    ) -> dict[str, Any] | list[Any]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if authenticated and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            async with self._session.request(
                method, url, headers=headers, json=json
            ) as resp:
                body_text = await resp.text()
                _LOGGER.debug(
                    "%s response [%d]: %s", operation, resp.status, body_text[:500]
                )

                if resp.status == 401 and authenticated and self._refresh_token:
                    refreshed = await self._refresh()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {self._access_token}"
                        async with self._session.request(
                            method, url, headers=headers, json=json
                        ) as retry_resp:
                            body_text = await retry_resp.text()
                            if retry_resp.status >= 400:
                                if not suppress_error_log:
                                    _log_failure(
                                        operation, url,
                                        status=retry_resp.status, body=body_text,
                                    )
                                raise FuseError(f"HTTP {retry_resp.status}")
                            return await retry_resp.json(content_type=None)

                if resp.status >= 400:
                    if not suppress_error_log:
                        _log_failure(operation, url, status=resp.status, body=body_text)
                    raise FuseError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            if not suppress_error_log:
                _log_failure(operation, url, exc=exc)
            raise FuseError(str(exc)) from exc

    async def initial_challenge(self, email: str) -> dict[str, Any]:
        payload = {
            "challenge_type": "INITIAL",
            "data": {
                "method": "EMAIL",
                "data": {
                    "email_address": email,
                },
            },
        }
        data = await self._request(
            "POST", _AUTH_URL, json=payload, operation="initial_challenge"
        )

        token = _extract_auth_flow_token(data)
        if not token:
            _log_failure("initial_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No auth_flow_token in response")

        challenge_type = _detect_challenge_type(data)
        _LOGGER.info(
            "Initial challenge succeeded, flow type: %s", challenge_type
        )
        return {
            "auth_flow_token": token,
            "challenge_type": challenge_type,
        }

    async def otp_challenge(self, code: str, auth_flow_token: str) -> dict[str, str]:
        payload = {
            "challenge_type": "PHONE_OTP",
            "data": {
                "code": code,
            },
            "auth_flow_token": auth_flow_token,
        }
        data = await self._request(
            "POST", _AUTH_URL, json=payload, operation="otp_challenge"
        )

        tokens = _extract_tokens(data)
        if not tokens:
            _log_failure("otp_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("Authentication failed — no tokens in response")

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        return tokens

    async def magic_link_check(
        self, magic_token: str, auth_flow_token: str
    ) -> dict[str, str] | None:
        payload = {
            "challenge_type": "MAGIC_LINK_CHECK",
            "data": {
                "token": magic_token,
            },
            "auth_flow_token": auth_flow_token,
        }
        try:
            data = await self._request(
                "POST", _AUTH_URL, json=payload,
                operation="magic_link_check", suppress_error_log=True,
            )
        except FuseError:
            return None

        tokens = _extract_tokens(data)
        if tokens:
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]
            return tokens
        return None

    async def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        payload = {"refresh_token": self._refresh_token}
        try:
            data = await self._request(
                "POST", _REFRESH_URL, json=payload, operation="refresh"
            )
            tokens = _extract_tokens(data)
            if not tokens:
                return False
            self._access_token = tokens["access_token"]
            refresh = tokens.get("refresh_token")
            if refresh:
                self._refresh_token = refresh
            return True
        except FuseError:
            return False

    async def get_premises(self) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v2/customer/premises",
            authenticated=True,
            operation="get_premises",
        )

    async def get_balance(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v1/balance",
            authenticated=True,
            operation="get_balance",
        )

    async def get_chart(
        self,
        premises_fid: str,
        year: int,
        month: int | None = None,
        day: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"year": year}
        if month is not None:
            params["month"] = month
        if day is not None:
            params["day"] = day
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v1/premises/{premises_fid}/chart?{query}",
            authenticated=True,
            operation="get_chart",
        )

    async def get_tariff_details(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v1/tariff/details",
            authenticated=True,
            operation="get_tariff_details",
        )

    async def get_current_contracts(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v5/contracts-current",
            authenticated=True,
            operation="get_current_contracts",
        )

    async def get_bill(self, premises_fid: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v1/premises/{premises_fid}/your-bill",
            authenticated=True,
            operation="get_bill",
        )

    async def get_direct_debit_status(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE_URL}/api/v1/direct-debit-status",
            authenticated=True,
            operation="get_direct_debit_status",
        )

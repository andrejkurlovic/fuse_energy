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
    ) -> dict[str, Any] | list[Any]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if authenticated and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            async with self._session.request(
                method, url, headers=headers, json=json
            ) as resp:
                body_text = await resp.text()
                if resp.status == 401 and authenticated and self._refresh_token:
                    refreshed = await self._refresh()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {self._access_token}"
                        async with self._session.request(
                            method, url, headers=headers, json=json
                        ) as retry_resp:
                            body_text = await retry_resp.text()
                            if retry_resp.status >= 400:
                                _log_failure(operation, url, status=retry_resp.status, body=body_text)
                                raise FuseError(f"HTTP {retry_resp.status}")
                            return await retry_resp.json(content_type=None)

                if resp.status >= 400:
                    _log_failure(operation, url, status=resp.status, body=body_text)
                    raise FuseError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            _log_failure(operation, url, exc=exc)
            raise FuseError(str(exc)) from exc

    async def initial_challenge(self, email: str) -> str:
        payload = {
            "challenge_type": "InitialChallenge",
            "data": {"email": email},
        }
        data = await self._request(
            "POST", _AUTH_URL, json=payload, operation="initial_challenge"
        )
        auth_flow_token = data.get("auth_flow_token") or data.get("authFlowToken")
        if not auth_flow_token:
            _log_failure("initial_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("No auth_flow_token in response")
        return auth_flow_token

    async def otp_challenge(self, otp: str, auth_flow_token: str) -> dict[str, str]:
        payload = {
            "challenge_type": "OtpChallenge",
            "data": {"otp": otp},
            "auth_flow_token": auth_flow_token,
        }
        data = await self._request(
            "POST", _AUTH_URL, json=payload, operation="otp_challenge"
        )
        inner = data.get("data", data)
        access_token = inner.get("access_token")
        refresh_token = inner.get("refresh_token")
        if not access_token:
            _log_failure("otp_challenge", _AUTH_URL, body=str(data)[:500])
            raise FuseAuthError("Authentication failed — no access token returned")
        self._access_token = access_token
        self._refresh_token = refresh_token
        return {"access_token": access_token, "refresh_token": refresh_token}

    async def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        payload = {"refresh_token": self._refresh_token}
        try:
            data = await self._request(
                "POST", _REFRESH_URL, json=payload, operation="refresh"
            )
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            if not access_token:
                return False
            self._access_token = access_token
            if refresh_token:
                self._refresh_token = refresh_token
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

    async def get_chart(self, premises_fid: str, year: int, month: int | None = None, day: int | None = None) -> dict[str, Any]:
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

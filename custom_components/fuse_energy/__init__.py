"""Fuse Energy+ Home Assistant integration."""
from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuseAuthError, FuseEnergyAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_PREMISES_FID,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import FuseEnergyCoordinator
from .statistics import async_run_import

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.SENSOR]

_IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("start_date"): str,
        vol.Optional("end_date"): str,
        vol.Optional("include_electricity", default=True): bool,
        vol.Optional("include_gas", default=True): bool,
        vol.Optional("include_cost", default=True): bool,
        vol.Optional("granularity", default="auto"): vol.In(["auto", "daily", "hourly"]),
        vol.Optional("dry_run", default=True): bool,
    }
)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        _LOGGER.warning("FuseEnergy: invalid date %r — expected YYYY-MM-DD", s)
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    # Callback: called by the API after a silent token refresh so we can persist
    # the new tokens to the config entry. Without this, HA loses the refreshed
    # token on restart and has to re-auth unnecessarily.
    async def _on_token_refresh(access_token: str, refresh_token: str | None) -> None:
        updates: dict = {CONF_ACCESS_TOKEN: access_token}
        if refresh_token:
            updates[CONF_REFRESH_TOKEN] = refresh_token
        hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
        _LOGGER.debug("FuseEnergy: persisted refreshed tokens to config entry")

    # session_id and device_id are persistent per-installation UUIDs (required headers).
    # Generated during config flow and stored in entry.data.
    api = FuseEnergyAPI(
        session,
        session_id=entry.data.get(CONF_SESSION_ID),
        device_id=entry.data.get(CONF_DEVICE_ID),
        on_token_refresh=_on_token_refresh,
    )
    api.set_tokens(
        entry.data[CONF_ACCESS_TOKEN],
        entry.data.get(CONF_REFRESH_TOKEN),
    )

    coordinator = FuseEnergyCoordinator(
        hass,
        api,
        premises_fid=entry.data.get(CONF_PREMISES_FID, ""),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except FuseAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    # Register import_history service — idempotent (registered once for the domain).
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):

        async def _handle_import_history(call: ServiceCall) -> None:
            co: FuseEnergyCoordinator | None = next(
                (v for v in hass.data.get(DOMAIN, {}).values()
                 if isinstance(v, FuseEnergyCoordinator) and v.data is not None),
                None,
            )
            if co is None:
                _LOGGER.error(
                    "FuseEnergy import_history: no loaded coordinator — "
                    "is the integration authenticated and running?"
                )
                return

            data = call.data
            # Run as background task; service returns immediately.
            # Check HA logs for progress and the final summary.
            hass.async_create_task(
                async_run_import(
                    hass,
                    co.api,
                    co.data.premises_fid,
                    [sd.supply for sd in co.data.supplies],
                    start_date=_parse_date(data.get("start_date")),
                    end_date=_parse_date(data.get("end_date")),
                    include_electricity=data.get("include_electricity", True),
                    include_gas=data.get("include_gas", True),
                    include_cost=data.get("include_cost", True),
                    granularity=data.get("granularity", "auto"),
                    dry_run=data.get("dry_run", True),
                ),
                name="fuse_energy_import_history",
            )
            _LOGGER.info(
                "FuseEnergy import_history started (dry_run=%s) — check HA logs for summary",
                data.get("dry_run", True),
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            _handle_import_history,
            schema=_IMPORT_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
    return unload_ok

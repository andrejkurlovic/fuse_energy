"""Fuse Energy+ Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
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
)
from .coordinator import FuseEnergyCoordinator

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    # session_id and device_id are persistent per-installation UUIDs (required headers).
    # Generated during config flow and stored in entry.data.
    api = FuseEnergyAPI(
        session,
        session_id=entry.data.get(CONF_SESSION_ID),
        device_id=entry.data.get(CONF_DEVICE_ID),
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
    except FuseAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

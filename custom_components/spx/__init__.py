"""SPX integration for Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SPXApi, SPXAuthError, SPXApiError
from .const import DOMAIN
from .coordinator import SPXCoordinator

PLATFORMS = [Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    refresh_token = entry.data.get("refresh_token")
    if not refresh_token:
        raise ConfigEntryAuthFailed("SPX account sign in is required")
    api = SPXApi(async_get_clientsession(hass), refresh_token)
    coordinator = SPXCoordinator(hass, api, f"spx_{entry.entry_id}")
    try:
        await coordinator.start()
    except SPXAuthError as exc:
        await coordinator.stop()
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except (SPXApiError, OSError, TimeoutError) as exc:
        await coordinator.stop()
        raise ConfigEntryNotReady(str(exc)) from exc
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator: SPXCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop()
    return True

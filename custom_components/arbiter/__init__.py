"""Arbiter — decide what a switch should be doing when several automations disagree."""

from __future__ import annotations

import logging

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import DOMAIN, PLATFORMS
from .hub import ArbiterHub
from .schema import CONFIG_YAML, build_config
from .services import (
    async_refresh_reason_options,
    async_register_services,
    async_unregister_services,
)

_LOGGER = logging.getLogger(__name__)

type ArbiterConfigEntry = ConfigEntry[ArbiterHub]

CONFIG_SCHEMA = vol.Schema({DOMAIN: CONFIG_YAML}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import a YAML block, if one is present.

    This is a bootstrap so a prepared configuration can be adopted in one restart;
    once imported, the GUI is the source of truth and the YAML is ignored.
    """
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=config[DOMAIN]
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ArbiterConfigEntry) -> bool:
    """Set up the arbiter from its config entry."""
    hub = ArbiterHub(hass, entry.entry_id, build_config(hass, entry))
    await hub.async_setup()
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    # The `reason` dropdowns can only list what exists, and what exists changes as
    # reasons open and close. Wiring this here rather than inside the hub keeps the
    # hub from having to know about the service layer.
    @callback
    def _reason_options_changed() -> None:
        async_refresh_reason_options(hass, hub)

    entry.async_on_unload(hub.async_add_listener(_reason_options_changed))
    async_refresh_reason_options(hass, hub)

    # Fires for options changes *and* for any subentry being added, edited or
    # removed, so every kind of config change takes effect the same way.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ArbiterConfigEntry) -> bool:
    """Tear the arbiter down."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
        # Drop the remembered option lists so a reload re-publishes them.
        hass.data.pop(DOMAIN, None)
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_services(hass)
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ArbiterConfigEntry) -> None:
    """Rebuild after any configuration change."""
    await hass.config_entries.async_reload(entry.entry_id)

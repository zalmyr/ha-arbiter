"""A per-switch escape hatch.

Turning one of these off leaves the arbiter watching and reporting but not
writing, for exactly one switch. Useful when something is misconfigured at 2am and
you want it to stop touching the lights without pulling the whole integration.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ArbiterConfigEntry
from .entity import ArbiterSwitchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArbiterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one arbitration toggle per managed switch."""
    hub = entry.runtime_data
    async_add_entities(ArbitrationSwitch(hub, entity_id) for entity_id in hub.config.switches)


class ArbitrationSwitch(ArbiterSwitchEntity, SwitchEntity, RestoreEntity):
    """Whether the arbiter may write to this switch."""

    _attr_translation_key = "arbitration"
    _attr_entity_registry_enabled_default = True

    def __init__(self, hub, entity_id: str) -> None:
        super().__init__(hub, entity_id, "arbitration")
        self._enabled = True

    async def async_added_to_hass(self) -> None:
        """Restore the toggle, so a deliberate stand-down survives a restart."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._enabled = last_state.state == STATE_ON
        await self.hub.async_set_enabled(self.switch_entity_id, self._enabled)

    @property
    def is_on(self) -> bool:
        return self._enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"mode": self.hub.config.mode_for(self.switch_entity_id).value}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let the arbiter act on this switch again."""
        self._enabled = True
        self.async_write_ha_state()
        await self.hub.async_set_enabled(self.switch_entity_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the arbiter writing to this switch; it keeps tracking."""
        self._enabled = False
        self.async_write_ha_state()
        await self.hub.async_set_enabled(self.switch_entity_id, False)

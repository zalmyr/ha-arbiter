"""Shared entity plumbing."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import ArbiterHub


class ArbiterEntity(Entity):
    """Base for everything the arbiter exposes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: ArbiterHub, device_key: str, device_name: str, key: str) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.entry_id}_{device_key}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{hub.entry_id}_{device_key}")},
            name=device_name,
            manufacturer="Arbiter",
            entry_type=None,
        )

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the arbiter recomputes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class ArbiterSwitchEntity(ArbiterEntity):
    """An entity that reports on one managed switch."""

    def __init__(self, hub: ArbiterHub, entity_id: str, key: str) -> None:
        state = hub.hass.states.get(entity_id)
        name = state.name if state else entity_id
        super().__init__(hub, entity_id, name, key)
        self.switch_entity_id = entity_id

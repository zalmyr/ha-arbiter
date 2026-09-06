"""A sensor per managed switch naming the reason that is currently winning."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ArbiterConfigEntry
from .entity import ArbiterSwitchEntity

STATE_UNCLAIMED = "unclaimed"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArbiterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one winner sensor per managed switch."""
    hub = entry.runtime_data
    async_add_entities(WinningReasonSensor(hub, entity_id) for entity_id in hub.config.switches)


class WinningReasonSensor(ArbiterSwitchEntity, SensorEntity):
    """Which reason is deciding this switch right now."""

    _attr_translation_key = "winning_reason"

    def __init__(self, hub, entity_id: str) -> None:
        super().__init__(hub, entity_id, "winning_reason")

    @property
    def native_value(self) -> str:
        decision = self.hub.async_decision(self.switch_entity_id)
        if decision is None or decision.winner is None:
            return STATE_UNCLAIMED
        return decision.winner.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        decision = self.hub.async_decision(self.switch_entity_id)
        if decision is None:
            return {}
        winner = decision.winner
        return {
            "decision": decision.state.value,
            "priority": winner.priority if winner else None,
            "opened_by": winner.source if winner else None,
            "opened_at": winner.opened.isoformat() if winner else None,
            "expires": winner.expires.isoformat() if winner and winner.expires else None,
            "overruled": [r.name for r in decision.losers],
            "considered": [r.name for r in decision.considered],
            "fallback": self.hub.config.switches[self.switch_entity_id].default_state.value,
        }

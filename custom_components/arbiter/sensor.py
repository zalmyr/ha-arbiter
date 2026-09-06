"""Sensors: the winner per managed switch, and one overview of every reason."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ArbiterConfigEntry
from .entity import ArbiterEntity, ArbiterSwitchEntity

STATE_UNCLAIMED = "unclaimed"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArbiterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one winner sensor per managed switch, plus the reason overview."""
    hub = entry.runtime_data
    entities: list[ArbiterEntity] = [
        WinningReasonSensor(hub, entity_id) for entity_id in hub.config.switches
    ]
    entities.append(ReasonOverviewSensor(hub))
    async_add_entities(entities)


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


class ReasonOverviewSensor(ArbiterEntity, SensorEntity):
    """Every reason in one place: what is running, and what refers to it.

    The per-reason ``binary_sensor.reason_*`` entities only exist for reasons you
    configured. Manual overrides and the placeholder reasons unmapped automations
    get are created at runtime and have no entity of their own, so without this
    they are visible only inside ``arbiter.explain``.
    """

    _attr_has_entity_name = False
    _attr_name = "Arbiter reasons"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_native_unit_of_measurement = "reasons"

    def __init__(self, hub) -> None:
        super().__init__(hub, "reasons", "Arbiter reasons", "overview")

    @property
    def native_value(self) -> int:
        """How many reasons are in force right now."""
        return len(self.hub.store.live(dt_util.utcnow()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        live = sorted(
            self.hub.store.live(now), key=lambda r: (-r.priority, r.name)
        )
        configured = self.hub.config.reasons
        live_names = {r.name for r in live}

        return {
            "live": [
                {
                    "name": reason.name,
                    "priority": reason.priority,
                    "wants": reason.state.value,
                    "origin": reason.origin.value,
                    "opened_by": reason.source,
                    "opened_at": reason.opened.isoformat(),
                    "expires": reason.expires.isoformat() if reason.expires else None,
                    "switches": sorted(reason.targets),
                    "configured": reason.name in configured,
                }
                for reason in live
            ],
            # Configured but not currently in force, so you can see the whole set.
            "idle": sorted(name for name in configured if name not in live_names),
            # Live without a definition behind them: manual overrides, and the weak
            # placeholders held for automations that have no reason mapping yet.
            "unconfigured": sorted(
                name for name in live_names if name not in configured
            ),
            "automations": self._async_mappings(),
        }

    def _async_mappings(self) -> dict[str, dict[str, list[str]]]:
        """Which automations open and close each configured reason."""
        mappings: dict[str, dict[str, list[str]]] = {
            name: {"opens": [], "closes": []} for name in self.hub.config.reasons
        }
        for automation, sources in self.hub.config.sources.items():
            for source in sources:
                entry = mappings.setdefault(
                    source.reason, {"opens": [], "closes": []}
                )
                entry[f"{source.action.value}"].append(automation)
        for entry in mappings.values():
            entry["opens"].sort()
            entry["closes"].sort()
        return mappings

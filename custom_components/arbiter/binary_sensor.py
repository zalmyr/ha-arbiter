"""Binary sensors: what the arbiter thinks, and which reasons are live."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ArbiterConfigEntry
from .const import DesiredState
from .entity import ArbiterEntity, ArbiterSwitchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArbiterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one 'should be on' and one 'conflict' per switch, plus a reason sensor each."""
    hub = entry.runtime_data
    entities: list[ArbiterEntity] = []

    for entity_id in hub.config.switches:
        entities.append(ShouldBeOnSensor(hub, entity_id))
        entities.append(ConflictSensor(hub, entity_id))

    for name in hub.config.reasons:
        entities.append(ReasonSensor(hub, name))

    async_add_entities(entities)


class ShouldBeOnSensor(ArbiterSwitchEntity, BinarySensorEntity):
    """Whether this switch should be on, according to the live reasons.

    Deliberately independent of what the switch is actually doing: in advisory mode
    this is how you see what the arbiter *would* do before letting it act.
    """

    _attr_translation_key = "should_be_on"

    def __init__(self, hub, entity_id: str) -> None:
        super().__init__(hub, entity_id, "should_be_on")

    @property
    def is_on(self) -> bool | None:
        decision = self.hub.async_decision(self.switch_entity_id)
        if decision is None or decision.state is DesiredState.UNMANAGED:
            return None
        return decision.state is DesiredState.ON

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        decision = self.hub.async_decision(self.switch_entity_id)
        if decision is None:
            return {}
        return {
            "winning_reason": decision.winner.name if decision.winner else None,
            "reasons": [
                {
                    "name": reason.name,
                    "priority": reason.priority,
                    "wants": reason.state.value,
                    "source": reason.source,
                    "expires": reason.expires.isoformat() if reason.expires else None,
                }
                for reason in sorted(
                    decision.considered, key=lambda r: r.priority, reverse=True
                )
            ],
            "mode": self.hub.config.mode_for(self.switch_entity_id).value,
            "arbitration_enabled": self.hub.async_is_enabled(self.switch_entity_id),
            "bypassed": self.hub.bypassed,
        }


class ConflictSensor(ArbiterSwitchEntity, BinarySensorEntity):
    """On while two live reasons want opposite things for this switch."""

    _attr_translation_key = "conflict"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hub, entity_id: str) -> None:
        super().__init__(hub, entity_id, "conflict")

    @property
    def is_on(self) -> bool:
        decision = self.hub.async_decision(self.switch_entity_id)
        return bool(decision and decision.is_conflict)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        decision = self.hub.async_decision(self.switch_entity_id)
        history = self.hub.conflicts.get(self.switch_entity_id)
        return {
            "overruled": [r.name for r in decision.losers] if decision else [],
            "recent": [
                {
                    "when": record.when.isoformat(),
                    "winner": record.winner,
                    "state": record.winner_state,
                    "overruled": list(record.losers),
                }
                for record in reversed(history or [])
            ],
        }


class ReasonSensor(ArbiterEntity, BinarySensorEntity):
    """Whether a configured reason is currently in force.

    The dashboard equivalent of the input_boolean you would otherwise wire by
    hand — except this one is opened, closed and expired for you.
    """

    #: Named without the device prefix so these land at binary_sensor.reason_<name>,
    #: which is what the input_booleans they replace would have been called.
    _attr_has_entity_name = False

    def __init__(self, hub, reason_name: str) -> None:
        super().__init__(hub, "reasons", "Arbiter reasons", f"reason_{reason_name}")
        self._reason_name = reason_name
        self._attr_name = f"Reason {reason_name}"

    @property
    def is_on(self) -> bool:
        return self.hub.async_reason_is_live(self._reason_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reason = self.hub.store.get(self._reason_name)
        definition = self.hub.config.reasons.get(self._reason_name)
        attributes: dict[str, Any] = {
            "priority": definition.priority if definition else None,
            "wants": definition.state.value if definition else None,
            "lifetime": definition.lifetime.value if definition else None,
        }
        if reason is not None:
            attributes.update(
                {
                    "opened_by": reason.source,
                    "opened_at": reason.opened.isoformat(),
                    "expires": reason.expires.isoformat() if reason.expires else None,
                    "switches": sorted(reason.targets),
                    "seconds_remaining": (
                        int((reason.expires - dt_util.utcnow()).total_seconds())
                        if reason.expires
                        else None
                    ),
                }
            )
        return attributes

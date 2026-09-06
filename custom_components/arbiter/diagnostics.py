"""Diagnostics: the whole picture in one download."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import ArbiterConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ArbiterConfigEntry
) -> dict[str, Any]:
    """Return everything needed to reason about a misbehaving instance."""
    hub = entry.runtime_data
    now = dt_util.utcnow()

    return {
        "globals": {
            "bypass_entity": hub.config.globals.bypass_entity,
            "bypassed_now": hub.bypassed,
            "default_mode": hub.config.globals.default_mode.value,
            "override_timeout": str(hub.config.globals.override_timeout),
            "override_priority": hub.config.globals.override_priority,
            "override_sources": sorted(hub.config.globals.override_sources),
            "unknown_source_priority": hub.config.globals.unknown_source_priority,
            "ignore_unknown_sources": hub.config.globals.ignore_unknown_sources,
            "tie_break": hub.config.globals.tie_break.value,
        },
        "config_problems": list(hub.problems),
        "reasons": {
            name: {
                "priority": reason.priority,
                "wants": reason.state.value,
                "lifetime": reason.lifetime.value,
                "membership": reason.membership.value,
                "entities": list(reason.entities),
                "label": reason.label,
                "area": reason.area,
                "max_hold": str(reason.max_hold) if reason.max_hold else None,
                "weekdays": sorted(reason.weekdays),
                "condition": reason.condition,
            }
            for name, reason in hub.config.reasons.items()
        },
        "sources": {
            automation: [f"{m.action.value} {m.reason}" for m in maps]
            for automation, maps in hub.config.sources.items()
        },
        "live_reasons": [
            {
                "name": r.name,
                "priority": r.priority,
                "wants": r.state.value,
                "origin": r.origin.value,
                "opened_by": r.source,
                "opened_at": r.opened.isoformat(),
                "expires": r.expires.isoformat() if r.expires else None,
                "live": r.is_live(now),
                "switches": sorted(r.targets),
            }
            for r in hub.store.all()
        ],
        "switches": {
            entity_id: {
                "mode": hub.config.mode_for(entity_id).value,
                "default_state": switch.default_state.value,
                "arbitration_enabled": hub.async_is_enabled(entity_id),
                "should_be": (
                    decision.state.value
                    if (decision := hub.async_decision(entity_id))
                    else None
                ),
                "actual": (
                    state.state if (state := hass.states.get(entity_id)) else None
                ),
                "winning_reason": (
                    decision.winner.name
                    if decision and decision.winner
                    else None
                ),
                "recent_conflicts": [
                    {
                        "when": record.when.isoformat(),
                        "winner": record.winner,
                        "state": record.winner_state,
                        "overruled": list(record.losers),
                    }
                    for record in reversed(hub.conflicts.get(entity_id, ()))
                ],
            }
            for entity_id, switch in hub.config.switches.items()
        },
    }

"""Services: drive reasons by hand, and ask the arbiter to explain itself."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.automation import (
    automations_with_area,
    automations_with_device,
    automations_with_entity,
    automations_with_label,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.util import dt as dt_util
import voluptuous as vol
import yaml

from .const import (
    ATTR_DURATION,
    ATTR_TARGETS,
    ATTR_UNTIL,
    CONF_ACTION,
    CONF_AUTOMATION,
    CONF_MODE,
    CONF_PRIORITY,
    CONF_REASON,
    CONF_REASONS,
    CONF_SOURCES,
    CONF_STATE,
    CONF_SWITCHES,
    DOMAIN,
    SERVICE_CLEAR_OVERRIDES,
    SERVICE_CLOSE,
    SERVICE_CLOSE_ALL,
    SERVICE_EXPLAIN,
    SERVICE_EXPORT_CONFIG,
    SERVICE_FIND_CONFLICTS,
    SERVICE_OPEN,
    SERVICE_OVERRIDE,
    SERVICE_SET_MODE,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    DesiredState,
    Mode,
    Origin,
)
from .hub import ArbiterHub, manual_reason_name

_LOGGER = logging.getLogger(__name__)

#: Where the last-pushed option lists are remembered, so the schema is only
#: re-registered when the set of reasons actually changes.
_OPTIONS_CACHE = "reason_options"

OPEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REASON): cv.string,
        vol.Optional(ATTR_TARGETS): cv.entity_ids,
        vol.Optional(CONF_PRIORITY): vol.All(int, vol.Range(0, 100)),
        vol.Optional(CONF_STATE): vol.In([DesiredState.ON.value, DesiredState.OFF.value]),
        vol.Optional(ATTR_DURATION): cv.time_period,
        vol.Optional(ATTR_UNTIL): cv.datetime,
    }
)

CLOSE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REASON): cv.string,
        vol.Optional(ATTR_TARGETS): cv.entity_ids,
    }
)

CLOSE_ALL_SCHEMA = vol.Schema({vol.Optional(ATTR_TARGETS): cv.entity_ids})

OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(CONF_STATE): vol.In([DesiredState.ON.value, DesiredState.OFF.value]),
        vol.Optional(ATTR_DURATION): cv.time_period,
    }
)

CLEAR_OVERRIDES_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTITY_ID): cv.entity_ids})

SET_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(CONF_MODE): vol.In([m.value for m in Mode]),
    }
)

EXPLAIN_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTITY_ID): cv.entity_ids})


@callback
def _hub(hass: HomeAssistant) -> ArbiterHub:
    """Return the single arbiter hub, or explain why there isn't one."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("The Arbiter integration is not set up")
    return entries[0].runtime_data


@callback
def _openable(hub: ArbiterHub) -> list[str]:
    """Reasons `open` will accept: the ones actually defined."""
    return sorted(hub.config.reasons)


@callback
def _closeable(hub: ArbiterHub) -> list[str]:
    """Reasons `close` will accept.

    Includes reasons that are live without being configured — manual overrides and
    the placeholders unmapped automations get. They show up in
    ``sensor.arbiter_reasons``, so they have to be closable too.
    """
    live = {reason.name for reason in hub.store.live(dt_util.utcnow())}
    return sorted(set(hub.config.reasons) | live)


@callback
def _require_known(name: str, allowed: list[str], service: str) -> None:
    """Refuse a reason name that is not on the list.

    A typo used to be silent: `close` on a name nothing matched did nothing and
    reported nothing.
    """
    if name in allowed:
        return
    known = ", ".join(allowed) if allowed else "none are defined yet"
    raise ServiceValidationError(
        f"{DOMAIN}.{service} does not know the reason {name!r}. Valid reasons: {known}."
    )


#: Entity picker matching the domains a reason can drive.
_SWITCHES_SELECTOR = {
    "entity": {
        "multiple": True,
        "domain": ["switch", "light", "fan", "input_boolean"],
    }
}


@callback
def _reason_field(options: list[str]) -> dict[str, Any]:
    """A dropdown of reason names, with no free-text escape hatch."""
    return {
        "required": True,
        "selector": {"select": {"options": options, "mode": "dropdown", "sort": True}},
    }


@callback
def _open_description(options: list[str]) -> dict[str, Any]:
    """The `open` form, mirroring services.yaml but with a real reason picker."""
    return {
        "fields": {
            CONF_REASON: _reason_field(options),
            ATTR_TARGETS: {"selector": _SWITCHES_SELECTOR},
            CONF_PRIORITY: {
                "selector": {"number": {"min": 0, "max": 100, "mode": "slider"}}
            },
            CONF_STATE: {
                "selector": {
                    "select": {
                        "options": [DesiredState.ON.value, DesiredState.OFF.value]
                    }
                }
            },
            ATTR_DURATION: {"selector": {"duration": None}},
            ATTR_UNTIL: {"selector": {"datetime": None}},
        }
    }


@callback
def _close_description(options: list[str]) -> dict[str, Any]:
    """The `close` form."""
    return {
        "fields": {
            CONF_REASON: _reason_field(options),
            ATTR_TARGETS: {"selector": _SWITCHES_SELECTOR},
        }
    }


@callback
def async_refresh_reason_options(hass: HomeAssistant, hub: ArbiterHub) -> None:
    """Point the `reason` dropdowns at the reasons that exist right now.

    services.yaml can only describe a static text box, so the option lists are
    pushed at runtime and re-pushed whenever the set of reasons changes. The
    translations still supply the field names and descriptions.
    """
    openable, closeable = _openable(hub), _closeable(hub)

    cache = hass.data.setdefault(DOMAIN, {})
    if cache.get(_OPTIONS_CACHE) == (openable, closeable):
        return
    cache[_OPTIONS_CACHE] = (openable, closeable)

    async_set_service_schema(hass, DOMAIN, SERVICE_OPEN, _open_description(openable))
    async_set_service_schema(hass, DOMAIN, SERVICE_CLOSE, _close_description(closeable))


def async_register_services(hass: HomeAssistant) -> None:
    """Register every arbiter service, once."""
    if hass.services.has_service(DOMAIN, SERVICE_OPEN):
        return

    async def _open(call: ServiceCall) -> None:
        hub = _hub(hass)
        _require_known(call.data[CONF_REASON], _openable(hub), SERVICE_OPEN)
        expires = call.data.get(ATTR_UNTIL)
        if expires is not None:
            expires = dt_util.as_utc(expires)
        elif (duration := call.data.get(ATTR_DURATION)) is not None:
            expires = dt_util.utcnow() + duration
        state = call.data.get(CONF_STATE)
        await hub.async_open(
            call.data[CONF_REASON],
            state=DesiredState(state) if state else None,
            priority=call.data.get(CONF_PRIORITY),
            targets=call.data.get(ATTR_TARGETS),
            expires=expires,
        )

    async def _close(call: ServiceCall) -> None:
        hub = _hub(hass)
        _require_known(call.data[CONF_REASON], _closeable(hub), SERVICE_CLOSE)
        await hub.async_close(
            call.data[CONF_REASON], targets=call.data.get(ATTR_TARGETS)
        )

    async def _close_all(call: ServiceCall) -> None:
        await _hub(hass).async_close_all(targets=call.data.get(ATTR_TARGETS))

    async def _override(call: ServiceCall) -> None:
        hub = _hub(hass)
        duration: timedelta = call.data.get(
            ATTR_DURATION, hub.config.globals.override_timeout
        )
        state = DesiredState(call.data[CONF_STATE])
        for entity_id in call.data[ATTR_ENTITY_ID]:
            await hub.async_open(
                manual_reason_name(entity_id),
                state=state,
                priority=hub.config.globals.override_priority,
                targets=[entity_id],
                expires=dt_util.utcnow() + duration,
                source="service",
                origin=Origin.OVERRIDE,
            )

    async def _clear_overrides(call: ServiceCall) -> None:
        hub = _hub(hass)
        if entity_ids := call.data.get(ATTR_ENTITY_ID):
            for entity_id in entity_ids:
                await hub.async_close(manual_reason_name(entity_id))
        else:
            await hub.async_close_all(origins={Origin.OVERRIDE})

    async def _set_mode(call: ServiceCall) -> None:
        hub = _hub(hass)
        mode = Mode(call.data[CONF_MODE])
        for entity_id in call.data[ATTR_ENTITY_ID]:
            await hub.async_set_mode(entity_id, mode)

    async def _explain(call: ServiceCall) -> ServiceResponse:
        hub = _hub(hass)
        wanted = call.data.get(ATTR_ENTITY_ID) or list(hub.config.switches)
        return {"switches": {e: _explain_switch(hub, e) for e in wanted}}

    async def _find_conflicts(call: ServiceCall) -> ServiceResponse:
        return _find_conflicts_response(hass, _hub(hass))

    async def _export_config(call: ServiceCall) -> ServiceResponse:
        return _export(hass)

    hass.services.async_register(DOMAIN, SERVICE_OPEN, _open, schema=OPEN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLOSE, _close, schema=CLOSE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLOSE_ALL, _close_all, schema=CLOSE_ALL_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_OVERRIDE, _override, schema=OVERRIDE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_OVERRIDES, _clear_overrides, schema=CLEAR_OVERRIDES_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_SET_MODE, _set_mode, schema=SET_MODE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPLAIN,
        _explain,
        schema=EXPLAIN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FIND_CONFLICTS,
        _find_conflicts,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_CONFIG,
        _export_config,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the services when the last entry unloads."""
    for service in (
        SERVICE_OPEN,
        SERVICE_CLOSE,
        SERVICE_CLOSE_ALL,
        SERVICE_OVERRIDE,
        SERVICE_CLEAR_OVERRIDES,
        SERVICE_SET_MODE,
        SERVICE_EXPLAIN,
        SERVICE_FIND_CONFLICTS,
        SERVICE_EXPORT_CONFIG,
    ):
        hass.services.async_remove(DOMAIN, service)


def _explain_switch(hub: ArbiterHub, entity_id: str) -> dict[str, Any]:
    """Describe why a switch is where it is."""
    decision = hub.async_decision(entity_id)
    switch = hub.config.switches.get(entity_id)
    if decision is None or switch is None:
        return {"managed": False}

    return {
        "managed": True,
        "mode": hub.config.mode_for(entity_id).value,
        "arbitration_enabled": hub.async_is_enabled(entity_id),
        "bypassed": hub.bypassed,
        "should_be": decision.state.value,
        "actual": (state.state if (state := hub.hass.states.get(entity_id)) else None),
        "winning_reason": decision.winner.name if decision.winner else None,
        "because": (
            f"{decision.winner.name} (priority {decision.winner.priority}) wants "
            f"{decision.winner.state.value}"
            if decision.winner
            else f"no reason is live, so the fallback is {switch.default_state.value}"
        ),
        "live_reasons": [
            {
                "name": r.name,
                "priority": r.priority,
                "wants": r.state.value,
                "origin": r.origin.value,
                "opened_by": r.source,
                "opened_at": r.opened.isoformat(),
                "expires": r.expires.isoformat() if r.expires else None,
            }
            for r in sorted(decision.considered, key=lambda r: r.priority, reverse=True)
        ],
        "overruled": [r.name for r in decision.losers],
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


def _automations_touching(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Every automation that can command this switch, however it addresses it.

    A label or area target does not name the entity, so checking only
    ``automations_with_entity`` would miss most of the real callers.
    """
    found = set(automations_with_entity(hass, entity_id))

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return sorted(found)

    for label in entry.labels:
        found |= set(automations_with_label(hass, label))

    area_id = entry.area_id
    if entry.device_id:
        found |= set(automations_with_device(hass, entry.device_id))
        if area_id is None:
            device = dr.async_get(hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
    if area_id:
        found |= set(automations_with_area(hass, area_id))

    return sorted(found)


def _automation_enabled(hass: HomeAssistant, entity_id: str) -> bool:
    """Return False when an automation exists in the config but is switched off.

    Whether an automation is enabled lives in the entity registry, not in
    automations.yaml, so reading the file cannot tell you whether it actually runs.
    ``automations_with_*`` does not filter on it either — it walks every loaded
    automation entity regardless of state — so without this check a deliberately
    switched-off automation would be reported as a gap to close.
    """
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_ON


def _find_conflicts_response(hass: HomeAssistant, hub: ArbiterHub) -> ServiceResponse:
    """Report who can touch each switch and who has actually fought over it."""
    mapped = set(hub.config.sources)
    overrides = hub.config.globals.override_sources
    switches: dict[str, Any] = {}

    for entity_id in hub.config.switches:
        touching = _automations_touching(hass, entity_id)
        disabled = [a for a in touching if not _automation_enabled(hass, a)]
        switches[entity_id] = {
            "automations": touching,
            "disabled": disabled,
            "unmapped": [
                a
                for a in touching
                if a not in mapped and a not in overrides and a not in disabled
            ],
            "observed_conflicts": [
                {
                    "when": record.when.isoformat(),
                    "winner": record.winner,
                    "overruled": list(record.losers),
                }
                for record in reversed(hub.conflicts.get(entity_id, ()))
            ],
        }

    return {
        "switches": switches,
        "config_problems": list(hub.problems),
        "hint": (
            "An automation that only turns things off at the end of an event is "
            "usually a 'closes' mapping, not an 'opens' one: closing lets whatever "
            "else is still live keep the switch on. Anything listed under 'disabled' "
            "is switched off and needs no mapping until you turn it back on."
        ),
    }


def _export(hass: HomeAssistant) -> ServiceResponse:
    """Dump the live configuration as YAML, for committing to version control."""
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    config: dict[str, Any] = dict(entry.data) | dict(entry.options)

    buckets = {
        SUBENTRY_REASON: CONF_REASONS,
        SUBENTRY_SWITCH: CONF_SWITCHES,
        SUBENTRY_SOURCE: CONF_SOURCES,
    }
    for key in buckets.values():
        config[key] = []
    for subentry in entry.subentries.values():
        if (key := buckets.get(subentry.subentry_type)) is not None:
            config[key].append(dict(subentry.data))

    config[CONF_SOURCES].sort(key=lambda s: (s[CONF_AUTOMATION], s[CONF_ACTION]))

    return {
        "yaml": yaml.safe_dump({DOMAIN: config}, sort_keys=False, allow_unicode=True),
        "config": config,
    }

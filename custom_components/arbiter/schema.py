"""Turn stored config into models, and describe the forms that produce it.

The GUI writes subentries and the YAML importer writes the same dicts, so both
paths converge here and there is only one place that knows what a stored reason
looks like.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, selector
import voluptuous as vol

from .const import (
    CONF_ACTION,
    CONF_AREA,
    CONF_AUTOMATION,
    CONF_BYPASS_ENTITY,
    CONF_CONDITION,
    CONF_DEFAULT_MODE,
    CONF_DEFAULT_STATE,
    CONF_END,
    CONF_ENTITIES,
    CONF_ENTITY_ID,
    CONF_EVENT,
    CONF_IGNORE_UNKNOWN_SOURCES,
    CONF_KIND,
    CONF_LABEL,
    CONF_LIFETIME,
    CONF_MAX_HOLD,
    CONF_MEMBERSHIP,
    CONF_MODE,
    CONF_NAME,
    CONF_OFFSET,
    CONF_OVERRIDE_PRIORITY,
    CONF_OVERRIDE_SOURCES,
    CONF_OVERRIDE_TIMEOUT,
    CONF_PRIORITY,
    CONF_REASON,
    CONF_REASONS,
    CONF_SOURCES,
    CONF_START,
    CONF_STATE,
    CONF_SWITCHES,
    CONF_TIE_BREAK,
    CONF_TIME,
    CONF_UNKNOWN_SOURCE_PRIORITY,
    DEFAULT_OVERRIDE_PRIORITY,
    DEFAULT_OVERRIDE_TIMEOUT,
    DEFAULT_PRIORITY,
    DEFAULT_UNKNOWN_SOURCE_PRIORITY,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    BoundaryKind,
    DesiredState,
    Lifetime,
    Membership,
    Mode,
    SourceAction,
    TieBreak,
)
from .models import Boundary, GlobalConfig, ReasonDef, SourceMap, SwitchConfig
from .registry import ArbiterConfig

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUN_EVENTS = ("sunrise", "sunset")
CONF_WEEKDAYS = "weekdays"


# -- selector helpers ---------------------------------------------------------


def _select(options: tuple[str, ...], key: str, *, multiple: bool = False):
    """A translated dropdown, so the UI shows wording rather than raw values."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(options),
            translation_key=key,
            mode=selector.SelectSelectorMode.DROPDOWN,
            multiple=multiple,
        )
    )


PRIORITY_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=100, step=1, mode=selector.NumberSelectorMode.SLIDER)
)
DURATION_SELECTOR = selector.DurationSelector(
    selector.DurationSelectorConfig(enable_day=True)
)
SWITCH_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["switch", "light", "fan", "input_boolean"])
)
SWITCHES_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        domain=["switch", "light", "fan", "input_boolean"], multiple=True
    )
)
AUTOMATION_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="automation")
)
AUTOMATIONS_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="automation", multiple=True)
)
BOUNDARY_ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["input_datetime", "sensor"])
)


# -- form schemas -------------------------------------------------------------


def global_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """The main setup / options form."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_BYPASS_ENTITY,
                description={"suggested_value": defaults.get(CONF_BYPASS_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="input_boolean")
            ),
            vol.Required(
                CONF_DEFAULT_MODE, default=defaults.get(CONF_DEFAULT_MODE, Mode.ADVISORY.value)
            ): _select(tuple(m.value for m in Mode), CONF_MODE),
            vol.Required(
                CONF_OVERRIDE_TIMEOUT,
                default=defaults.get(
                    CONF_OVERRIDE_TIMEOUT, _duration_to_dict(DEFAULT_OVERRIDE_TIMEOUT)
                ),
            ): DURATION_SELECTOR,
            vol.Required(
                CONF_OVERRIDE_PRIORITY,
                default=defaults.get(CONF_OVERRIDE_PRIORITY, DEFAULT_OVERRIDE_PRIORITY),
            ): PRIORITY_SELECTOR,
            vol.Optional(
                CONF_OVERRIDE_SOURCES,
                description={"suggested_value": defaults.get(CONF_OVERRIDE_SOURCES, [])},
            ): AUTOMATIONS_SELECTOR,
            vol.Required(
                CONF_UNKNOWN_SOURCE_PRIORITY,
                default=defaults.get(
                    CONF_UNKNOWN_SOURCE_PRIORITY, DEFAULT_UNKNOWN_SOURCE_PRIORITY
                ),
            ): PRIORITY_SELECTOR,
            vol.Required(
                CONF_IGNORE_UNKNOWN_SOURCES,
                default=defaults.get(CONF_IGNORE_UNKNOWN_SOURCES, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_TIE_BREAK, default=defaults.get(CONF_TIE_BREAK, TieBreak.LAST_WINS.value)
            ): _select(tuple(t.value for t in TieBreak), CONF_TIE_BREAK),
        }
    )


def reason_basics_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step 1 of a reason: what it is and how strongly it argues."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): cv.string,
            vol.Required(
                CONF_PRIORITY, default=defaults.get(CONF_PRIORITY, DEFAULT_PRIORITY)
            ): PRIORITY_SELECTOR,
            vol.Required(
                CONF_STATE, default=defaults.get(CONF_STATE, DesiredState.ON.value)
            ): _select((DesiredState.ON.value, DesiredState.OFF.value), CONF_STATE),
            vol.Required(
                CONF_MEMBERSHIP,
                default=defaults.get(CONF_MEMBERSHIP, Membership.ENTITIES.value),
            ): _select(tuple(m.value for m in Membership), CONF_MEMBERSHIP),
            vol.Required(
                CONF_LIFETIME, default=defaults.get(CONF_LIFETIME, Lifetime.LATCH.value)
            ): _select(tuple(x.value for x in Lifetime), CONF_LIFETIME),
        }
    )


def membership_schema(
    membership: Membership, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Step 2 of a reason: which switches it covers."""
    defaults = defaults or {}
    match membership:
        case Membership.ENTITIES:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])
                    ): SWITCHES_SELECTOR
                }
            )
        case Membership.LABEL:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_LABEL,
                        description={"suggested_value": defaults.get(CONF_LABEL)},
                    ): selector.LabelSelector()
                }
            )
        case Membership.AREA:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_AREA,
                        description={"suggested_value": defaults.get(CONF_AREA)},
                    ): selector.AreaSelector()
                }
            )


def latch_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step 3a: a latch needs a bound, or a missed close pins the switches."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_MAX_HOLD,
                default=defaults.get(CONF_MAX_HOLD, {"hours": 12, "minutes": 0, "seconds": 0}),
            ): DURATION_SELECTOR,
            vol.Optional(
                CONF_CONDITION,
                description={"suggested_value": defaults.get(CONF_CONDITION)},
            ): selector.TemplateSelector(),
        }
    )


def boundary_kind_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Ask how one edge of a window is expressed."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_KIND, default=defaults.get(CONF_KIND, BoundaryKind.TIME.value)
            ): _select(tuple(k.value for k in BoundaryKind), CONF_KIND)
        }
    )


def boundary_schema(
    kind: BoundaryKind, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Ask for the value that edge needs."""
    defaults = defaults or {}
    offset = vol.Optional(
        CONF_OFFSET, default=defaults.get(CONF_OFFSET, {"hours": 0, "minutes": 0, "seconds": 0})
    )
    match kind:
        case BoundaryKind.TIME:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_TIME,
                        description={"suggested_value": defaults.get(CONF_TIME)},
                    ): selector.TimeSelector()
                }
            )
        case BoundaryKind.SUN:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_EVENT, default=defaults.get(CONF_EVENT, "sunset")
                    ): _select(SUN_EVENTS, CONF_EVENT),
                    offset: selector.DurationSelector(
                        selector.DurationSelectorConfig(allow_negative=True)
                    ),
                }
            )
        case BoundaryKind.ENTITY:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_ENTITY_ID,
                        description={"suggested_value": defaults.get(CONF_ENTITY_ID)},
                    ): BOUNDARY_ENTITY_SELECTOR,
                    offset: selector.DurationSelector(
                        selector.DurationSelectorConfig(allow_negative=True)
                    ),
                }
            )


def window_extras_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step 3b tail: optional weekday filter and condition."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_WEEKDAYS,
                description={"suggested_value": defaults.get(CONF_WEEKDAYS, [])},
            ): _select(WEEKDAYS, CONF_WEEKDAYS, multiple=True),
            vol.Optional(
                CONF_CONDITION,
                description={"suggested_value": defaults.get(CONF_CONDITION)},
            ): selector.TemplateSelector(),
        }
    )


def switch_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """The managed-switch form."""
    defaults = defaults or {}
    schema: dict[Any, Any] = {}
    if CONF_ENTITY_ID not in defaults:
        schema[vol.Required(CONF_ENTITY_ID)] = SWITCH_SELECTOR
    schema.update(
        {
            vol.Required(
                CONF_MODE, default=defaults.get(CONF_MODE, Mode.ADVISORY.value)
            ): _select(tuple(m.value for m in Mode), CONF_MODE),
            vol.Required(
                CONF_DEFAULT_STATE,
                default=defaults.get(CONF_DEFAULT_STATE, DesiredState.OFF.value),
            ): _select(tuple(s.value for s in DesiredState), CONF_DEFAULT_STATE),
            vol.Optional(
                CONF_OVERRIDE_TIMEOUT,
                description={"suggested_value": defaults.get(CONF_OVERRIDE_TIMEOUT)},
            ): DURATION_SELECTOR,
        }
    )
    return vol.Schema(schema)


def source_schema(
    reason_names: list[str], defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """The automation-to-reason mapping form."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AUTOMATION,
                description={"suggested_value": defaults.get(CONF_AUTOMATION)},
            ): AUTOMATION_SELECTOR,
            vol.Required(
                CONF_ACTION, default=defaults.get(CONF_ACTION, SourceAction.OPENS.value)
            ): _select(tuple(a.value for a in SourceAction), CONF_ACTION),
            vol.Required(
                CONF_REASON,
                default=defaults.get(CONF_REASON, reason_names[0] if reason_names else ""),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=reason_names, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


# -- stored dict -> model -----------------------------------------------------


def _duration_to_dict(value: timedelta) -> dict[str, int]:
    total = int(value.total_seconds())
    return {"hours": total // 3600, "minutes": (total % 3600) // 60, "seconds": total % 60}


def _to_timedelta(value: Any) -> timedelta | None:
    """Accept whatever a DurationSelector, YAML or storage hands us."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    try:
        return cv.time_period(value)
    except vol.Invalid:
        _LOGGER.warning("Ignoring unparseable duration %r", value)
        return None


def boundary_from_dict(data: dict[str, Any] | None) -> Boundary | None:
    """Build a Boundary from its stored form."""
    if not data:
        return None
    kind = BoundaryKind(data[CONF_KIND])
    offset = _to_timedelta(data.get(CONF_OFFSET)) or timedelta()
    at = None
    if raw_time := data.get(CONF_TIME):
        at = cv.time(raw_time)
    return Boundary(
        kind=kind,
        offset=offset,
        at=at,
        event=data.get(CONF_EVENT),
        entity_id=data.get(CONF_ENTITY_ID),
    )


def reason_from_dict(data: dict[str, Any]) -> ReasonDef:
    """Build a ReasonDef from a subentry or an imported YAML block."""
    weekdays = frozenset(
        WEEKDAYS.index(day) for day in data.get(CONF_WEEKDAYS, []) if day in WEEKDAYS
    )
    return ReasonDef(
        name=data[CONF_NAME],
        priority=int(data.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
        state=DesiredState(data.get(CONF_STATE, DesiredState.ON.value)),
        lifetime=Lifetime(data[CONF_LIFETIME]),
        membership=Membership(data.get(CONF_MEMBERSHIP, Membership.ENTITIES.value)),
        entities=tuple(data.get(CONF_ENTITIES, ()) or ()),
        label=data.get(CONF_LABEL),
        area=data.get(CONF_AREA),
        max_hold=_to_timedelta(data.get(CONF_MAX_HOLD)),
        start=boundary_from_dict(data.get(CONF_START)),
        end=boundary_from_dict(data.get(CONF_END)),
        weekdays=weekdays,
        condition=data.get(CONF_CONDITION),
    )


def switch_from_dict(data: dict[str, Any], default_mode: Mode) -> SwitchConfig:
    """Build a SwitchConfig from a subentry or an imported YAML block."""
    return SwitchConfig(
        entity_id=data[CONF_ENTITY_ID],
        mode=Mode(data.get(CONF_MODE, default_mode.value)),
        default_state=DesiredState(data.get(CONF_DEFAULT_STATE, DesiredState.OFF.value)),
        override_timeout=_to_timedelta(data.get(CONF_OVERRIDE_TIMEOUT)),
    )


def source_from_dict(data: dict[str, Any]) -> SourceMap:
    """Build a SourceMap from a subentry or an imported YAML block."""
    return SourceMap(
        automation=data[CONF_AUTOMATION],
        action=SourceAction(data[CONF_ACTION]),
        reason=data[CONF_REASON],
    )


def globals_from_dict(data: dict[str, Any]) -> GlobalConfig:
    """Build the instance-wide settings."""
    return GlobalConfig(
        bypass_entity=data.get(CONF_BYPASS_ENTITY),
        default_mode=Mode(data.get(CONF_DEFAULT_MODE, Mode.ADVISORY.value)),
        override_timeout=_to_timedelta(data.get(CONF_OVERRIDE_TIMEOUT))
        or DEFAULT_OVERRIDE_TIMEOUT,
        override_priority=int(data.get(CONF_OVERRIDE_PRIORITY, DEFAULT_OVERRIDE_PRIORITY)),
        override_sources=frozenset(data.get(CONF_OVERRIDE_SOURCES, []) or []),
        unknown_source_priority=int(
            data.get(CONF_UNKNOWN_SOURCE_PRIORITY, DEFAULT_UNKNOWN_SOURCE_PRIORITY)
        ),
        ignore_unknown_sources=bool(data.get(CONF_IGNORE_UNKNOWN_SOURCES, False)),
        tie_break=TieBreak(data.get(CONF_TIE_BREAK, TieBreak.LAST_WINS.value)),
    )


def build_config(hass: HomeAssistant, entry: ConfigEntry) -> ArbiterConfig:
    """Assemble the whole configuration from an entry and its subentries."""
    globals_ = globals_from_dict({**entry.data, **entry.options})
    config = ArbiterConfig(globals=globals_)

    for subentry in entry.subentries.values():
        data = dict(subentry.data)
        try:
            match subentry.subentry_type:
                case type_ if type_ == SUBENTRY_REASON:
                    reason = reason_from_dict(data)
                    config.reasons[reason.name] = reason
                case type_ if type_ == SUBENTRY_SWITCH:
                    switch = switch_from_dict(data, globals_.default_mode)
                    config.switches[switch.entity_id] = switch
                case type_ if type_ == SUBENTRY_SOURCE:
                    source = source_from_dict(data)
                    config.sources.setdefault(source.automation, ())
                    config.sources[source.automation] += (source,)
        except (KeyError, ValueError) as err:
            _LOGGER.error(
                "Skipping invalid %s subentry %r: %s",
                subentry.subentry_type,
                subentry.title,
                err,
            )

    return config


# -- YAML import --------------------------------------------------------------

BOUNDARY_YAML = vol.Schema(
    {
        vol.Required(CONF_KIND): vol.In([k.value for k in BoundaryKind]),
        vol.Optional(CONF_TIME): cv.string,
        vol.Optional(CONF_EVENT): vol.In(SUN_EVENTS),
        vol.Optional(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_OFFSET): cv.time_period,
    }
)

REASON_YAML = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_LIFETIME): vol.In([x.value for x in Lifetime]),
        vol.Optional(CONF_PRIORITY, default=DEFAULT_PRIORITY): vol.All(int, vol.Range(0, 100)),
        vol.Optional(CONF_STATE, default=DesiredState.ON.value): vol.In(
            [DesiredState.ON.value, DesiredState.OFF.value]
        ),
        vol.Optional(CONF_MEMBERSHIP, default=Membership.ENTITIES.value): vol.In(
            [m.value for m in Membership]
        ),
        vol.Optional(CONF_ENTITIES, default=[]): cv.entity_ids,
        vol.Optional(CONF_LABEL): cv.string,
        vol.Optional(CONF_AREA): cv.string,
        vol.Optional(CONF_MAX_HOLD): cv.time_period,
        vol.Optional(CONF_START): BOUNDARY_YAML,
        vol.Optional(CONF_END): BOUNDARY_YAML,
        vol.Optional(CONF_WEEKDAYS, default=[]): [vol.In(WEEKDAYS)],
        vol.Optional(CONF_CONDITION): cv.template,
    }
)

SWITCH_YAML = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_MODE): vol.In([m.value for m in Mode]),
        vol.Optional(CONF_DEFAULT_STATE, default=DesiredState.OFF.value): vol.In(
            [s.value for s in DesiredState]
        ),
        vol.Optional(CONF_OVERRIDE_TIMEOUT): cv.time_period,
    }
)

SOURCE_YAML = vol.Schema(
    {
        vol.Required(CONF_AUTOMATION): cv.entity_id,
        vol.Required(CONF_ACTION): vol.In([a.value for a in SourceAction]),
        vol.Required(CONF_REASON): cv.string,
    }
)

CONFIG_YAML = vol.Schema(
    {
        vol.Optional(CONF_BYPASS_ENTITY): cv.entity_id,
        vol.Optional(CONF_DEFAULT_MODE, default=Mode.ADVISORY.value): vol.In(
            [m.value for m in Mode]
        ),
        vol.Optional(CONF_OVERRIDE_TIMEOUT): cv.time_period,
        vol.Optional(CONF_OVERRIDE_PRIORITY, default=DEFAULT_OVERRIDE_PRIORITY): vol.All(
            int, vol.Range(0, 100)
        ),
        vol.Optional(CONF_OVERRIDE_SOURCES, default=[]): cv.entity_ids,
        vol.Optional(
            CONF_UNKNOWN_SOURCE_PRIORITY, default=DEFAULT_UNKNOWN_SOURCE_PRIORITY
        ): vol.All(int, vol.Range(0, 100)),
        vol.Optional(CONF_IGNORE_UNKNOWN_SOURCES, default=False): cv.boolean,
        vol.Optional(CONF_TIE_BREAK, default=TieBreak.LAST_WINS.value): vol.In(
            [t.value for t in TieBreak]
        ),
        vol.Optional(CONF_REASONS, default=[]): [REASON_YAML],
        vol.Optional(CONF_SWITCHES, default=[]): [SWITCH_YAML],
        vol.Optional(CONF_SOURCES, default=[]): [SOURCE_YAML],
    }
)

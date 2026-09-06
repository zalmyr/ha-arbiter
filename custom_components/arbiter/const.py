"""Constants for the Arbiter integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "arbiter"

# Config entry / subentry keys -------------------------------------------------

SUBENTRY_REASON: Final = "reason"
SUBENTRY_SWITCH: Final = "switch"
SUBENTRY_SOURCE: Final = "source"

CONF_BYPASS_ENTITY: Final = "bypass_entity"
CONF_DEFAULT_MODE: Final = "default_mode"
CONF_OVERRIDE_TIMEOUT: Final = "override_timeout"
CONF_OVERRIDE_PRIORITY: Final = "override_priority"
CONF_OVERRIDE_SOURCES: Final = "override_sources"
CONF_UNKNOWN_SOURCE_PRIORITY: Final = "unknown_source_priority"
CONF_IGNORE_UNKNOWN_SOURCES: Final = "ignore_unknown_sources"
CONF_TIE_BREAK: Final = "tie_break"

CONF_NAME: Final = "name"
CONF_PRIORITY: Final = "priority"
CONF_STATE: Final = "state"
CONF_MEMBERSHIP: Final = "membership"
CONF_ENTITIES: Final = "entities"
CONF_LABEL: Final = "label"
CONF_AREA: Final = "area"
CONF_LIFETIME: Final = "lifetime"
CONF_MAX_HOLD: Final = "max_hold"
CONF_START: Final = "start"
CONF_END: Final = "end"
CONF_CONDITION: Final = "condition"

CONF_KIND: Final = "kind"
CONF_TIME: Final = "time"
CONF_EVENT: Final = "event"
CONF_OFFSET: Final = "offset"
CONF_ENTITY_ID: Final = "entity_id"

CONF_MODE: Final = "mode"
CONF_DEFAULT_STATE: Final = "default_state"

CONF_AUTOMATION: Final = "automation"
CONF_ACTION: Final = "action"
CONF_REASON: Final = "reason"

CONF_REASONS: Final = "reasons"
CONF_SWITCHES: Final = "switches"
CONF_SOURCES: Final = "sources"

# Defaults ---------------------------------------------------------------------

DEFAULT_OVERRIDE_TIMEOUT: Final = timedelta(minutes=90)
DEFAULT_OVERRIDE_PRIORITY: Final = 100
DEFAULT_UNKNOWN_SOURCE_PRIORITY: Final = 10
DEFAULT_PRIORITY: Final = 50

#: The arbiter debounces applies so a burst of reason changes yields one call.
APPLY_DEBOUNCE_SECONDS: Final = 0.25

#: How long a context id issued by the arbiter is remembered, so the resulting
#: ``state_changed`` is recognised as our own write rather than someone's action.
SELF_CONTEXT_TTL: Final = timedelta(seconds=30)

#: How long a context id is mapped back to the automation that created it.
ATTRIBUTION_TTL: Final = timedelta(minutes=5)

#: Bound on both caches, so a busy instance cannot grow them without limit.
CACHE_MAX_ENTRIES: Final = 2048

#: Conflicts kept per switch for ``arbiter.explain``.
CONFLICT_LOG_SIZE: Final = 20

# Storage ----------------------------------------------------------------------

STORAGE_KEY: Final = f"{DOMAIN}.state"
STORAGE_VERSION: Final = 1

# Events -----------------------------------------------------------------------

EVENT_OPENED: Final = f"{DOMAIN}_opened"
EVENT_CLOSED: Final = f"{DOMAIN}_closed"
EVENT_APPLIED: Final = f"{DOMAIN}_applied"
EVENT_CONFLICT: Final = f"{DOMAIN}_conflict"
EVENT_OVERRIDE: Final = f"{DOMAIN}_override"
EVENT_REVERTED: Final = f"{DOMAIN}_reverted"

# Services ---------------------------------------------------------------------

SERVICE_OPEN: Final = "open"
SERVICE_CLOSE: Final = "close"
SERVICE_CLOSE_ALL: Final = "close_all"
SERVICE_OVERRIDE: Final = "override"
SERVICE_CLEAR_OVERRIDES: Final = "clear_overrides"
SERVICE_SET_MODE: Final = "set_mode"
SERVICE_EXPLAIN: Final = "explain"
SERVICE_FIND_CONFLICTS: Final = "find_conflicts"
SERVICE_EXPORT_CONFIG: Final = "export_config"

ATTR_TARGETS: Final = "targets"
ATTR_DURATION: Final = "duration"
ATTR_UNTIL: Final = "until"

#: Reserved reason name for human/external overrides.
MANUAL_REASON: Final = "manual"


class Mode(StrEnum):
    """How far the arbiter is allowed to go for a given switch."""

    ADVISORY = "advisory"
    """Never write. Track reasons and report what would have happened."""

    GUARD = "guard"
    """Map observed automation commands onto reasons and revert the losers."""

    CLAIMS = "claims"
    """Only the arbiter writes; automations call arbiter.open / arbiter.close."""


class DesiredState(StrEnum):
    """What a reason (or a fallback) asks a switch to do."""

    ON = "on"
    OFF = "off"
    UNMANAGED = "unmanaged"
    """Express no opinion — used as a fallback so the arbiter leaves a switch alone."""


class Lifetime(StrEnum):
    """How a reason stops being live."""

    WINDOW = "window"
    """Self-closing: computed from an independent start and end."""

    LATCH = "latch"
    """Opened by one actor and closed by another, bounded by max_hold."""


class Origin(StrEnum):
    """Where a live reason came from, which decides whether it survives a restart."""

    WINDOW = "window"
    LATCH = "latch"
    SERVICE = "service"
    OVERRIDE = "override"
    UNKNOWN = "unknown"


class TieBreak(StrEnum):
    """How to resolve two live reasons of equal priority that disagree."""

    LAST_WINS = "last_wins"
    FIRST_WINS = "first_wins"
    OFF_WINS = "off_wins"


class Membership(StrEnum):
    """How a reason names the switches it applies to."""

    ENTITIES = "entities"
    LABEL = "label"
    AREA = "area"


class SourceAction(StrEnum):
    """What an observed automation command does to its mapped reason."""

    OPENS = "opens"
    CLOSES = "closes"


class BoundaryKind(StrEnum):
    """How one edge of a window is specified."""

    TIME = "time"
    """A wall-clock time, e.g. 02:00:00."""

    SUN = "sun"
    """A sun event plus an offset, e.g. sunset + 00:25:00."""

    ENTITY = "entity"
    """An input_datetime or timestamp sensor plus an offset."""


class Attribution(StrEnum):
    """Who caused a state change the arbiter did not make itself."""

    HUMAN = "human"
    """A person, via the UI/app (context carried a user_id) or a declared override source."""

    AUTOMATION = "automation"
    EXTERNAL = "external"
    """No user and no attributable automation — device-local or direct Z-Wave."""


PLATFORMS: Final = ["binary_sensor", "sensor", "switch"]

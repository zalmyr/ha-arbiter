"""Resolve a window boundary to a point in time.

Deliberately mirrors what Home Assistant's own time trigger accepts, so a boundary
can be written the same way the existing automations write their triggers: a
wall-clock time, a sun event plus an offset, or an ``input_datetime`` / timestamp
``sensor`` plus an offset. That last form is the one that matters here — the
calendar sensors this instance runs on hold a different absolute time every day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import BoundaryKind
from .models import Boundary

_LOGGER = logging.getLogger(__name__)

#: Boundaries that name a specific moment rather than a time of day. Shifting the
#: date on one is meaningless: a sensor holding today's sunset time names a
#: specific moment, not a pattern that can be re-projected onto another day.
ABSOLUTE_KINDS = (BoundaryKind.ENTITY,)


@callback
def async_resolve_boundary(
    hass: HomeAssistant, boundary: Boundary, day: date
) -> datetime | None:
    """Return when ``boundary`` falls on ``day``, as an aware UTC datetime.

    ``day`` is a local date and is ignored for boundaries that already name an
    absolute moment. Returns None when the boundary cannot be resolved right now —
    an entity that is missing, unavailable, or holding something unparseable.
    """
    match boundary.kind:
        case BoundaryKind.TIME:
            assert boundary.at is not None
            naive = datetime.combine(day, boundary.at)
            resolved = dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))

        case BoundaryKind.SUN:
            assert boundary.event is not None
            event_dt = get_astral_event_date(hass, boundary.event, day)
            if event_dt is None:
                _LOGGER.debug("No %s on %s at this location", boundary.event, day)
                return None
            resolved = dt_util.as_utc(event_dt)

        case BoundaryKind.ENTITY:
            assert boundary.entity_id is not None
            resolved_or_none = _resolve_entity(hass, boundary.entity_id, day)
            if resolved_or_none is None:
                return None
            resolved = resolved_or_none

    return resolved + boundary.offset


def _resolve_entity(hass: HomeAssistant, entity_id: str, day: date) -> datetime | None:
    """Resolve an input_datetime or timestamp sensor to an aware UTC datetime."""
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug("Boundary entity %s is not usable yet", entity_id)
        return None

    domain = entity_id.split(".", 1)[0]

    if domain == "input_datetime":
        has_date = state.attributes.get("has_date", False)
        has_time = state.attributes.get("has_time", True)
        if has_date:
            parsed = dt_util.parse_datetime(state.state)
            return dt_util.as_utc(parsed) if parsed else None
        if not has_time:
            return None
        # A time-only helper is a time of day, so it projects onto ``day``.
        parsed_time = dt_util.parse_time(state.state)
        if parsed_time is None:
            return None
        naive = datetime.combine(day, parsed_time)
        return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))

    if state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.TIMESTAMP:
        parsed = dt_util.parse_datetime(state.state)
        return dt_util.as_utc(parsed) if parsed else None

    # Tolerate a plain sensor that happens to hold a parseable datetime or time,
    # which is common for calendar-derived helpers that predate device classes.
    if (parsed := dt_util.parse_datetime(state.state)) is not None:
        return dt_util.as_utc(parsed)
    if (parsed_time := dt_util.parse_time(state.state)) is not None:
        naive = datetime.combine(day, parsed_time)
        return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))

    _LOGGER.warning(
        "Boundary entity %s holds %r, which is not a time or timestamp",
        entity_id,
        state.state,
    )
    return None


@callback
def async_boundary_entities(boundary: Boundary | None) -> set[str]:
    """Return the entities a boundary depends on, so changes can be watched.

    The calendar sensors move every day; without watching them a window computed
    at startup would keep using yesterday's value.
    """
    if boundary is not None and boundary.kind is BoundaryKind.ENTITY:
        assert boundary.entity_id is not None
        return {boundary.entity_id}
    return set()


def candidate_days(now: datetime) -> list[date]:
    """Return the local dates a window in progress could have started on.

    Yesterday is included because a window that crosses midnight is still open in
    the small hours; tomorrow because the next transition may belong to it.
    """
    today = dt_util.as_local(now).date()
    return [today - timedelta(days=1), today, today + timedelta(days=1)]

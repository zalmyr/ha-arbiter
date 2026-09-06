"""Evaluate window reasons: is this one open, and when does that change?

A window reason is the answer to "different on and off schedules, while still
knowing whether the lights should be on". Its start and end are specified
independently — 07:00 to an ``input_datetime``, or sunset + 25 minutes to a
timestamp sensor — and between them the reason simply *is* live. Nothing has to
fire twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant, callback

from .models import ReasonDef
from .schedule_time import async_boundary_entities, async_resolve_boundary, candidate_days

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WindowState:
    """Where a window reason stands at a given moment."""

    is_open: bool
    closes_at: datetime | None = None
    next_transition: datetime | None = None


@callback
def async_evaluate_window(
    hass: HomeAssistant, reason: ReasonDef, now: datetime
) -> WindowState:
    """Return whether ``reason``'s window is open at ``now``, and what happens next.

    Candidate windows are built for yesterday, today and tomorrow so a window that
    crosses midnight is handled without special cases.
    """
    assert reason.start is not None and reason.end is not None

    intervals: set[tuple[datetime, datetime]] = set()
    for day in candidate_days(now):
        start = async_resolve_boundary(hass, reason.start, day)
        if start is None:
            continue

        # The weekday filter is about the day the window *starts* on, judged in
        # local time, so a Saturday-evening window that runs past midnight counts
        # as Saturday's.
        if reason.weekdays:
            from homeassistant.util import dt as dt_util

            if dt_util.as_local(start).weekday() not in reason.weekdays:
                continue

        end = async_resolve_boundary(hass, reason.end, day)
        if end is None:
            continue
        if end <= start:
            # The window runs past midnight.
            end += timedelta(days=1)

        intervals.add((start, end))

    if not intervals:
        return WindowState(is_open=False)

    open_interval = next((iv for iv in intervals if iv[0] <= now < iv[1]), None)

    edges = sorted(
        edge for interval in intervals for edge in interval if edge > now
    )
    next_transition = edges[0] if edges else None

    return WindowState(
        is_open=open_interval is not None,
        closes_at=open_interval[1] if open_interval else None,
        next_transition=next_transition,
    )


@callback
def async_window_entities(reason: ReasonDef) -> set[str]:
    """Return the entities whose changes should re-evaluate this window.

    The calendar sensors hold a new absolute time every day, so a window built on
    them has to be recomputed when they update or it keeps using yesterday's value.
    """
    return async_boundary_entities(reason.start) | async_boundary_entities(reason.end)

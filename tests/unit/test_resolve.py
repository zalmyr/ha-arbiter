"""The resolution rule on its own, with no Home Assistant involved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.arbiter.const import (
    BoundaryKind,
    DesiredState,
    Lifetime,
    Origin,
    TieBreak,
)
from custom_components.arbiter.models import Boundary, LiveReason, ReasonDef, resolve

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
SWITCH = "switch.porch"


def live(
    name: str,
    priority: int,
    state: DesiredState,
    *,
    opened: datetime = NOW,
    expires: datetime | None = None,
    targets: frozenset[str] = frozenset({SWITCH}),
) -> LiveReason:
    return LiveReason(
        name=name,
        priority=priority,
        state=state,
        origin=Origin.LATCH,
        targets=targets,
        opened=opened,
        expires=expires,
    )


def test_no_reasons_falls_back_to_default():
    decision = resolve([], SWITCH, DesiredState.OFF, TieBreak.LAST_WINS, NOW)
    assert decision.state is DesiredState.OFF
    assert decision.winner is None
    assert not decision.is_conflict


def test_unmanaged_default_expresses_no_opinion():
    decision = resolve([], SWITCH, DesiredState.UNMANAGED, TieBreak.LAST_WINS, NOW)
    assert decision.state is DesiredState.UNMANAGED


def test_highest_priority_wins():
    reasons = [
        live("daily", 20, DesiredState.ON),
        live("night_lockout", 90, DesiredState.OFF),
    ]
    decision = resolve(reasons, SWITCH, DesiredState.OFF, TieBreak.LAST_WINS, NOW)
    assert decision.winner.name == "night_lockout"
    assert decision.state is DesiredState.OFF
    assert decision.is_conflict
    assert [r.name for r in decision.losers] == ["daily"]


def test_reason_that_does_not_name_the_switch_is_ignored():
    reasons = [live("elsewhere", 99, DesiredState.ON, targets=frozenset({"switch.other"}))]
    decision = resolve(reasons, SWITCH, DesiredState.OFF, TieBreak.LAST_WINS, NOW)
    assert decision.winner is None
    assert decision.state is DesiredState.OFF


def test_expired_reason_is_ignored():
    reasons = [
        live("stale", 90, DesiredState.ON, expires=NOW - timedelta(minutes=1)),
        live("daily", 20, DesiredState.OFF),
    ]
    decision = resolve(reasons, SWITCH, DesiredState.ON, TieBreak.LAST_WINS, NOW)
    assert decision.winner.name == "daily"


@pytest.mark.parametrize(
    ("tie_break", "expected"),
    [
        (TieBreak.LAST_WINS, "newer"),
        (TieBreak.FIRST_WINS, "older"),
        (TieBreak.OFF_WINS, "older"),
    ],
)
def test_tie_breaks(tie_break: TieBreak, expected: str):
    reasons = [
        live("older", 50, DesiredState.OFF, opened=NOW - timedelta(hours=1)),
        live("newer", 50, DesiredState.ON, opened=NOW),
    ]
    decision = resolve(reasons, SWITCH, DesiredState.OFF, tie_break, NOW)
    assert decision.winner.name == expected


def test_off_wins_only_applies_at_the_top_priority():
    reasons = [
        live("low_off", 10, DesiredState.OFF),
        live("high_on", 80, DesiredState.ON),
    ]
    decision = resolve(reasons, SWITCH, DesiredState.OFF, TieBreak.OFF_WINS, NOW)
    assert decision.winner.name == "high_on"
    assert decision.state is DesiredState.ON


def test_agreeing_reasons_are_not_a_conflict():
    reasons = [
        live("daily", 20, DesiredState.ON),
        live("party", 50, DesiredState.ON),
    ]
    decision = resolve(reasons, SWITCH, DesiredState.OFF, TieBreak.LAST_WINS, NOW)
    assert decision.winner.name == "party"
    assert not decision.is_conflict


def test_latch_without_max_hold_is_rejected():
    """A latch with no bound is the stuck-boolean failure mode, so it is not allowed."""
    with pytest.raises(ValueError, match="max_hold"):
        ReasonDef(
            name="evening",
            priority=70,
            state=DesiredState.ON,
            lifetime=Lifetime.LATCH,
        )


def test_window_without_edges_is_rejected():
    with pytest.raises(ValueError, match="needs a start and an end"):
        ReasonDef(
            name="daily",
            priority=20,
            state=DesiredState.ON,
            lifetime=Lifetime.WINDOW,
        )


def test_boundary_requires_the_field_its_kind_uses():
    with pytest.raises(ValueError, match="missing its value"):
        Boundary(kind=BoundaryKind.ENTITY)

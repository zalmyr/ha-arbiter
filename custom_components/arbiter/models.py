"""Data model for Arbiter, plus the pure resolution rule.

Nothing in this module touches Home Assistant, so the resolution rule — the part
that decides what a switch should be doing — can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from .const import (
    BoundaryKind,
    DesiredState,
    Lifetime,
    Membership,
    Mode,
    Origin,
    SourceAction,
    TieBreak,
)


@dataclass(frozen=True, slots=True)
class Boundary:
    """One edge of a window.

    Mirrors the shape Home Assistant's own time trigger accepts, so a boundary can
    be a wall-clock time, a sun event plus an offset, or an ``input_datetime`` /
    timestamp ``sensor`` plus an offset. The last form is what lets a window track
    entities whose value moves every day.
    """

    kind: BoundaryKind
    offset: timedelta = timedelta()
    at: time | None = None
    event: str | None = None
    entity_id: str | None = None

    def __post_init__(self) -> None:
        """Reject a boundary that is missing the field its kind depends on."""
        required = {
            BoundaryKind.TIME: self.at,
            BoundaryKind.SUN: self.event,
            BoundaryKind.ENTITY: self.entity_id,
        }[self.kind]
        if required is None:
            raise ValueError(f"{self.kind} boundary is missing its value")


@dataclass(frozen=True, slots=True)
class ReasonDef:
    """A configured reason: why a set of switches should be on (or off)."""

    name: str
    priority: int
    state: DesiredState
    lifetime: Lifetime
    membership: Membership = Membership.ENTITIES
    entities: tuple[str, ...] = ()
    label: str | None = None
    area: str | None = None
    max_hold: timedelta | None = None
    start: Boundary | None = None
    end: Boundary | None = None
    #: Weekdays (0 = Monday) the window may start on. Empty means every day.
    #: Kept separate from ``condition`` because a template is only re-evaluated
    #: when an entity changes, and "it is Saturday" changes with the clock alone.
    weekdays: frozenset[int] = frozenset()
    condition: str | None = None

    def __post_init__(self) -> None:
        """Reject a reason whose lifetime is missing the fields it needs."""
        if self.state not in (DesiredState.ON, DesiredState.OFF):
            raise ValueError("a reason must ask for 'on' or 'off'")
        if self.lifetime is Lifetime.WINDOW and (self.start is None or self.end is None):
            raise ValueError(f"window reason {self.name!r} needs a start and an end")
        if self.lifetime is Lifetime.LATCH and self.max_hold is None:
            raise ValueError(
                f"latch reason {self.name!r} needs max_hold: without it a missed "
                "close pins the switches indefinitely"
            )


@dataclass(frozen=True, slots=True)
class LiveReason:
    """A reason that is currently in force.

    ``targets`` is resolved at open time, so a reason keeps acting on the switches
    it applied to when it opened even if a label's membership changes underneath it.
    """

    name: str
    priority: int
    state: DesiredState
    origin: Origin
    targets: frozenset[str]
    opened: datetime
    expires: datetime | None = None
    source: str | None = None

    def is_live(self, now: datetime) -> bool:
        """Return True if this reason has not expired at ``now``."""
        return self.expires is None or self.expires > now

    def with_expiry(self, expires: datetime | None) -> LiveReason:
        """Return a copy with a different expiry."""
        return replace(self, expires=expires)

    def without_targets(self, targets: Iterable[str]) -> LiveReason | None:
        """Return a copy that no longer covers ``targets``.

        Returns None when nothing is left, which the caller treats as the reason
        being closed outright.
        """
        remaining = self.targets - frozenset(targets)
        return replace(self, targets=remaining) if remaining else None


@dataclass(frozen=True, slots=True)
class SwitchConfig:
    """A switch the arbiter manages."""

    entity_id: str
    mode: Mode = Mode.ADVISORY
    default_state: DesiredState = DesiredState.OFF
    override_timeout: timedelta | None = None


@dataclass(frozen=True, slots=True)
class SourceMap:
    """An automation mapped onto the reason its commands open or close."""

    automation: str
    action: SourceAction
    reason: str


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """Instance-wide settings from the config entry."""

    bypass_entity: str | None = None
    default_mode: Mode = Mode.ADVISORY
    override_timeout: timedelta = timedelta(minutes=90)
    override_priority: int = 100
    override_sources: frozenset[str] = frozenset()
    unknown_source_priority: int = 10
    ignore_unknown_sources: bool = False
    tie_break: TieBreak = TieBreak.LAST_WINS


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of resolving every live reason for one switch."""

    entity_id: str
    state: DesiredState
    winner: LiveReason | None = None
    considered: tuple[LiveReason, ...] = ()
    losers: tuple[LiveReason, ...] = ()

    @property
    def is_conflict(self) -> bool:
        """True when a live reason wanted the opposite of what won."""
        return bool(self.losers)


@dataclass
class ConflictRecord:
    """One observed disagreement, kept for ``arbiter.explain``."""

    when: datetime
    entity_id: str
    winner: str
    winner_state: str
    losers: tuple[str, ...] = field(default_factory=tuple)


def _sort_key(reason: LiveReason) -> tuple[datetime, str]:
    """Order reasons deterministically, oldest first."""
    return (reason.opened, reason.name)


def resolve(
    reasons: list[LiveReason],
    entity_id: str,
    default_state: DesiredState,
    tie_break: TieBreak,
    now: datetime,
) -> Decision:
    """Decide what ``entity_id`` should be doing.

    Highest priority among the live reasons that name this switch wins. Equal
    priorities are broken by ``tie_break``. With nothing live the switch falls back
    to ``default_state``, which may be ``unmanaged`` to leave it alone entirely.
    """
    candidates = sorted(
        (r for r in reasons if r.is_live(now) and entity_id in r.targets),
        key=_sort_key,
    )
    if not candidates:
        return Decision(entity_id=entity_id, state=default_state)

    top_priority = max(r.priority for r in candidates)
    top = [r for r in candidates if r.priority == top_priority]

    if len(top) == 1 or tie_break is TieBreak.FIRST_WINS:
        winner = top[0]
    elif tie_break is TieBreak.OFF_WINS:
        # Safety-first: if anything at the top priority wants off, off wins.
        off = [r for r in top if r.state is DesiredState.OFF]
        winner = off[-1] if off else top[-1]
    else:  # LAST_WINS
        winner = top[-1]

    losers = tuple(r for r in candidates if r.state is not winner.state)
    return Decision(
        entity_id=entity_id,
        state=winner.state,
        winner=winner,
        considered=tuple(candidates),
        losers=losers,
    )

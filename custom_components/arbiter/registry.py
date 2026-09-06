"""The reason store: what is live right now, and what the config says.

This is the part that turns one-shot automation commands into standing opinions.
An automation that fires at 07:00 and is never heard from again still holds its
reason open until the reason's own lifetime ends — which is what stops a later,
unrelated automation from winning by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging

from .const import Lifetime, Mode, Origin
from .models import (
    GlobalConfig,
    LiveReason,
    ReasonDef,
    SourceMap,
    SwitchConfig,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ArbiterConfig:
    """Everything the config entry and its subentries add up to."""

    globals: GlobalConfig = field(default_factory=GlobalConfig)
    reasons: dict[str, ReasonDef] = field(default_factory=dict)
    switches: dict[str, SwitchConfig] = field(default_factory=dict)
    sources: dict[str, tuple[SourceMap, ...]] = field(default_factory=dict)

    def mode_for(self, entity_id: str) -> Mode:
        """Return the mode governing ``entity_id``."""
        switch = self.switches.get(entity_id)
        return switch.mode if switch else self.globals.default_mode

    def validate(self) -> list[str]:
        """Return human-readable problems with this config.

        Surfaced through diagnostics and logged at setup rather than raised, so one
        bad subentry cannot stop the rest of the integration from loading.
        """
        problems: list[str] = []
        for automation, maps in self.sources.items():
            for source in maps:
                reason = self.reasons.get(source.reason)
                if reason is None:
                    problems.append(
                        f"{automation} is mapped to unknown reason {source.reason!r}"
                    )
                elif reason.lifetime is Lifetime.WINDOW:
                    problems.append(
                        f"{automation} is mapped to {source.reason!r}, which is a "
                        "window reason and opens and closes on its own schedule; "
                        "the mapping will be ignored"
                    )
        return problems


class ReasonStore:
    """The live reasons, keyed by name."""

    def __init__(self) -> None:
        self._live: dict[str, LiveReason] = {}

    def __contains__(self, name: str) -> bool:
        return name in self._live

    def __len__(self) -> int:
        return len(self._live)

    def get(self, name: str) -> LiveReason | None:
        """Return a live reason by name, if it is open."""
        return self._live.get(name)

    def all(self) -> list[LiveReason]:
        """Return every open reason, expired or not."""
        return list(self._live.values())

    def live(self, now: datetime) -> list[LiveReason]:
        """Return the reasons still in force at ``now``."""
        return [r for r in self._live.values() if r.is_live(now)]

    def open(self, reason: LiveReason) -> LiveReason | None:
        """Open (or re-open) a reason; return what it replaced, if anything."""
        previous = self._live.get(reason.name)
        self._live[reason.name] = reason
        return previous

    def close(self, name: str) -> LiveReason | None:
        """Close a reason; return it if it was open."""
        return self._live.pop(name, None)

    def close_all(self, *, origins: set[Origin] | None = None) -> list[LiveReason]:
        """Close every reason, or only those with the given origins."""
        if origins is None:
            closed = list(self._live.values())
            self._live.clear()
            return closed
        closed = [r for r in self._live.values() if r.origin in origins]
        for reason in closed:
            del self._live[reason.name]
        return closed

    def purge_expired(self, now: datetime) -> list[LiveReason]:
        """Drop reasons whose time is up; return them."""
        expired = [r for r in self._live.values() if not r.is_live(now)]
        for reason in expired:
            del self._live[reason.name]
            _LOGGER.debug("Reason %r expired", reason.name)
        return expired

    def next_expiry(self, now: datetime) -> datetime | None:
        """Return the earliest upcoming expiry, so one timer covers them all."""
        upcoming = [
            r.expires for r in self._live.values() if r.expires is not None and r.expires > now
        ]
        return min(upcoming) if upcoming else None

    def targets(self) -> set[str]:
        """Return every switch named by any open reason."""
        return {entity_id for reason in self._live.values() for entity_id in reason.targets}


def latch_expiry(reason: ReasonDef, now: datetime) -> datetime | None:
    """Return when a latch reason must give up.

    ``max_hold`` is mandatory on latches precisely because the automation that is
    supposed to close one may not run — its conditions may not match on the day.
    Without a bound, that pins the switches indefinitely.
    """
    if reason.lifetime is not Lifetime.LATCH or reason.max_hold is None:
        return None
    return now + reason.max_hold


def source_maps_for(
    config: ArbiterConfig, automation: str
) -> tuple[SourceMap, ...]:
    """Return the reason mappings declared for ``automation``."""
    return config.sources.get(automation, ())

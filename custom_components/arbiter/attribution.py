"""Work out who caused a state change.

Home Assistant reuses the caller's :class:`~homeassistant.core.Context` verbatim
for the state write a service call produces, so the ``state_changed`` event carries
the id of whoever asked for it. That single fact gives us both halves of this
module: we can recognise our own writes, and we can name the automation behind
somebody else's.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
import logging

from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CALLBACK_TYPE, Context, Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    ATTRIBUTION_TTL,
    CACHE_MAX_ENTRIES,
    SELF_CONTEXT_TTL,
    Attribution,
)

_LOGGER = logging.getLogger(__name__)


class _TTLCache:
    """A small insertion-ordered cache with per-entry expiry and a hard cap.

    Both caches here are fed by the event bus, so they need a bound; a busy
    instance fires thousands of automations a day.
    """

    def __init__(self, ttl: timedelta, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: OrderedDict[str, tuple[datetime, str | None]] = OrderedDict()

    def set(self, key: str, value: str | None, now: datetime) -> None:
        """Store ``key`` and drop anything that has aged out."""
        self._data.pop(key, None)
        self._data[key] = (now + self._ttl, value)
        self._prune(now)

    def get(self, key: str, now: datetime) -> str | _Missing | None:
        """Return the stored value, or ``MISSING`` if absent or expired."""
        entry = self._data.get(key)
        if entry is None:
            return MISSING
        expires, value = entry
        if expires <= now:
            del self._data[key]
            return MISSING
        return value

    def __contains__(self, key: str) -> bool:
        return self.get(key, dt_util.utcnow()) is not MISSING

    def _prune(self, now: datetime) -> None:
        while self._data:
            key, (expires, _) = next(iter(self._data.items()))
            if expires > now:
                break
            del self._data[key]
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


class _Missing:
    """Sentinel distinguishing 'not cached' from 'cached as None'."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING = _Missing()


class Attributor:
    """Tracks context ids so state changes can be traced back to their cause."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._own = _TTLCache(SELF_CONTEXT_TTL)
        self._automations = _TTLCache(ATTRIBUTION_TTL)
        self._unsub: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Begin recording which context belongs to which automation."""
        if self._unsub is None:
            self._unsub = self.hass.bus.async_listen(
                EVENT_AUTOMATION_TRIGGERED, self._async_automation_triggered
            )

    @callback
    def async_stop(self) -> None:
        """Stop listening and drop both caches."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._own.clear()
        self._automations.clear()

    @callback
    def _async_automation_triggered(self, event: Event) -> None:
        """Record the run context an automation is about to act under.

        The automation builds one ``Context`` per run and every service call it makes
        carries it, so this id is what shows up on the resulting ``state_changed``.
        """
        entity_id = event.data.get(ATTR_ENTITY_ID)
        if entity_id:
            self._automations.set(event.context.id, entity_id, dt_util.utcnow())

    @callback
    def async_note_own(self, context: Context) -> None:
        """Remember a context the arbiter created, so we ignore its echo."""
        self._own.set(context.id, None, dt_util.utcnow())

    @callback
    def async_is_own(self, context: Context) -> bool:
        """Return True if this state change is the arbiter reading its own write."""
        now = dt_util.utcnow()
        if self._own.get(context.id, now) is not MISSING:
            return True
        return (
            context.parent_id is not None
            and self._own.get(context.parent_id, now) is not MISSING
        )

    @callback
    def async_attribute(
        self, context: Context, override_sources: frozenset[str]
    ) -> tuple[Attribution, str | None]:
        """Classify a state change the arbiter did not make.

        Returns the kind of actor and, for an automation, its entity id. An
        automation listed in ``override_sources`` is reported as human: a scene
        button wired through an automation is still a person pressing a switch.
        """
        now = dt_util.utcnow()

        automation = self._automations.get(context.id, now)
        if automation is MISSING and context.parent_id is not None:
            automation = self._automations.get(context.parent_id, now)

        if automation is not MISSING and automation is not None:
            if automation in override_sources:
                return Attribution.HUMAN, automation
            return Attribution.AUTOMATION, automation

        if context.user_id is not None:
            return Attribution.HUMAN, None

        return Attribution.EXTERNAL, None

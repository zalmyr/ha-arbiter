"""The arbiter itself: watches, decides, and (when allowed) acts."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import datetime
import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    DOMAIN as HA_DOMAIN,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    TrackTemplate,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_template_result,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from .applier import async_apply
from .attribution import Attributor
from .const import (
    APPLY_DEBOUNCE_SECONDS,
    CONFLICT_LOG_SIZE,
    EVENT_APPLIED,
    EVENT_CLOSED,
    EVENT_CONFLICT,
    EVENT_OPENED,
    EVENT_OVERRIDE,
    EVENT_REVERTED,
    MANUAL_REASON,
    STORAGE_KEY,
    STORAGE_VERSION,
    Attribution,
    DesiredState,
    Lifetime,
    Mode,
    Origin,
    SourceAction,
)
from .expand import async_expand_target, async_reason_targets
from .models import ConflictRecord, Decision, LiveReason, resolve
from .registry import ArbiterConfig, ReasonStore, latch_expiry, source_maps_for
from .windows import async_evaluate_window, async_window_entities

_LOGGER = logging.getLogger(__name__)

#: Origins worth writing to disk. Window reasons are recomputed from the clock on
#: every start, so persisting them would only risk restoring a stale one.
PERSISTED_ORIGINS = frozenset({Origin.LATCH, Origin.SERVICE, Origin.OVERRIDE, Origin.UNKNOWN})

#: Service domains whose turn_on / turn_off / toggle calls can move a managed switch.
COMMAND_DOMAINS = frozenset(
    {HA_DOMAIN, "switch", "light", "fan", "input_boolean", "group", "scene"}
)

#: How many (context, entity) pairs to remember so a command and the state change
#: it causes are not both counted.
HANDLED_HISTORY = 512


def manual_reason_name(entity_id: str) -> str:
    """Manual overrides are per switch, so each keeps its own timer."""
    return f"{MANUAL_REASON}:{entity_id}"


def _unknown_reason_name(automation: str) -> str:
    """Name for the placeholder reason an unmapped automation gets."""
    return f"unknown:{automation}"


class ArbiterHub:
    """Owns the live reasons and drives the managed switches."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, config: ArbiterConfig
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self.store = ReasonStore()
        self.attributor = Attributor(hass)
        self.conflicts: dict[str, deque[ConflictRecord]] = defaultdict(
            lambda: deque(maxlen=CONFLICT_LOG_SIZE)
        )
        self.decisions: dict[str, Decision] = {}
        self.problems: list[str] = []

        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer_unsub: CALLBACK_TYPE | None = None
        self._timer_at: datetime | None = None
        self._observed: dict[str, DesiredState] = {}
        self._started = False
        self._bypassed = False
        #: Per-switch escape hatch, driven by switch.<name>_arbitration. A switch
        #: turned off here is still tracked, but never written to.
        self._enabled: dict[str, bool] = {}
        self._handled: deque[tuple[str, str]] = deque(maxlen=HANDLED_HISTORY)
        self._persist = Store[dict](hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}")
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=APPLY_DEBOUNCE_SECONDS,
            immediate=False,
            function=self._async_refresh,
        )

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listening and restore anything that outlived the last restart."""
        self.problems = self.config.validate()
        for problem in self.problems:
            _LOGGER.warning("Arbiter config: %s", problem)

        self.attributor.async_start()
        await self._async_restore()

        managed = list(self.config.switches)
        if managed:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, managed, self._async_switch_changed
                )
            )
            self._unsubs.append(
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._async_service_called)
            )

        if bypass := self.config.globals.bypass_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [bypass], self._async_bypass_changed
                )
            )
            state = self.hass.states.get(bypass)
            self._bypassed = state is not None and state.state == STATE_ON

        self._async_track_boundary_entities()
        self._async_track_conditions()

        if self.hass.is_running:
            self._started = True
            await self._async_refresh()
        else:
            # Entity states are not trustworthy until start-up finishes; acting on
            # a machine full of `unknown` would turn everything off.
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._async_ha_started
                )
            )

    async def _async_ha_started(self, _event: Event) -> None:
        self._started = True
        await self._async_refresh()

    async def async_unload(self) -> None:
        """Detach everything and persist what should survive."""
        await self._async_persist()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._async_cancel_timer()
        self.attributor.async_stop()
        self._debouncer.async_shutdown()

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register an entity to be told when decisions change."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    @callback
    def _async_notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # -- tracking -------------------------------------------------------------

    @callback
    def _async_track_boundary_entities(self) -> None:
        """Re-evaluate windows when the entities their boundaries read change.

        The calendar sensors hold a new absolute time every day; without this a
        window computed at startup would keep using yesterday's value.
        """
        entities: set[str] = set()
        for reason in self.config.reasons.values():
            if reason.lifetime is Lifetime.WINDOW:
                entities |= async_window_entities(reason)
        if entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sorted(entities), self._async_boundary_changed
                )
            )

    @callback
    def _async_track_conditions(self) -> None:
        """Re-evaluate when a reason's condition template changes."""
        templates = [
            TrackTemplate(Template(reason.condition, self.hass), None)
            for reason in self.config.reasons.values()
            if reason.condition
        ]
        if not templates:
            return
        result = async_track_template_result(
            self.hass, templates, self._async_condition_changed
        )
        self._unsubs.append(result.async_remove)

    @callback
    def _async_condition_changed(self, *_args) -> None:
        self._schedule_refresh()

    @callback
    def _async_boundary_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_refresh()

    @callback
    def _async_bypass_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        self._bypassed = new_state is not None and new_state.state == STATE_ON
        _LOGGER.debug("Arbiter bypass is now %s", self._bypassed)
        self._schedule_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._debouncer.async_call())

    # -- observing other actors ----------------------------------------------

    @callback
    def _async_service_called(self, event: Event) -> None:
        """React to somebody *commanding* a managed switch.

        The command is the signal, not the state change it may or may not cause:
        an automation turning on a switch that is already on produces no
        ``state_changed`` at all, and watching only state would silently miss its
        reason. That is how an event starting during the day would go unrecorded,
        leaving nothing to close when it ended.
        """
        service = event.data.get("service")
        if service not in (SERVICE_TURN_ON, SERVICE_TURN_OFF, SERVICE_TOGGLE):
            return
        if event.data.get("domain") not in COMMAND_DOMAINS:
            return

        targeted = async_expand_target(self.hass, dict(event.data.get("service_data") or {}))
        for entity_id in targeted & set(self.config.switches):
            if service is SERVICE_TOGGLE or service == SERVICE_TOGGLE:
                current = self.hass.states.get(entity_id)
                observed = (
                    DesiredState.OFF
                    if current is not None and current.state == STATE_ON
                    else DesiredState.ON
                )
            else:
                observed = (
                    DesiredState.ON if service == SERVICE_TURN_ON else DesiredState.OFF
                )
            self._async_observe(entity_id, observed, event.context)

    @callback
    def _async_switch_changed(self, event: Event[EventStateChangedData]) -> None:
        """React to a managed switch changing without a command we saw.

        Covers what the service bus cannot: a physical wall switch, a Z-Wave
        association, anything the device did on its own.
        """
        entity_id = event.data[ATTR_ENTITY_ID]
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

        if new_state is None or new_state.state not in (STATE_ON, STATE_OFF):
            return
        if old_state is not None and old_state.state == new_state.state:
            return  # attributes moved, the switch did not

        observed = DesiredState.ON if new_state.state == STATE_ON else DesiredState.OFF
        self._async_observe(entity_id, observed, event.context, reality_moved=True)

    @callback
    def _async_observe(
        self,
        entity_id: str,
        observed: DesiredState,
        context: Context,
        *,
        reality_moved: bool = False,
    ) -> None:
        """Record that somebody other than us wants this switch in this state.

        A command and the state change it causes share a context and must only be
        classified once — but both still have to trigger a re-evaluation. The
        command arrives *before* the service runs, so a refresh triggered by it
        alone can read the old state, conclude nothing needs doing, and miss the
        change that lands a moment later.
        """
        if self._bypassed or not self._started:
            return
        if self.attributor.async_is_own(context):
            return  # our own write coming back around

        key = (context.id, entity_id)
        if key not in self._handled:
            self._handled.append(key)

            kind, automation = self.attributor.async_attribute(
                context, self.config.globals.override_sources
            )
            if kind in (Attribution.HUMAN, Attribution.EXTERNAL):
                self._async_open_override(entity_id, observed, kind, automation)
            elif automation is not None:
                self._async_apply_source(automation, observed, entity_id)

            self._observed[entity_id] = observed
        elif reality_moved:
            self._observed[entity_id] = observed

        self._schedule_refresh()

    @callback
    def _async_open_override(
        self,
        entity_id: str,
        observed: DesiredState,
        kind: Attribution,
        automation: str | None,
    ) -> None:
        """Record that a person just set this switch, and give them the time to keep it."""
        switch = self.config.switches.get(entity_id)
        timeout = (
            switch.override_timeout
            if switch and switch.override_timeout
            else self.config.globals.override_timeout
        )
        now = dt_util.utcnow()
        reason = LiveReason(
            name=manual_reason_name(entity_id),
            priority=self.config.globals.override_priority,
            state=observed,
            origin=Origin.OVERRIDE,
            targets=frozenset({entity_id}),
            opened=now,
            expires=now + timeout,
            source=automation or kind.value,
        )
        self.store.open(reason)
        _LOGGER.debug(
            "Manual override on %s -> %s for %s (%s)",
            entity_id,
            observed,
            timeout,
            reason.source,
        )
        self.hass.bus.async_fire(
            EVENT_OVERRIDE,
            {
                ATTR_ENTITY_ID: entity_id,
                "state": observed.value,
                "source": reason.source,
                "expires": reason.expires.isoformat() if reason.expires else None,
            },
        )

    @callback
    def _async_apply_source(
        self, automation: str, observed: DesiredState, entity_id: str
    ) -> None:
        """Open or close the reason an automation is mapped to."""
        maps = source_maps_for(self.config, automation)

        if not maps:
            if self.config.globals.ignore_unknown_sources:
                return
            self._async_open_unknown(automation, observed, entity_id)
            return

        now = dt_util.utcnow()
        for source in maps:
            definition = self.config.reasons.get(source.reason)
            if definition is None or definition.lifetime is Lifetime.WINDOW:
                # Already reported by config validation; a window reason runs on
                # its own schedule and must not be forced open by a source.
                continue

            if source.action is SourceAction.CLOSES:
                if self.store.close(source.reason):
                    _LOGGER.debug("%s closed reason %r", automation, source.reason)
                    self.hass.bus.async_fire(
                        EVENT_CLOSED,
                        {"reason": source.reason, "source": automation},
                    )
                continue

            reason = LiveReason(
                name=source.reason,
                priority=definition.priority,
                state=definition.state,
                origin=Origin.LATCH,
                targets=async_reason_targets(self.hass, definition),
                opened=now,
                expires=latch_expiry(definition, now),
                source=automation,
            )
            self.store.open(reason)
            _LOGGER.debug("%s opened reason %r", automation, source.reason)
            self.hass.bus.async_fire(
                EVENT_OPENED, {"reason": source.reason, "source": automation}
            )

    @callback
    def _async_open_unknown(
        self, automation: str, observed: DesiredState, entity_id: str
    ) -> None:
        """Give an unmapped automation a weak reason rather than dropping it silently."""
        name = _unknown_reason_name(automation)
        now = dt_util.utcnow()
        existing = self.store.get(name)
        targets = (existing.targets if existing else frozenset()) | {entity_id}
        self.store.open(
            LiveReason(
                name=name,
                priority=self.config.globals.unknown_source_priority,
                state=observed,
                origin=Origin.UNKNOWN,
                targets=frozenset(targets),
                opened=now,
                expires=now + self.config.globals.override_timeout,
                source=automation,
            )
        )
        _LOGGER.debug(
            "Automation %s is not mapped to a reason; holding it at priority %s",
            automation,
            self.config.globals.unknown_source_priority,
        )

    # -- the decision loop ----------------------------------------------------

    async def _async_refresh(self) -> None:
        """Recompute every switch and act on the result."""
        if not self._started:
            return

        now = dt_util.utcnow()
        for expired in self.store.purge_expired(now):
            self.hass.bus.async_fire(
                EVENT_CLOSED, {"reason": expired.name, "source": "expired"}
            )

        next_window = self._async_sync_windows(now)
        observed = self._observed
        self._observed = {}

        live = self.store.live(now)
        for entity_id, switch in self.config.switches.items():
            decision = resolve(
                live,
                entity_id,
                switch.default_state,
                self.config.globals.tie_break,
                now,
            )
            self.decisions[entity_id] = decision

            if decision.is_conflict and decision.winner is not None:
                record = ConflictRecord(
                    when=now,
                    entity_id=entity_id,
                    winner=decision.winner.name,
                    winner_state=decision.state.value,
                    losers=tuple(r.name for r in decision.losers),
                )
                self.conflicts[entity_id].append(record)
                self.hass.bus.async_fire(
                    EVENT_CONFLICT,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        "winner": record.winner,
                        "state": record.winner_state,
                        "losers": list(record.losers),
                    },
                )

            mode = self.config.mode_for(entity_id)
            if self._bypassed or mode is Mode.ADVISORY or not self.async_is_enabled(entity_id):
                continue

            if await async_apply(self.hass, entity_id, decision.state, self.attributor):
                self.hass.bus.async_fire(
                    EVENT_APPLIED,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        "state": decision.state.value,
                        "winner": decision.winner.name if decision.winner else None,
                    },
                )
                if (was := observed.get(entity_id)) is not None and was != decision.state:
                    self.hass.bus.async_fire(
                        EVENT_REVERTED,
                        {
                            ATTR_ENTITY_ID: entity_id,
                            "attempted": was.value,
                            "restored": decision.state.value,
                            "winner": decision.winner.name if decision.winner else None,
                        },
                    )

        self._async_schedule_timer(now, next_window)
        self._async_notify()
        await self._async_persist()

    @callback
    def _async_sync_windows(self, now: datetime) -> datetime | None:
        """Open and close window reasons; return the earliest upcoming transition."""
        soonest: datetime | None = None

        for name, definition in self.config.reasons.items():
            if definition.lifetime is not Lifetime.WINDOW:
                continue

            state = async_evaluate_window(self.hass, definition, now)
            if state.next_transition is not None and (
                soonest is None or state.next_transition < soonest
            ):
                soonest = state.next_transition

            should_be_open = state.is_open and self._async_condition_holds(definition)
            existing = self.store.get(name)

            if should_be_open:
                if existing is None or existing.origin is not Origin.WINDOW:
                    self.store.open(
                        LiveReason(
                            name=name,
                            priority=definition.priority,
                            state=definition.state,
                            origin=Origin.WINDOW,
                            targets=async_reason_targets(self.hass, definition),
                            opened=now,
                            expires=state.closes_at,
                            source="window",
                        )
                    )
                    self.hass.bus.async_fire(
                        EVENT_OPENED, {"reason": name, "source": "window"}
                    )
                elif existing.expires != state.closes_at:
                    # The boundary entity moved (a new day's sunset time, say).
                    self.store.open(existing.with_expiry(state.closes_at))
            elif existing is not None and existing.origin is Origin.WINDOW:
                self.store.close(name)
                self.hass.bus.async_fire(
                    EVENT_CLOSED, {"reason": name, "source": "window"}
                )

        return soonest

    @callback
    def _async_condition_holds(self, definition) -> bool:
        """Evaluate a reason's optional condition template."""
        if not definition.condition:
            return True
        try:
            rendered = Template(definition.condition, self.hass).async_render(
                parse_result=False
            )
        except Exception as err:
            _LOGGER.warning(
                "Condition for reason %r failed to render (%s); treating as false",
                definition.name,
                err,
            )
            return False
        return str(rendered).strip().lower() in ("true", "on", "yes", "1")

    # -- timers ---------------------------------------------------------------

    @callback
    def _async_cancel_timer(self) -> None:
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None
            self._timer_at = None

    @callback
    def _async_schedule_timer(
        self, now: datetime, next_window: datetime | None
    ) -> None:
        """Wake once, at whichever comes first: an expiry or a window transition."""
        candidates = [t for t in (self.store.next_expiry(now), next_window) if t is not None]
        target = min(candidates) if candidates else None

        if target == self._timer_at:
            return

        self._async_cancel_timer()
        if target is None:
            return

        self._timer_at = target
        self._timer_unsub = async_track_point_in_utc_time(
            self.hass, self._async_timer_fired, target
        )

    @callback
    def _async_timer_fired(self, _now: datetime) -> None:
        self._timer_unsub = None
        self._timer_at = None
        self._schedule_refresh()

    # -- public operations ----------------------------------------------------

    async def async_open(
        self,
        name: str,
        *,
        state: DesiredState | None = None,
        priority: int | None = None,
        targets: Iterable[str] | None = None,
        expires: datetime | None = None,
        source: str = "service",
        origin: Origin = Origin.SERVICE,
    ) -> None:
        """Open a reason by name, filling in whatever the config already says."""
        definition = self.config.reasons.get(name)
        now = dt_util.utcnow()

        if definition is not None:
            resolved_targets = (
                frozenset(targets)
                if targets is not None
                else async_reason_targets(self.hass, definition)
            )
            if expires is None and definition.lifetime is Lifetime.LATCH:
                expires = latch_expiry(definition, now)
            reason = LiveReason(
                name=name,
                priority=priority if priority is not None else definition.priority,
                state=state or definition.state,
                origin=origin,
                targets=resolved_targets,
                opened=now,
                expires=expires,
                source=source,
            )
        else:
            if state is None or targets is None:
                raise ValueError(
                    f"reason {name!r} is not configured, so state and targets are required"
                )
            reason = LiveReason(
                name=name,
                priority=priority if priority is not None else 50,
                state=state,
                origin=origin,
                targets=frozenset(targets),
                opened=now,
                expires=expires,
                source=source,
            )

        self.store.open(reason)
        self.hass.bus.async_fire(EVENT_OPENED, {"reason": name, "source": source})
        await self._debouncer.async_call()

    async def async_close(
        self, name: str, targets: Iterable[str] | None = None
    ) -> bool:
        """Close a reason; return True if it was open.

        Without ``targets`` the reason is dropped entirely. With them it stops
        covering just those switches and keeps holding the rest, which is what you
        want when one room's event has ended but the others are still going.
        """
        if targets is None:
            was_open = self.store.close(name) is not None
            released: list[str] = []
        else:
            previous = self.store.release(name, targets)
            was_open = previous is not None
            released = sorted(previous.targets & frozenset(targets)) if previous else []
            if was_open and not released:
                # The reason was open but covered none of these switches.
                return False

        if was_open:
            self._async_fire_closed(name, released)
            await self._debouncer.async_call()
        return was_open

    async def async_close_all(
        self,
        *,
        origins: set[Origin] | None = None,
        targets: Iterable[str] | None = None,
    ) -> int:
        """Close every reason, or free the given switches from all of them."""
        if targets is None:
            closed = self.store.close_all(origins=origins)
            for reason in closed:
                self._async_fire_closed(reason.name, [])
            if closed:
                await self._debouncer.async_call()
            return len(closed)

        wanted = frozenset(targets)
        affected = [
            reason
            for reason in self.store.all()
            if reason.targets & wanted and (origins is None or reason.origin in origins)
        ]
        for reason in affected:
            self.store.release(reason.name, wanted)
            self._async_fire_closed(reason.name, sorted(reason.targets & wanted))
        if affected:
            await self._debouncer.async_call()
        return len(affected)

    @callback
    def _async_fire_closed(self, name: str, released: list[str]) -> None:
        """Announce a close, saying which switches it freed.

        An empty ``released`` means the whole reason went, so a listener can tell a
        partial release from a full one.
        """
        self.hass.bus.async_fire(
            EVENT_CLOSED, {"reason": name, "source": "service", "released": released}
        )

    async def async_set_mode(self, entity_id: str, mode: Mode) -> None:
        """Change how far the arbiter may go for one switch."""
        switch = self.config.switches.get(entity_id)
        if switch is None:
            raise ValueError(f"{entity_id} is not managed by the arbiter")
        from dataclasses import replace

        self.config.switches[entity_id] = replace(switch, mode=mode)
        await self._debouncer.async_call()

    @callback
    def async_is_enabled(self, entity_id: str) -> bool:
        """True unless this switch's arbitration has been turned off."""
        return self._enabled.get(entity_id, True)

    async def async_set_enabled(self, entity_id: str, enabled: bool) -> None:
        """Turn arbitration for one switch on or off."""
        self._enabled[entity_id] = enabled
        await self._debouncer.async_call()

    @callback
    def async_decision(self, entity_id: str) -> Decision | None:
        """Return the last decision computed for a switch."""
        return self.decisions.get(entity_id)

    @callback
    def async_reason_is_live(self, name: str) -> bool:
        """Return True if a configured reason is currently in force."""
        reason = self.store.get(name)
        return reason is not None and reason.is_live(dt_util.utcnow())

    @property
    def bypassed(self) -> bool:
        """True while the global bypass entity is on."""
        return self._bypassed

    # -- persistence ----------------------------------------------------------

    async def _async_persist(self) -> None:
        """Write the reasons that should outlive a restart."""
        data = {
            "reasons": [
                {
                    "name": r.name,
                    "priority": r.priority,
                    "state": r.state.value,
                    "origin": r.origin.value,
                    "targets": sorted(r.targets),
                    "opened": r.opened.isoformat(),
                    "expires": r.expires.isoformat() if r.expires else None,
                    "source": r.source,
                }
                for r in self.store.all()
                if r.origin in PERSISTED_ORIGINS
            ]
        }
        await self._persist.async_save(data)

    async def _async_restore(self) -> None:
        """Reload reasons from disk, dropping anything that expired while we were down."""
        data = await self._persist.async_load()
        if not data:
            return

        now = dt_util.utcnow()
        for raw in data.get("reasons", []):
            expires = dt_util.parse_datetime(raw["expires"]) if raw.get("expires") else None
            if expires is not None and expires <= now:
                continue
            opened = dt_util.parse_datetime(raw["opened"]) or now
            self.store.open(
                LiveReason(
                    name=raw["name"],
                    priority=raw["priority"],
                    state=DesiredState(raw["state"]),
                    origin=Origin(raw["origin"]),
                    targets=frozenset(raw["targets"]),
                    opened=opened,
                    expires=expires,
                    source=raw.get("source"),
                )
            )
        _LOGGER.debug("Restored %d reason(s)", len(self.store))

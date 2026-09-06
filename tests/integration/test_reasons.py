"""End-to-end behaviour of the reason engine inside Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, Context, HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.arbiter.const import DOMAIN, Mode, SourceAction
from tests.conftest import (
    DAILY_OFF,
    DAILY_ON,
    DEFAULT_GLOBALS,
    GLOBAL_OVERRIDE,
    HALL,
    PARTY_OFF,
    PARTY_ON,
    PORCH,
    PORCH_BUTTON_HELD,
    reason_subentry,
    setup_entry,
    source_subentry,
    switch_subentry,
)


@pytest.fixture(autouse=True)
def no_debounce():
    """Run the apply loop immediately so tests do not wait on wall-clock time."""
    import custom_components.arbiter.hub as hub_module

    original = hub_module.APPLY_DEBOUNCE_SECONDS
    hub_module.APPLY_DEBOUNCE_SECONDS = 0
    yield
    hub_module.APPLY_DEBOUNCE_SECONDS = original


async def flush(hass: HomeAssistant, freezer: FrozenDateTimeFactory | None = None) -> None:
    """Let the debounced apply loop run.

    The debouncer schedules on the event loop clock, which freezegun holds still,
    so a test with frozen time has to advance it or the refresh never fires.
    """
    for _ in range(3):
        if freezer is not None:
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def _call_under(
    hass: HomeAssistant, context: Context, changes: dict[str, str]
) -> None:
    """Issue the same service calls an actor would, under one context."""
    for entity_id, state in changes.items():
        await hass.services.async_call(
            HA_DOMAIN,
            SERVICE_TURN_ON if state == STATE_ON else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            context=context,
        )


async def act_as(
    hass: HomeAssistant,
    automation: str,
    changes: dict[str, str],
    freezer: FrozenDateTimeFactory | None = None,
) -> Context:
    """Simulate an automation running and commanding switches.

    Fires ``automation_triggered`` and then makes the service calls under the same
    context, exactly as a real automation does — which is what lets the arbiter
    attribute the resulting state change back to this automation.
    """
    context = Context()
    hass.bus.async_fire(
        EVENT_AUTOMATION_TRIGGERED,
        {ATTR_ENTITY_ID: automation, "name": automation},
        context=context,
    )
    await hass.async_block_till_done()
    await _call_under(hass, context, changes)
    await flush(hass, freezer)
    return context


async def act_as_person(
    hass: HomeAssistant, user_id: str, entity_id: str, state: str
) -> None:
    """Simulate somebody tapping the switch in the app (context carries a user)."""
    await _call_under(hass, Context(user_id=user_id), {entity_id: state})
    await flush(hass)


async def act_as_device(hass: HomeAssistant, entity_id: str, state: str) -> None:
    """Simulate the device reporting a change nobody commanded (a physical press)."""
    hass.states.async_set(entity_id, state, context=Context())
    await flush(hass)


# -- the reported bug ---------------------------------------------------------

DAILY_PARTY_SUBENTRIES = [
    switch_subentry(PORCH, mode=Mode.GUARD),
    switch_subentry(HALL, mode=Mode.GUARD),
    reason_subentry(
        "daily",
        priority=20,
        entities=[PORCH, HALL],
        max_hold={"hours": 14, "minutes": 0, "seconds": 0},
    ),
    reason_subentry(
        "party",
        priority=50,
        entities=[PORCH, HALL],
        max_hold={"hours": 12, "minutes": 0, "seconds": 0},
    ),
    source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
    source_subentry(DAILY_OFF, SourceAction.CLOSES.value, "daily"),
    source_subentry(PARTY_ON, SourceAction.OPENS.value, "party"),
    source_subentry(PARTY_OFF, SourceAction.CLOSES.value, "party"),
]


async def test_party_ending_does_not_undo_the_morning_schedule(
    hass: HomeAssistant, helpers, switches
):
    """The bug: a party ending at 11am turned the lights off mid-morning.

    The morning schedule is a standing reason, not a past event, so the party
    ending only closes its own reason and the lights stay on.
    """
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=DAILY_PARTY_SUBENTRIES)

    # Morning: the daily schedule turns the lights on.
    await act_as(hass, DAILY_ON, {PORCH: STATE_ON, HALL: STATE_ON})
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_ON

    # A party starts and ends during the day.
    await act_as(hass, PARTY_ON, {PORCH: STATE_ON, HALL: STATE_ON})
    await act_as(hass, PARTY_OFF, {PORCH: STATE_OFF, HALL: STATE_OFF})

    # The daily reason is still live, so the lights come straight back.
    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get(HALL).state == STATE_ON
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "daily"


async def test_party_ending_after_the_daily_schedule_does_turn_them_off(
    hass: HomeAssistant, helpers, switches
):
    """The same close at 3am finds nothing else live, so the lights go off.

    No time condition is needed to express "only after the daily lights-out time".
    """
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=DAILY_PARTY_SUBENTRIES)

    await act_as(hass, DAILY_ON, {PORCH: STATE_ON, HALL: STATE_ON})
    await act_as(hass, PARTY_ON, {PORCH: STATE_ON, HALL: STATE_ON})

    # The evening schedule closes the daily reason.
    await act_as(hass, DAILY_OFF, {PORCH: STATE_OFF, HALL: STATE_OFF})
    # Only the party is holding them up now.
    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "party"

    # Hours later the party's cleanup runs.
    await act_as(hass, PARTY_OFF, {PORCH: STATE_OFF, HALL: STATE_OFF})

    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_OFF
    assert hass.states.get("sensor.porch_winning_reason").state == "unclaimed"


# -- guard mode ---------------------------------------------------------------


async def test_guard_reverts_a_lower_priority_automation(
    hass: HomeAssistant, helpers, switches
):
    """A weak automation cannot switch off what a stronger reason is holding on."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            reason_subentry("special_day", priority=60, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
            source_subentry(DAILY_OFF, SourceAction.CLOSES.value, "daily"),
            source_subentry(
                "automation.holiday_lights_on",
                SourceAction.OPENS.value,
                "special_day",
            ),
        ],
    )

    await act_as(
        hass,
        "automation.holiday_lights_on",
        {PORCH: STATE_ON},
    )
    assert hass.states.get(PORCH).state == STATE_ON

    # The daily off-schedule fires; it closes 'daily', which was never open, and
    # its turn-off loses to the live special_day reason.
    await act_as(hass, DAILY_OFF, {PORCH: STATE_OFF})

    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "special_day"


async def test_advisory_mode_reports_but_never_writes(
    hass: HomeAssistant, helpers, switches
):
    """Advisory is the safe starting point: it shows the divergence, changes nothing."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.ADVISORY),
            reason_subentry("special_day", priority=60, entities=[PORCH]),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(
                "automation.holiday_lights_on", SourceAction.OPENS.value, "special_day"
            ),
            source_subentry(DAILY_OFF, SourceAction.CLOSES.value, "daily"),
        ],
    )

    await act_as(hass, "automation.holiday_lights_on", {PORCH: STATE_ON})
    # A weaker automation turns it off. In guard mode this would be reverted.
    await act_as(hass, DAILY_OFF, {PORCH: STATE_OFF})

    # The arbiter disagrees, and says so, but leaves the switch alone.
    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "special_day"


# -- people -------------------------------------------------------------------


async def test_scene_button_beats_the_schedule_then_releases(
    hass: HomeAssistant, helpers, switches, freezer: FrozenDateTimeFactory
):
    """Holding a wall switch is a person acting, even though an automation runs it."""
    await setup_entry(
        hass,
        data={**DEFAULT_GLOBALS, "override_timeout": {"hours": 1, "minutes": 30, "seconds": 0}},
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
        ],
    )

    await act_as(hass, DAILY_ON, {PORCH: STATE_ON}, freezer)
    assert hass.states.get(PORCH).state == STATE_ON

    # The gabbai holds the scene button to turn it off.
    await act_as(hass, PORCH_BUTTON_HELD, {PORCH: STATE_OFF}, freezer)

    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("sensor.porch_winning_reason").state.startswith("manual")

    # After the override expires the daily schedule takes back over.
    freezer.tick(timedelta(hours=1, minutes=31))
    async_fire_time_changed(hass)
    await flush(hass, freezer)

    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "daily"


async def test_app_tap_is_treated_as_a_person(
    hass: HomeAssistant, helpers, switches, hass_admin_user
):
    """A change carrying a user id outranks the schedules."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
        ],
    )

    await act_as(hass, DAILY_ON, {PORCH: STATE_ON})
    await act_as_person(hass, hass_admin_user.id, PORCH, STATE_OFF)

    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_OFF


async def test_physical_press_is_treated_as_a_person(
    hass: HomeAssistant, helpers, switches
):
    """A change with no command behind it is the device, i.e. somebody at the wall."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
        ],
    )

    await act_as(hass, DAILY_ON, {PORCH: STATE_ON})
    await act_as_device(hass, PORCH, STATE_OFF)

    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_OFF


# -- lifetimes ----------------------------------------------------------------


async def test_latch_gives_up_after_max_hold(
    hass: HomeAssistant, helpers, switches, freezer: FrozenDateTimeFactory
):
    """The stuck-boolean case: the closing automation never runs.

    Without a bound the switch would stay on forever; max_hold is what stops that.
    """
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry(
                "evening",
                priority=70,
                entities=[PORCH],
                max_hold={"hours": 2, "minutes": 0, "seconds": 0},
            ),
            source_subentry(
                "automation.evening_lights_on", SourceAction.OPENS.value, "evening"
            ),
        ],
    )

    await act_as(hass, "automation.evening_lights_on", {PORCH: STATE_ON}, freezer)
    assert hass.states.get("binary_sensor.reason_evening").state == STATE_ON

    freezer.tick(timedelta(hours=2, minutes=1))
    async_fire_time_changed(hass)
    await flush(hass, freezer)

    assert hass.states.get("binary_sensor.reason_evening").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_OFF


# -- bypass -------------------------------------------------------------------


async def test_bypass_stands_the_arbiter_down(hass: HomeAssistant, helpers, switches):
    """The existing global_override kill switch keeps working the way it always did."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
        ],
    )

    await act_as(hass, DAILY_ON, {PORCH: STATE_ON})

    hass.states.async_set(GLOBAL_OVERRIDE, STATE_ON)
    await flush(hass)

    # With the bypass on, anything may turn the switch off and it stays off.
    await _call_under(hass, Context(), {PORCH: STATE_OFF})
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_OFF


# -- unmapped automations -----------------------------------------------------


async def test_unmapped_automation_gets_a_weak_say(hass: HomeAssistant, helpers, switches):
    """Nothing is silently dropped: an unmapped automation still holds a low reason."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("daily", priority=20, entities=[PORCH]),
            source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
        ],
    )

    await act_as(hass, "automation.something_nobody_mapped", {PORCH: STATE_ON})

    winner = hass.states.get("sensor.porch_winning_reason").state
    assert winner == "unknown:automation.something_nobody_mapped"
    assert hass.states.get(PORCH).state == STATE_ON

    # ...and it loses to a properly mapped, higher-priority reason.
    await act_as(hass, DAILY_ON, {PORCH: STATE_ON})
    assert hass.states.get("sensor.porch_winning_reason").state == "daily"


# -- the arbiter's own writes -------------------------------------------------


async def test_arbiter_does_not_react_to_itself(hass: HomeAssistant, helpers, switches):
    """Its own revert must not look like somebody overriding it."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry("special_day", priority=60, entities=[PORCH]),
            source_subentry("automation.holiday_lights_on", SourceAction.OPENS.value, "special_day"),
        ],
    )

    await act_as(hass, "automation.holiday_lights_on", {PORCH: STATE_ON})
    await act_as(hass, "automation.random_low", {PORCH: STATE_OFF})
    await flush(hass)

    hub = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    # The revert produced no manual override, which is what a self-echo would cause.
    assert not any(r.name.startswith("manual:") for r in hub.store.all())
    assert hass.states.get(PORCH).state == STATE_ON

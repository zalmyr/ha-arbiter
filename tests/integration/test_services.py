"""Services: driving reasons by hand, and asking the arbiter to explain itself."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.helpers.service import async_get_all_descriptions
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.arbiter.const import (
    ATTR_TARGETS,
    CONF_MODE,
    CONF_REASON,
    CONF_STATE,
    DOMAIN,
    SERVICE_CLEAR_OVERRIDES,
    SERVICE_CLOSE,
    SERVICE_CLOSE_ALL,
    SERVICE_EXPLAIN,
    SERVICE_FIND_CONFLICTS,
    SERVICE_OPEN,
    SERVICE_OVERRIDE,
    SERVICE_SET_MODE,
    Mode,
    SourceAction,
)
from tests.conftest import (
    DAILY_ON,
    DEFAULT_GLOBALS,
    HALL,
    PORCH,
    reason_subentry,
    setup_entry,
    source_subentry,
    switch_subentry,
)


@pytest.fixture(autouse=True)
def no_debounce():
    """Run the apply loop immediately."""
    import custom_components.arbiter.hub as hub_module

    original = hub_module.APPLY_DEBOUNCE_SECONDS
    hub_module.APPLY_DEBOUNCE_SECONDS = 0
    yield
    hub_module.APPLY_DEBOUNCE_SECONDS = original


async def flush(hass: HomeAssistant, freezer: FrozenDateTimeFactory | None = None) -> None:
    for _ in range(3):
        if freezer is not None:
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
        await asyncio.sleep(0)
        await hass.async_block_till_done()


BASIC = [
    switch_subentry(PORCH, mode=Mode.GUARD),
    switch_subentry(HALL, mode=Mode.GUARD),
    reason_subentry("daily", priority=20, entities=[PORCH, HALL]),
    reason_subentry("night_lockout", priority=90, state="off", entities=[PORCH]),
    source_subentry(DAILY_ON, SourceAction.OPENS.value, "daily"),
]


async def test_open_and_close(hass: HomeAssistant, helpers, switches):
    """arbiter.open holds the switches; arbiter.close lets them go."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get(HALL).state == STATE_ON

    await hass.services.async_call(
        DOMAIN, SERVICE_CLOSE, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_OFF


async def test_close_can_release_one_switch(hass: HomeAssistant, helpers, switches):
    """A reason covering two switches can be let go of on just one of them.

    One room's event ending should not take down a reason the other room is still
    relying on.
    """
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get(HALL).state == STATE_ON

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLOSE,
        {CONF_REASON: "daily", ATTR_TARGETS: [PORCH]},
        blocking=True,
    )
    await flush(hass)

    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get(HALL).state == STATE_ON
    # The reason is still live, just no longer covering the porch.
    assert hass.states.get("binary_sensor.reason_daily").state == STATE_ON
    assert hass.states.get("sensor.hall_winning_reason").state == "daily"
    assert hass.states.get("sensor.porch_winning_reason").state == "unclaimed"


async def test_releasing_the_last_switch_closes_the_reason(
    hass: HomeAssistant, helpers, switches
):
    """Once nothing is left to hold, the reason is gone rather than empty."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)

    for switch in (PORCH, HALL):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CLOSE,
            {CONF_REASON: "daily", ATTR_TARGETS: [switch]},
            blocking=True,
        )
        await flush(hass)

    assert hass.states.get("binary_sensor.reason_daily").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get(HALL).state == STATE_OFF


async def test_releasing_a_switch_the_reason_never_covered_changes_nothing(
    hass: HomeAssistant, helpers, switches
):
    """night_lockout only covers the porch, so releasing the hall is a no-op."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "night_lockout"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get("binary_sensor.reason_night_lockout").state == STATE_ON

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLOSE,
        {CONF_REASON: "night_lockout", ATTR_TARGETS: [HALL]},
        blocking=True,
    )
    await flush(hass)

    assert hass.states.get("binary_sensor.reason_night_lockout").state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "night_lockout"


async def test_close_all_can_free_one_switch(hass: HomeAssistant, helpers, switches):
    """Free a stuck switch from everything holding it, without touching the others."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "night_lockout"}, blocking=True
    )
    await flush(hass)

    await hass.services.async_call(
        DOMAIN, SERVICE_CLOSE_ALL, {ATTR_TARGETS: [PORCH]}, blocking=True
    )
    await flush(hass)

    # Nothing claims the porch any more; the hall is still held by daily.
    assert hass.states.get("sensor.porch_winning_reason").state == "unclaimed"
    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("sensor.hall_winning_reason").state == "daily"
    assert hass.states.get(HALL).state == STATE_ON


async def test_open_with_a_duration_expires(
    hass: HomeAssistant, helpers, switches, freezer: FrozenDateTimeFactory
):
    """A reason opened for a fixed time gives up on its own."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_OPEN,
        {CONF_REASON: "daily", "duration": {"hours": 1, "minutes": 0, "seconds": 0}},
        blocking=True,
    )
    await flush(hass, freezer)
    assert hass.states.get(PORCH).state == STATE_ON

    freezer.tick(timedelta(hours=1, minutes=1))
    async_fire_time_changed(hass)
    await flush(hass, freezer)

    assert hass.states.get(PORCH).state == STATE_OFF


async def test_an_off_reason_outranks_an_on_reason(hass: HomeAssistant, helpers, switches):
    """A higher-priority 'off' reason wins, and the conflict is visible."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "night_lockout"}, blocking=True
    )
    await flush(hass)

    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("binary_sensor.porch_conflict").state == STATE_ON
    # night_lockout only covers the porch, so the hall stay on.
    assert hass.states.get(HALL).state == STATE_ON


async def test_override_and_clear(hass: HomeAssistant, helpers, switches):
    """An explicit override wins, and clearing it hands control back."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON

    await hass.services.async_call(
        DOMAIN,
        SERVICE_OVERRIDE,
        {ATTR_ENTITY_ID: [PORCH], CONF_STATE: "off"},
        blocking=True,
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_OFF

    await hass.services.async_call(
        DOMAIN, SERVICE_CLEAR_OVERRIDES, {}, blocking=True
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON


async def test_set_mode_switches_between_watching_and_acting(
    hass: HomeAssistant, helpers, switches
):
    """Advisory watches; flipping to guard makes the same reason act."""
    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.ADVISORY),
            reason_subentry("daily", priority=20, entities=[PORCH]),
        ],
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_OFF  # advisory: no write

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_MODE,
        {ATTR_ENTITY_ID: [PORCH], CONF_MODE: Mode.GUARD.value},
        blocking=True,
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON


async def test_arbitration_switch_stands_down_one_switch(
    hass: HomeAssistant, helpers, switches
):
    """The per-switch escape hatch stops writes without losing tracking."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.porch_arbitration"}, blocking=True
    )
    await flush(hass)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)

    # Still tracked, but not written to. The hall are unaffected.
    assert hass.states.get("binary_sensor.porch_should_be_on").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get(HALL).state == STATE_ON


async def test_explain_says_why(hass: HomeAssistant, helpers, switches):
    """explain is the debugging tool: what won, what lost, and what it wants."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "night_lockout"}, blocking=True
    )
    await flush(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPLAIN,
        {ATTR_ENTITY_ID: [PORCH]},
        blocking=True,
        return_response=True,
    )

    detail = response["switches"][PORCH]
    assert detail["managed"] is True
    assert detail["should_be"] == "off"
    assert detail["winning_reason"] == "night_lockout"
    assert "priority 90" in detail["because"]
    assert detail["overruled"] == ["daily"]
    assert {r["name"] for r in detail["live_reasons"]} == {"daily", "night_lockout"}


async def test_explain_with_nothing_live(hass: HomeAssistant, helpers, switches):
    """With no reason open, explain names the fallback rather than a winner."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_EXPLAIN, {}, blocking=True, return_response=True
    )

    detail = response["switches"][PORCH]
    assert detail["winning_reason"] is None
    assert "fallback is off" in detail["because"]


async def test_find_conflicts_sees_through_labels(
    hass: HomeAssistant, helpers, switches
):
    """An automation that targets a label still counts as touching the switch.

    Checking only automations_with_entity would miss most real callers, because
    the automations address their switches by label and area.
    """
    labels = lr.async_get(hass)
    label = labels.async_create("turn on daily")
    registry = er.async_get(hass)
    registry.async_update_entity(PORCH, labels={label.label_id})
    await hass.async_block_till_done()

    assert await async_setup_automation(hass, label.label_id)

    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_FIND_CONFLICTS, {}, blocking=True, return_response=True
    )

    detail = response["switches"][PORCH]
    assert "automation.labelled_schedule" in detail["automations"]
    # It is enabled and has no reason mapping, so it is reported as something to map.
    assert "automation.labelled_schedule" in detail["unmapped"]
    assert detail["disabled"] == []


async def test_find_conflicts_does_not_flag_switched_off_automations(
    hass: HomeAssistant, helpers, switches
):
    """A deliberately switched-off automation is not a gap to close.

    automations.yaml does not record whether an automation is enabled, and
    automations_with_* returns it either way, so without checking the live state a
    switched-off automation looks exactly like one you forgot to map.
    """
    labels = lr.async_get(hass)
    label = labels.async_create("turn on daily")
    registry = er.async_get(hass)
    registry.async_update_entity(PORCH, labels={label.label_id})
    await hass.async_block_till_done()

    assert await async_setup_automation(hass, label.label_id)

    await hass.services.async_call(
        "automation",
        "turn_off",
        {ATTR_ENTITY_ID: "automation.labelled_schedule"},
        blocking=True,
    )
    await hass.async_block_till_done()

    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_FIND_CONFLICTS, {}, blocking=True, return_response=True
    )

    detail = response["switches"][PORCH]
    # Still listed, because it would command the switch if switched back on...
    assert "automation.labelled_schedule" in detail["automations"]
    assert "automation.labelled_schedule" in detail["disabled"]
    # ...but not presented as something to map.
    assert "automation.labelled_schedule" not in detail["unmapped"]


async def async_setup_automation(hass: HomeAssistant, label_id: str) -> bool:
    """Create an automation that targets a label rather than an entity."""
    from homeassistant.setup import async_setup_component

    return await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "labelled",
                    "alias": "labelled schedule",
                    "trigger": {"platform": "event", "event_type": "never_fired"},
                    "action": {
                        "service": "switch.turn_on",
                        "target": {"label_id": label_id},
                    },
                }
            ]
        },
    )


async def test_reasons_survive_a_restart(
    hass: HomeAssistant, helpers, switches, freezer: FrozenDateTimeFactory
):
    """A latch opened before a restart is still holding afterwards."""
    entry = await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass, freezer)
    assert hass.states.get(PORCH).state == STATE_ON

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await flush(hass, freezer)

    hub = entry.runtime_data
    assert hub.store.get("daily") is not None
    assert hass.states.get("binary_sensor.reason_daily").state == STATE_ON


# -- seeing every reason at once ----------------------------------------------


async def test_reason_overview_lists_live_and_idle(
    hass: HomeAssistant, helpers, switches
):
    """One entity answers "what is running" without checking each reason in turn."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    overview = hass.states.get("sensor.arbiter_reasons")
    assert overview.state == "0"
    assert sorted(overview.attributes["idle"]) == ["daily", "night_lockout"]

    await hass.services.async_call(
        DOMAIN, SERVICE_OPEN, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)

    overview = hass.states.get("sensor.arbiter_reasons")
    assert overview.state == "1"
    live = overview.attributes["live"]
    assert [r["name"] for r in live] == ["daily"]
    assert live[0]["priority"] == 20
    assert live[0]["wants"] == "on"
    assert live[0]["configured"] is True
    assert sorted(live[0]["switches"]) == [HALL, PORCH]
    assert overview.attributes["idle"] == ["night_lockout"]


async def test_reason_overview_shows_reasons_that_have_no_entity(
    hass: HomeAssistant, helpers, switches, hass_admin_user
):
    """A manual override has no binary_sensor of its own, so it must show up here."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_OVERRIDE,
        {ATTR_ENTITY_ID: [PORCH], CONF_STATE: "on"},
        blocking=True,
    )
    await flush(hass)

    overview = hass.states.get("sensor.arbiter_reasons")
    manual = f"manual:{PORCH}"
    assert manual in overview.attributes["unconfigured"]
    assert manual in [r["name"] for r in overview.attributes["live"]]
    # ...and it genuinely has no entity of its own, which is why this matters.
    assert hass.states.get(f"binary_sensor.reason_{manual}") is None


async def test_reason_overview_maps_automations_to_reasons(
    hass: HomeAssistant, helpers, switches
):
    """Which automations open and close each reason, in one place."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    mappings = hass.states.get("sensor.arbiter_reasons").attributes["automations"]
    assert mappings["daily"]["opens"] == [DAILY_ON]
    assert mappings["daily"]["closes"] == []
    # A configured reason nothing maps to still appears, with empty lists.
    assert mappings["night_lockout"] == {"opens": [], "closes": []}


async def test_reason_overview_counts_drop_when_reasons_close(
    hass: HomeAssistant, helpers, switches
):
    """The count follows the live set, including partial releases."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)

    for reason in ("daily", "night_lockout"):
        await hass.services.async_call(
            DOMAIN, SERVICE_OPEN, {CONF_REASON: reason}, blocking=True
        )
    await flush(hass)
    assert hass.states.get("sensor.arbiter_reasons").state == "2"

    # Releasing one switch of a two-switch reason keeps it live.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLOSE,
        {CONF_REASON: "daily", ATTR_TARGETS: [PORCH]},
        blocking=True,
    )
    await flush(hass)
    assert hass.states.get("sensor.arbiter_reasons").state == "2"

    await hass.services.async_call(
        DOMAIN, SERVICE_CLOSE, {CONF_REASON: "daily"}, blocking=True
    )
    await flush(hass)
    assert hass.states.get("sensor.arbiter_reasons").state == "1"


# -- the reason picker --------------------------------------------------------


async def reason_options(hass: HomeAssistant, service: str) -> list[str]:
    """The options the UI would show for a service's reason field."""
    descriptions = await async_get_all_descriptions(hass)
    field = descriptions[DOMAIN][service]["fields"][CONF_REASON]
    return field["selector"]["select"]["options"]


async def test_reason_options_track_what_exists(hass: HomeAssistant, helpers, switches):
    """open offers the defined reasons; close also offers whatever is live."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    assert await reason_options(hass, SERVICE_OPEN) == ["daily", "night_lockout"]
    assert await reason_options(hass, SERVICE_CLOSE) == ["daily", "night_lockout"]

    # A manual override is live but undefined, so only close should learn about it.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_OVERRIDE,
        {ATTR_ENTITY_ID: [PORCH], CONF_STATE: "on"},
        blocking=True,
    )
    await flush(hass)

    manual = f"manual:{PORCH}"
    assert manual in await reason_options(hass, SERVICE_CLOSE)
    assert manual not in await reason_options(hass, SERVICE_OPEN)


async def test_open_refuses_an_unknown_reason(hass: HomeAssistant, helpers, switches):
    """Typing a name that is not defined is an error, not a new reason."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    with pytest.raises(ServiceValidationError, match="does not know the reason"):
        await hass.services.async_call(
            DOMAIN, SERVICE_OPEN, {CONF_REASON: "dailly"}, blocking=True
        )


async def test_close_refuses_an_unknown_reason(hass: HomeAssistant, helpers, switches):
    """A typo here used to be a silent no-op, which is the whole point of refusing."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    with pytest.raises(ServiceValidationError, match="daily"):
        await hass.services.async_call(
            DOMAIN, SERVICE_CLOSE, {CONF_REASON: "dailly"}, blocking=True
        )


async def test_close_accepts_a_live_unconfigured_reason(
    hass: HomeAssistant, helpers, switches
):
    """A manual override has no definition, but it must still be closable."""
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=BASIC)
    await flush(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_OVERRIDE,
        {ATTR_ENTITY_ID: [PORCH], CONF_STATE: "on"},
        blocking=True,
    )
    await flush(hass)
    assert hass.states.get(PORCH).state == STATE_ON

    await hass.services.async_call(
        DOMAIN, SERVICE_CLOSE, {CONF_REASON: f"manual:{PORCH}"}, blocking=True
    )
    await flush(hass)

    assert hass.states.get(PORCH).state == STATE_OFF

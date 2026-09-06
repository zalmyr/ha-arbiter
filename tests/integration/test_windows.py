"""Window reasons: independent on- and off-schedules that still hold a state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.arbiter.const import (
    CONF_ENTITY_ID,
    CONF_KIND,
    CONF_OFFSET,
    CONF_TIME,
    BoundaryKind,
    Lifetime,
    Mode,
)
from tests.conftest import (
    DEFAULT_GLOBALS,
    HALL,
    PORCH,
    reason_subentry,
    setup_entry,
    switch_subentry,
)

DUSK = "sensor.dusk"
DAWN = "sensor.dawn"


@pytest.fixture(autouse=True)
def no_debounce():
    """Run the apply loop immediately."""
    import custom_components.arbiter.hub as hub_module

    original = hub_module.APPLY_DEBOUNCE_SECONDS
    hub_module.APPLY_DEBOUNCE_SECONDS = 0
    yield
    hub_module.APPLY_DEBOUNCE_SECONDS = original


async def settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance the frozen clock enough for the debounced refresh to run."""
    for _ in range(3):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await asyncio.sleep(0)
        await hass.async_block_till_done()


def clock(value: str) -> dict:
    return {CONF_KIND: BoundaryKind.TIME.value, CONF_TIME: value}


def entity_at(entity_id: str, offset: dict | None = None) -> dict:
    return {
        CONF_KIND: BoundaryKind.ENTITY.value,
        CONF_ENTITY_ID: entity_id,
        CONF_OFFSET: offset or {"hours": 0, "minutes": 0, "seconds": 0},
    }


def set_timestamp(hass: HomeAssistant, entity_id: str, when: datetime) -> None:
    """Publish a timestamp sensor the way the calendar integration does."""
    hass.states.async_set(
        entity_id,
        when.isoformat(),
        {ATTR_DEVICE_CLASS: SensorDeviceClass.TIMESTAMP},
    )


async def test_window_holds_the_switch_between_its_edges(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """A window is a standing state, not two edge triggers."""
    freezer.move_to("2026-09-06 05:00:00-04:00")

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry(
                "daily",
                priority=20,
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=clock("07:00:00"),
                end=clock("22:00:00"),
            ),
        ],
    )
    await settle(hass, freezer)

    # Before the window opens, nothing is claiming it.
    assert hass.states.get("binary_sensor.reason_daily").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_OFF

    freezer.move_to("2026-09-06 08:00:00-04:00")
    await settle(hass, freezer)

    assert hass.states.get("binary_sensor.reason_daily").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_ON

    freezer.move_to("2026-09-06 22:30:00-04:00")
    await settle(hass, freezer)

    assert hass.states.get("binary_sensor.reason_daily").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_OFF


async def test_window_crossing_midnight(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """An off-window that runs into the small hours is open at 03:00."""
    freezer.move_to("2026-09-06 03:00:00-04:00")

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD, default_state="on"),
            reason_subentry(
                "night_lockout",
                priority=90,
                state="off",
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=clock("23:00:00"),
                end=clock("05:00:00"),
            ),
        ],
    )
    await settle(hass, freezer)

    assert hass.states.get("binary_sensor.reason_night_lockout").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_OFF

    freezer.move_to("2026-09-06 06:00:00-04:00")
    await settle(hass, freezer)

    # Out of the lockout, the fallback takes over.
    assert hass.states.get("binary_sensor.reason_night_lockout").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_ON


async def test_window_built_on_calendar_sensors(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """dusk + 25 minutes to dawn + 3, driven by two timestamp sensors."""
    freezer.move_to("2026-09-05 12:00:00-04:00")

    set_timestamp(hass, DUSK, dt_util.parse_datetime("2026-09-05T19:00:00-04:00"))
    set_timestamp(hass, DAWN, dt_util.parse_datetime("2026-09-05T19:45:00-04:00"))
    await hass.async_block_till_done()

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD, default_state="on"),
            reason_subentry(
                "quiet_hour",
                priority=70,
                state="off",
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=entity_at(DUSK, {"hours": 0, "minutes": 25, "seconds": 0}),
                end=entity_at(DAWN, {"hours": 0, "minutes": 3, "seconds": 0}),
            ),
        ],
    )
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_quiet_hour").state == STATE_OFF

    # 19:30 is inside dusk+25 (19:25) .. dawn+3 (19:48).
    freezer.move_to("2026-09-05 19:30:00-04:00")
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_quiet_hour").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_OFF

    freezer.move_to("2026-09-05 20:00:00-04:00")
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_quiet_hour").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_ON


async def test_window_follows_the_sensor_when_it_moves(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """The calendar sensors hold a new time every day; the window has to follow.

    Without watching the boundary entities, a window computed at startup would
    keep using yesterday's value.
    """
    freezer.move_to("2026-09-05 19:30:00-04:00")

    set_timestamp(hass, DUSK, dt_util.parse_datetime("2026-09-05T19:00:00-04:00"))
    set_timestamp(hass, DAWN, dt_util.parse_datetime("2026-09-05T19:45:00-04:00"))
    await hass.async_block_till_done()

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD, default_state="on"),
            reason_subentry(
                "quiet_hour",
                priority=70,
                state="off",
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=entity_at(DUSK),
                end=entity_at(DAWN),
            ),
        ],
    )
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_quiet_hour").state == STATE_ON

    # A later dusk moves the window off the current moment.
    set_timestamp(hass, DUSK, dt_util.parse_datetime("2026-09-05T20:00:00-04:00"))
    set_timestamp(hass, DAWN, dt_util.parse_datetime("2026-09-05T20:45:00-04:00"))
    await settle(hass, freezer)

    assert hass.states.get("binary_sensor.reason_quiet_hour").state == STATE_OFF
    assert hass.states.get(PORCH).state == STATE_ON


async def test_window_weekday_filter(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """A Saturday-only window stays shut the rest of the week.

    Weekday is a first-class field rather than a template because "it is Saturday"
    changes with the clock, and a template is only re-evaluated when an entity does.
    """
    subentries = [
        switch_subentry(PORCH, mode=Mode.GUARD),
        reason_subentry(
            "weekend_afternoon",
            priority=70,
            entities=[PORCH],
            lifetime=Lifetime.WINDOW,
            start=clock("14:00:00"),
            end=clock("18:00:00"),
            weekdays=["sat"],
        ),
    ]

    # Friday afternoon: shut.
    freezer.move_to("2026-09-04 15:00:00-04:00")
    await setup_entry(hass, data=DEFAULT_GLOBALS, subentries=subentries)
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_weekend_afternoon").state == STATE_OFF

    # Saturday afternoon: open.
    freezer.move_to("2026-09-05 15:00:00-04:00")
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_weekend_afternoon").state == STATE_ON


async def test_window_condition_template(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """A condition gates the window and is re-checked when its entities change."""
    freezer.move_to("2026-09-06 12:00:00-04:00")
    hass.states.async_set("binary_sensor.holiday", STATE_OFF)
    await hass.async_block_till_done()

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry(
                "holiday",
                priority=60,
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=clock("07:30:00"),
                end=clock("23:00:00"),
                condition="{{ is_state('binary_sensor.holiday','on') }}",
            ),
        ],
    )
    await settle(hass, freezer)
    assert hass.states.get("binary_sensor.reason_holiday").state == STATE_OFF

    hass.states.async_set("binary_sensor.holiday", STATE_ON)
    await settle(hass, freezer)

    assert hass.states.get("binary_sensor.reason_holiday").state == STATE_ON
    assert hass.states.get(PORCH).state == STATE_ON


async def test_special_day_window_outranks_the_daily_window(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """Priority replaces the exclusion conditions the automations carry today."""
    freezer.move_to("2026-09-06 13:00:00-04:00")

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            reason_subentry(
                "daily",
                priority=20,
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=clock("07:00:00"),
                end=clock("22:00:00"),
            ),
            reason_subentry(
                "holiday_daytime_off",
                priority=60,
                state="off",
                entities=[PORCH],
                lifetime=Lifetime.WINDOW,
                start=clock("12:00:00"),
                end=clock("14:00:00"),
            ),
        ],
    )
    await settle(hass, freezer)

    # Both windows are open; the special day wins and the conflict is reported.
    assert hass.states.get(PORCH).state == STATE_OFF
    assert hass.states.get("sensor.porch_winning_reason").state == "holiday_daytime_off"
    assert hass.states.get("binary_sensor.porch_conflict").state == STATE_ON

    freezer.move_to("2026-09-06 15:00:00-04:00")
    await settle(hass, freezer)

    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get("sensor.porch_winning_reason").state == "daily"
    assert hass.states.get("binary_sensor.porch_conflict").state == STATE_OFF


async def test_window_membership_by_label(
    hass: HomeAssistant, helpers, switches, local_timezone, freezer: FrozenDateTimeFactory
):
    """A reason can take its switches from a label, the way the automations target them."""
    from homeassistant.helpers import entity_registry as er, label_registry as lr

    from custom_components.arbiter.const import CONF_LABEL, CONF_MEMBERSHIP, Membership

    freezer.move_to("2026-09-06 12:00:00-04:00")

    labels = lr.async_get(hass)
    label = labels.async_create("turn on daily")
    registry = er.async_get(hass)
    for entity_id in (PORCH, HALL):
        registry.async_update_entity(entity_id, labels={label.label_id})
    await hass.async_block_till_done()

    reason = reason_subentry(
        "daily",
        priority=20,
        lifetime=Lifetime.WINDOW,
        start=clock("07:00:00"),
        end=clock("22:00:00"),
    )
    reason["data"][CONF_MEMBERSHIP] = Membership.LABEL.value
    reason["data"][CONF_LABEL] = label.label_id

    await setup_entry(
        hass,
        data=DEFAULT_GLOBALS,
        subentries=[
            switch_subentry(PORCH, mode=Mode.GUARD),
            switch_subentry(HALL, mode=Mode.GUARD),
            reason,
        ],
    )
    await settle(hass, freezer)

    # Both labelled switches are covered, without either being named.
    assert hass.states.get(PORCH).state == STATE_ON
    assert hass.states.get(HALL).state == STATE_ON

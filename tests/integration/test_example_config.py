"""The shipped example configuration must actually load.

arbiter.example.yaml is meant to be pasted into configuration.yaml, so a schema
error in it would surface as a failed setup on a live instance.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import yaml

from custom_components.arbiter.const import (
    DOMAIN,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    Lifetime,
    Mode,
    SourceAction,
)

EXAMPLE = Path(__file__).parents[2] / "arbiter.example.yaml"


def load_example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))[DOMAIN]


async def import_example(hass: HomeAssistant):
    """Import the shipped example and return the resulting hub."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=load_example()
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_example_config_imports_cleanly(hass: HomeAssistant, helpers, switches):
    """It parses, imports, and produces a configuration with no problems."""
    hub = (await import_example(hass)).runtime_data

    # No dangling mappings, and no source pointed at a self-scheduling window.
    assert hub.problems == []

    assert set(hub.config.switches) == {"switch.porch", "switch.hall"}
    assert set(hub.config.reasons) == {
        "daily",
        "weekend_evening",
        "night_lockout",
        "party",
        "holiday",
    }

    # Everything starts in advisory: importing this must not move a switch.
    assert all(
        hub.config.mode_for(entity_id) is Mode.ADVISORY
        for entity_id in hub.config.switches
    )


async def test_example_maps_an_event_ending_as_a_close(
    hass: HomeAssistant, helpers, switches
):
    """The example has to demonstrate the thing that makes Arbiter worth having."""
    hub = (await import_example(hass)).runtime_data

    party_off = hub.config.sources["automation.party_lights_off"]
    assert party_off[0].action is SourceAction.CLOSES
    assert party_off[0].reason == "party"


async def test_example_windows_and_latches_are_well_formed(
    hass: HomeAssistant, helpers, switches
):
    """Windows carry both edges; every latch carries a bound."""
    hub = (await import_example(hass)).runtime_data

    # The daily baseline is one window driven by two independent input_datetimes.
    daily = hub.config.reasons["daily"]
    assert daily.lifetime is Lifetime.WINDOW
    assert daily.start.entity_id == "input_datetime.turn_on_lights_daily"
    assert daily.end.entity_id == "input_datetime.turn_off_lights_daily"

    for reason in hub.config.reasons.values():
        if reason.lifetime is Lifetime.LATCH:
            # Without a bound, a close that never runs pins the switches forever.
            assert reason.max_hold is not None
        else:
            assert reason.start is not None and reason.end is not None


async def test_example_subentry_counts(hass: HomeAssistant, helpers, switches):
    """Reasons, switches and mappings each become their own editable rows."""
    entry = await import_example(hass)

    counts: dict[str, int] = {}
    for sub in entry.subentries.values():
        counts[sub.subentry_type] = counts.get(sub.subentry_type, 0) + 1

    assert counts[SUBENTRY_REASON] == 5
    assert counts[SUBENTRY_SWITCH] == 2
    assert counts[SUBENTRY_SOURCE] == 4

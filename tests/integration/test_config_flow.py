"""The GUI: setting Arbiter up and adding reasons, switches and mappings."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.arbiter.const import (
    CONF_ACTION,
    CONF_AUTOMATION,
    CONF_BYPASS_ENTITY,
    CONF_DEFAULT_MODE,
    CONF_DEFAULT_STATE,
    CONF_END,
    CONF_ENTITIES,
    CONF_ENTITY_ID,
    CONF_EVENT,
    CONF_KIND,
    CONF_LIFETIME,
    CONF_MAX_HOLD,
    CONF_MEMBERSHIP,
    CONF_MODE,
    CONF_NAME,
    CONF_OFFSET,
    CONF_OVERRIDE_PRIORITY,
    CONF_OVERRIDE_SOURCES,
    CONF_OVERRIDE_TIMEOUT,
    CONF_PRIORITY,
    CONF_REASON,
    CONF_REASONS,
    CONF_SOURCES,
    CONF_START,
    CONF_STATE,
    CONF_SWITCHES,
    CONF_TIE_BREAK,
    CONF_TIME,
    CONF_UNKNOWN_SOURCE_PRIORITY,
    DOMAIN,
    SERVICE_EXPORT_CONFIG,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    BoundaryKind,
    Lifetime,
    Membership,
    Mode,
    SourceAction,
    TieBreak,
)
from tests.conftest import (
    DAILY_ON,
    GLOBAL_OVERRIDE,
    PARTY_OFF,
    PARTY_ON,
    PORCH,
    PORCH_BUTTON_HELD,
    setup_entry,
)

GLOBALS_INPUT = {
    CONF_BYPASS_ENTITY: GLOBAL_OVERRIDE,
    CONF_DEFAULT_MODE: Mode.ADVISORY.value,
    CONF_OVERRIDE_TIMEOUT: {"hours": 1, "minutes": 30, "seconds": 0},
    CONF_OVERRIDE_PRIORITY: 100,
    CONF_OVERRIDE_SOURCES: [PORCH_BUTTON_HELD],
    CONF_UNKNOWN_SOURCE_PRIORITY: 10,
    "ignore_unknown_sources": False,
    CONF_TIE_BREAK: TieBreak.LAST_WINS.value,
}


async def test_user_flow_creates_the_entry(hass: HomeAssistant, helpers):
    """The main form collects the instance-wide settings."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GLOBALS_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Arbiter"
    assert result["data"][CONF_BYPASS_ENTITY] == GLOBAL_OVERRIDE
    assert result["data"][CONF_OVERRIDE_SOURCES] == [PORCH_BUTTON_HELD]


async def test_only_one_instance(hass: HomeAssistant):
    """A second setup is refused; reasons and switches are subentries, not entries."""
    MockConfigEntry(domain=DOMAIN, data=GLOBALS_INPUT).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_edits_the_settings(hass: HomeAssistant, helpers, switches):
    """The options form reopens the same fields, prefilled."""
    entry = await setup_entry(hass, data=GLOBALS_INPUT, subentries=[])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**GLOBALS_INPUT, CONF_DEFAULT_MODE: Mode.GUARD.value}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DEFAULT_MODE] == Mode.GUARD.value


# -- subentries ---------------------------------------------------------------


async def start_subentry(hass: HomeAssistant, entry, subentry_type: str):
    """Begin an 'add' subentry flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), context={"source": SOURCE_USER}
    )


async def test_add_latch_reason(hass: HomeAssistant, helpers, switches):
    """A latch reason: basics, membership, then its mandatory bound."""
    entry = await setup_entry(hass, data=GLOBALS_INPUT, subentries=[])

    result = await start_subentry(hass, entry, SUBENTRY_REASON)
    assert result["step_id"] == "basics"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "party",
            CONF_PRIORITY: 50,
            CONF_STATE: "on",
            CONF_MEMBERSHIP: Membership.ENTITIES.value,
            CONF_LIFETIME: Lifetime.LATCH.value,
        },
    )
    assert result["step_id"] == "membership"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ENTITIES: [PORCH]}
    )
    assert result["step_id"] == "latch"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_MAX_HOLD: {"hours": 12, "minutes": 0, "seconds": 0}}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "party"

    stored = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_REASON
    )
    assert stored.data[CONF_NAME] == "party"
    assert stored.data[CONF_MAX_HOLD] == {"hours": 12, "minutes": 0, "seconds": 0}


async def test_add_window_reason_with_independent_edges(
    hass: HomeAssistant, helpers, switches
):
    """A window's start and end are asked for separately, and may differ in kind."""
    entry = await setup_entry(hass, data=GLOBALS_INPUT, subentries=[])

    result = await start_subentry(hass, entry, SUBENTRY_REASON)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "daily",
            CONF_PRIORITY: 20,
            CONF_STATE: "on",
            CONF_MEMBERSHIP: Membership.ENTITIES.value,
            CONF_LIFETIME: Lifetime.WINDOW.value,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ENTITIES: [PORCH]}
    )
    assert result["step_id"] == "start_kind"

    # Start: a wall-clock time.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_KIND: BoundaryKind.TIME.value}
    )
    assert result["step_id"] == "start_value"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_TIME: "07:00:00"}
    )
    assert result["step_id"] == "end_kind"

    # End: a sun event, showing the two edges are independent.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_KIND: BoundaryKind.SUN.value}
    )
    assert result["step_id"] == "end_value"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_EVENT: "sunset", CONF_OFFSET: {"hours": 0, "minutes": 25, "seconds": 0}},
    )
    assert result["step_id"] == "window_extras"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"weekdays": ["sat"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_REASON
    )
    assert stored.data[CONF_START] == {
        CONF_KIND: BoundaryKind.TIME.value,
        CONF_TIME: "07:00:00",
    }
    assert stored.data[CONF_END][CONF_KIND] == BoundaryKind.SUN.value
    assert stored.data[CONF_END][CONF_EVENT] == "sunset"
    assert stored.data["weekdays"] == ["sat"]


async def test_add_switch(hass: HomeAssistant, helpers, switches):
    """Adding a managed switch registers its entities."""
    entry = await setup_entry(hass, data=GLOBALS_INPUT, subentries=[])

    result = await start_subentry(hass, entry, SUBENTRY_SWITCH)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_ID: PORCH,
            CONF_MODE: Mode.GUARD.value,
            CONF_DEFAULT_STATE: "off",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PORCH
    assert hass.states.get("binary_sensor.porch_should_be_on") is not None


async def test_mapping_needs_a_reason_to_point_at(hass: HomeAssistant, helpers, switches):
    """A mapping with no reasons defined would dangle, so the flow says so."""
    entry = await setup_entry(hass, data=GLOBALS_INPUT, subentries=[])

    result = await start_subentry(hass, entry, SUBENTRY_SOURCE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_reasons"


async def test_add_source_mapping(hass: HomeAssistant, helpers, switches):
    """The mapping screen reads as a sentence and offers the existing reasons."""
    entry = await setup_entry(
        hass,
        data=GLOBALS_INPUT,
        subentries=[
            {
                "data": {
                    CONF_NAME: "party",
                    CONF_PRIORITY: 50,
                    CONF_STATE: "on",
                    CONF_MEMBERSHIP: Membership.ENTITIES.value,
                    CONF_ENTITIES: [PORCH],
                    CONF_LIFETIME: Lifetime.LATCH.value,
                    CONF_MAX_HOLD: {"hours": 12, "minutes": 0, "seconds": 0},
                },
                "subentry_type": SUBENTRY_REASON,
                "title": "party",
                "unique_id": "reason:party",
            }
        ],
    )

    result = await start_subentry(hass, entry, SUBENTRY_SOURCE)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_AUTOMATION: PARTY_OFF,
            CONF_ACTION: SourceAction.CLOSES.value,
            CONF_REASON: "party",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{PARTY_OFF} closes party"


# -- YAML bootstrap and export ------------------------------------------------

IMPORT_YAML = {
    CONF_BYPASS_ENTITY: GLOBAL_OVERRIDE,
    CONF_DEFAULT_MODE: Mode.GUARD.value,
    CONF_OVERRIDE_SOURCES: [PORCH_BUTTON_HELD],
    CONF_REASONS: [
        {
            CONF_NAME: "daily",
            CONF_LIFETIME: Lifetime.WINDOW.value,
            CONF_PRIORITY: 20,
            CONF_ENTITIES: [PORCH],
            CONF_START: {CONF_KIND: "time", CONF_TIME: "07:00:00"},
            CONF_END: {CONF_KIND: "time", CONF_TIME: "22:00:00"},
        },
        {
            CONF_NAME: "party",
            CONF_LIFETIME: Lifetime.LATCH.value,
            CONF_PRIORITY: 50,
            CONF_ENTITIES: [PORCH],
            CONF_MAX_HOLD: "12:00:00",
        },
    ],
    CONF_SWITCHES: [{CONF_ENTITY_ID: PORCH, CONF_MODE: Mode.GUARD.value}],
    CONF_SOURCES: [
        {CONF_AUTOMATION: PARTY_ON, CONF_ACTION: "opens", CONF_REASON: "party"},
        {CONF_AUTOMATION: PARTY_OFF, CONF_ACTION: "closes", CONF_REASON: "party"},
    ],
}


async def test_yaml_import_bootstraps_the_whole_config(
    hass: HomeAssistant, helpers, switches
):
    """A prepared YAML block is adopted in one restart, subentries and all."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=IMPORT_YAML
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    by_type: dict[str, list] = {}
    for sub in entry.subentries.values():
        by_type.setdefault(sub.subentry_type, []).append(sub)

    assert len(by_type[SUBENTRY_REASON]) == 2
    assert len(by_type[SUBENTRY_SWITCH]) == 1
    assert len(by_type[SUBENTRY_SOURCE]) == 2

    # Durations survive the trip to storage as JSON.
    party = next(
        s for s in by_type[SUBENTRY_REASON] if s.data[CONF_NAME] == "party"
    )
    assert party.data[CONF_MAX_HOLD] == {"hours": 12, "minutes": 0, "seconds": 0}

    hub = entry.runtime_data
    assert set(hub.config.reasons) == {"daily", "party"}
    assert hub.config.sources[PARTY_OFF][0].action is SourceAction.CLOSES
    assert hub.problems == []


async def test_export_config_round_trips(hass: HomeAssistant, helpers, switches):
    """export_config gives back YAML that describes the same configuration."""
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=IMPORT_YAML
    )
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN, SERVICE_EXPORT_CONFIG, {}, blocking=True, return_response=True
    )

    exported = response["config"]
    assert exported[CONF_BYPASS_ENTITY] == GLOBAL_OVERRIDE
    assert {r[CONF_NAME] for r in exported[CONF_REASONS]} == {"daily", "party"}
    assert [s[CONF_ENTITY_ID] for s in exported[CONF_SWITCHES]] == [PORCH]
    assert len(exported[CONF_SOURCES]) == 2
    assert "arbiter:" in response["yaml"]


async def test_mapping_a_window_reason_is_reported(hass: HomeAssistant, helpers, switches):
    """A window opens and closes on its own schedule; mapping a source to it is a mistake."""
    bad = {
        **IMPORT_YAML,
        CONF_SOURCES: [
            {CONF_AUTOMATION: DAILY_ON, CONF_ACTION: "opens", CONF_REASON: "daily"}
        ],
    }
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=bad
    )
    await hass.async_block_till_done()

    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert any("window reason" in problem for problem in hub.problems)


async def test_import_is_refused_when_already_set_up(hass: HomeAssistant):
    """Import is a bootstrap, not a live sync: it does not overwrite the GUI."""
    MockConfigEntry(domain=DOMAIN, data=GLOBALS_INPUT).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=IMPORT_YAML
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"

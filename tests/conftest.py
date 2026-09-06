"""Shared test fixtures."""

from __future__ import annotations

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockToggleEntity,
    setup_test_component_platform,
)

from custom_components.arbiter.const import (
    CONF_ACTION,
    CONF_AUTOMATION,
    CONF_BYPASS_ENTITY,
    CONF_DEFAULT_MODE,
    CONF_DEFAULT_STATE,
    CONF_END,
    CONF_ENTITIES,
    CONF_ENTITY_ID,
    CONF_LIFETIME,
    CONF_MAX_HOLD,
    CONF_MEMBERSHIP,
    CONF_MODE,
    CONF_NAME,
    CONF_OVERRIDE_SOURCES,
    CONF_PRIORITY,
    CONF_REASON,
    CONF_START,
    CONF_STATE,
    DOMAIN,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    Lifetime,
    Membership,
    Mode,
)

pytest_plugins = "pytest_homeassistant_custom_component"

PORCH = "switch.porch"
HALL = "switch.hall"

DAILY_ON = "automation.daily_lights_on"
DAILY_OFF = "automation.daily_lights_off"
PARTY_ON = "automation.party_lights_on"
PARTY_OFF = "automation.party_lights_off"
PORCH_BUTTON_HELD = "automation.porch_button_held"

GLOBAL_OVERRIDE = "input_boolean.global_override"


def subentry(subentry_type: str, title: str, data: dict) -> dict:
    """Build a subentry in the shape MockConfigEntry expects."""
    return {
        "data": data,
        "subentry_type": subentry_type,
        "title": title,
        "unique_id": f"{subentry_type}:{title}",
    }


def reason_subentry(
    name: str,
    *,
    priority: int,
    state: str = "on",
    entities: list[str] | None = None,
    lifetime: Lifetime = Lifetime.LATCH,
    max_hold: dict | None = None,
    start: dict | None = None,
    end: dict | None = None,
    **extra,
) -> dict:
    """Build a reason subentry."""
    data = {
        CONF_NAME: name,
        CONF_PRIORITY: priority,
        CONF_STATE: state,
        CONF_MEMBERSHIP: Membership.ENTITIES.value,
        CONF_ENTITIES: entities or [PORCH],
        CONF_LIFETIME: lifetime.value,
        **extra,
    }
    if lifetime is Lifetime.LATCH:
        data[CONF_MAX_HOLD] = max_hold or {"hours": 12, "minutes": 0, "seconds": 0}
    else:
        data[CONF_START] = start
        data[CONF_END] = end
    return subentry(SUBENTRY_REASON, name, data)


def switch_subentry(
    entity_id: str, *, mode: Mode = Mode.GUARD, default_state: str = "off"
) -> dict:
    """Build a managed-switch subentry."""
    return subentry(
        SUBENTRY_SWITCH,
        entity_id,
        {
            CONF_ENTITY_ID: entity_id,
            CONF_MODE: mode.value,
            CONF_DEFAULT_STATE: default_state,
        },
    )


def source_subentry(automation: str, action: str, reason: str) -> dict:
    """Build an automation-to-reason mapping subentry."""
    title = f"{automation} {action} {reason}"
    return subentry(
        SUBENTRY_SOURCE,
        title,
        {CONF_AUTOMATION: automation, CONF_ACTION: action, CONF_REASON: reason},
    )


@pytest.fixture
async def helpers(hass: HomeAssistant) -> None:
    """Set up the input helpers the schedules are built on."""
    assert await async_setup_component(
        hass,
        "input_datetime",
        {
            "input_datetime": {
                "turn_on_lights_daily": {"has_date": False, "has_time": True},
                "turn_off_lights_daily": {"has_date": False, "has_time": True},
            }
        },
    )
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"global_override": None}}
    )
    await hass.async_block_till_done()


class RegisteredToggle(MockToggleEntity):
    """A toggle that lands in the entity registry, so it can carry labels and an area.

    Real Z-Wave switches are registered; without a unique id the registry has no
    entry to attach a label to, and label-scoped reasons could not be tested.
    """

    def __init__(self, name: str, state: str) -> None:
        super().__init__(name, state)
        self._attr_unique_id = f"arbiter-test-{name}"

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id


@pytest.fixture
async def switches(hass: HomeAssistant) -> None:
    """Provide real, controllable switches.

    Bare ``hass.states.async_set`` entries look right but cannot be driven by a
    service call, so the applier would silently do nothing.
    """
    entities = [RegisteredToggle("porch", "off"), RegisteredToggle("hall", "off")]
    setup_test_component_platform(hass, SWITCH_DOMAIN, entities)
    assert await async_setup_component(
        hass, SWITCH_DOMAIN, {SWITCH_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()


@pytest.fixture
async def local_timezone(hass: HomeAssistant) -> None:
    """Pin the instance timezone, since window boundaries are local times."""
    await hass.config.async_set_time_zone("America/New_York")


async def setup_entry(hass: HomeAssistant, *, data: dict, subentries: list[dict]):
    """Create and set up an Arbiter config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Arbiter", data=data, subentries_data=subentries
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


DEFAULT_GLOBALS = {
    CONF_DEFAULT_MODE: Mode.GUARD.value,
    CONF_BYPASS_ENTITY: GLOBAL_OVERRIDE,
    CONF_OVERRIDE_SOURCES: [PORCH_BUTTON_HELD],
}

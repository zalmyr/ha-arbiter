"""Fixtures for tests that boot a real Home Assistant instance."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/arbiter during these tests."""
    yield


@pytest.fixture(autouse=True)
async def core_services(hass: HomeAssistant) -> None:
    """Register homeassistant.turn_on / turn_off, which the applier calls.

    Always present on a real instance; the test harness starts without it.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()

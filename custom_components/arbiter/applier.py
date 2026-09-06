"""Turn a decision into a service call."""

from __future__ import annotations

import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, Context, HomeAssistant

from .attribution import Attributor
from .const import DesiredState

_LOGGER = logging.getLogger(__name__)


async def async_apply(
    hass: HomeAssistant,
    entity_id: str,
    state: DesiredState,
    attributor: Attributor,
) -> bool:
    """Drive ``entity_id`` to ``state``; return True if a call was made.

    Uses ``homeassistant.turn_on`` / ``turn_off`` so the same code path works for
    switches, lights, fans and input_booleans. The call carries a context the
    attributor has been told about, so the resulting ``state_changed`` is not
    mistaken for somebody overriding the arbiter.
    """
    if state is DesiredState.UNMANAGED:
        return False

    current = hass.states.get(entity_id)
    if current is None:
        _LOGGER.debug("Not applying to %s: entity does not exist", entity_id)
        return False
    if current.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug("Not applying to %s: state is %s", entity_id, current.state)
        return False
    if current.state == state.value:
        # Already where it should be; issuing the call anyway would just add noise
        # to the logbook and to every other integration listening for changes.
        return False

    context = Context()
    attributor.async_note_own(context)

    service = SERVICE_TURN_ON if state is DesiredState.ON else SERVICE_TURN_OFF
    _LOGGER.debug("Applying %s to %s", service, entity_id)
    await hass.services.async_call(
        HA_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=False,
        context=context,
    )
    return True

"""Resolve a reason's membership into concrete switch entity ids.

Most conflicts in a real config hide behind a label or an area rather than an
explicit entity list, so a reason has to be able to name its switches the same way
an automation's ``target:`` does.
"""

from __future__ import annotations

import logging

from homeassistant.const import ATTR_AREA_ID, ATTR_ENTITY_ID, ATTR_LABEL_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)

from .const import Membership
from .models import ReasonDef

_LOGGER = logging.getLogger(__name__)


@callback
def async_expand_target(hass: HomeAssistant, selector: dict) -> set[str]:
    """Expand a ``target:``-shaped dict into entity ids.

    Accepts the same keys a service call target does (``entity_id``, ``area_id``,
    ``label_id``, ``device_id``, ``floor_id``) and returns both directly and
    indirectly referenced entities, since a label or area reaches its switches
    indirectly.
    """
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(selector))
    return selected.referenced | selected.indirectly_referenced


@callback
def async_reason_targets(hass: HomeAssistant, reason: ReasonDef) -> frozenset[str]:
    """Return the switch entity ids a reason currently applies to.

    Resolution happens when a reason opens, so a live reason keeps acting on the
    switches it matched at that moment even if a label's membership changes later.
    """
    match reason.membership:
        case Membership.ENTITIES:
            selector = {ATTR_ENTITY_ID: list(reason.entities)}
        case Membership.LABEL:
            selector = {ATTR_LABEL_ID: reason.label}
        case Membership.AREA:
            selector = {ATTR_AREA_ID: reason.area}

    entities = async_expand_target(hass, selector)
    if not entities:
        _LOGGER.warning(
            "Reason %r resolved to no entities (%s); it will have no effect",
            reason.name,
            reason.membership,
        )
    return frozenset(entities)

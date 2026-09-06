"""GUI setup for Arbiter.

Reasons, managed switches and automation mappings are repeatable objects, so each
is a config subentry: they get their own add / edit / delete rows on the
integration page rather than being buried in one enormous form.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback

from .const import (
    CONF_ACTION,
    CONF_AUTOMATION,
    CONF_END,
    CONF_ENTITY_ID,
    CONF_KIND,
    CONF_LIFETIME,
    CONF_MEMBERSHIP,
    CONF_NAME,
    CONF_REASON,
    CONF_REASONS,
    CONF_SOURCES,
    CONF_START,
    CONF_SWITCHES,
    DOMAIN,
    SUBENTRY_REASON,
    SUBENTRY_SOURCE,
    SUBENTRY_SWITCH,
    BoundaryKind,
    Lifetime,
    Membership,
)
from .schema import (
    CONFIG_YAML,
    boundary_kind_schema,
    boundary_schema,
    global_schema,
    latch_schema,
    membership_schema,
    reason_basics_schema,
    source_schema,
    switch_schema,
    window_extras_schema,
)

_LOGGER = logging.getLogger(__name__)

TITLE = "Arbiter"


class ArbiterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the single Arbiter entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the instance-wide settings."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title=TITLE, data=user_input)

        return self.async_show_form(step_id="user", data_schema=global_schema())

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Seed the entry from a YAML block.

        A one-time bootstrap so a prepared config can be adopted in a single
        restart. After this the GUI is authoritative.
        """
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        config = CONFIG_YAML(import_data)
        subentries: list[ConfigSubentryData] = []

        for reason in config.pop(CONF_REASONS, []):
            subentries.append(
                ConfigSubentryData(
                    data=_jsonify(reason),
                    subentry_type=SUBENTRY_REASON,
                    title=reason[CONF_NAME],
                    unique_id=f"reason:{reason[CONF_NAME]}",
                )
            )
        for switch in config.pop(CONF_SWITCHES, []):
            subentries.append(
                ConfigSubentryData(
                    data=_jsonify(switch),
                    subentry_type=SUBENTRY_SWITCH,
                    title=switch[CONF_ENTITY_ID],
                    unique_id=f"switch:{switch[CONF_ENTITY_ID]}",
                )
            )
        for source in config.pop(CONF_SOURCES, []):
            title = f"{source[CONF_AUTOMATION]} {source[CONF_ACTION]} {source[CONF_REASON]}"
            subentries.append(
                ConfigSubentryData(
                    data=_jsonify(source),
                    subentry_type=SUBENTRY_SOURCE,
                    title=title,
                    unique_id=f"source:{title}",
                )
            )

        _LOGGER.info("Imported Arbiter config with %d subentries", len(subentries))
        return self.async_create_entry(
            title=TITLE, data=_jsonify(config), subentries=subentries
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return ArbiterOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Declare the repeatable objects this integration manages."""
        return {
            SUBENTRY_REASON: ReasonSubentryFlow,
            SUBENTRY_SWITCH: SwitchSubentryFlow,
            SUBENTRY_SOURCE: SourceSubentryFlow,
        }


class ArbiterOptionsFlow(OptionsFlow):
    """Edit the instance-wide settings.

    Reloading is handled by the entry's update listener rather than
    ``OptionsFlowWithReload``, because adding or editing a subentry fires the same
    listener and those changes have to take effect too.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the same form the initial setup used, prefilled."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=global_schema(current)
        )


class ReasonSubentryFlow(ConfigSubentryFlow):
    """Add or edit one reason.

    Multi-step because the lifetime branches: a latch needs a bound, a window needs
    two independently specified edges.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # -- entry points ---------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new reason."""
        return await self.async_step_basics(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing reason, prefilled with what it currently says."""
        self._data = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_basics()

    # -- steps ----------------------------------------------------------------

    async def async_step_basics(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name, priority, what it asks for, how it is scoped and how it ends."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_membership()

        return self.async_show_form(
            step_id="basics", data_schema=reason_basics_schema(self._data)
        )

    async def async_step_membership(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Which switches this reason covers."""
        membership = Membership(self._data[CONF_MEMBERSHIP])

        if user_input is not None:
            self._data.update(user_input)
            if Lifetime(self._data[CONF_LIFETIME]) is Lifetime.LATCH:
                return await self.async_step_latch()
            return await self.async_step_start_kind()

        return self.async_show_form(
            step_id="membership",
            data_schema=membership_schema(membership, self._data),
            description_placeholders={"membership": membership.value},
        )

    async def async_step_latch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """A latch's bound, so a missed close cannot pin the switches."""
        if user_input is not None:
            self._data.update(user_input)
            return self._finish()

        return self.async_show_form(
            step_id="latch", data_schema=latch_schema(self._data)
        )

    async def async_step_start_kind(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """How the window's start is expressed."""
        if user_input is not None:
            self._data.setdefault(CONF_START, {})[CONF_KIND] = user_input[CONF_KIND]
            return await self.async_step_start_value()

        return self.async_show_form(
            step_id="start_kind",
            data_schema=boundary_kind_schema(self._data.get(CONF_START, {})),
        )

    async def async_step_start_value(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The value the window's start needs."""
        kind = BoundaryKind(self._data[CONF_START][CONF_KIND])

        if user_input is not None:
            self._data[CONF_START].update(user_input)
            return await self.async_step_end_kind()

        return self.async_show_form(
            step_id="start_value",
            data_schema=boundary_schema(kind, self._data[CONF_START]),
            description_placeholders={"kind": kind.value},
        )

    async def async_step_end_kind(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """How the window's end is expressed — independent of the start."""
        if user_input is not None:
            self._data.setdefault(CONF_END, {})[CONF_KIND] = user_input[CONF_KIND]
            return await self.async_step_end_value()

        return self.async_show_form(
            step_id="end_kind",
            data_schema=boundary_kind_schema(self._data.get(CONF_END, {})),
        )

    async def async_step_end_value(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The value the window's end needs."""
        kind = BoundaryKind(self._data[CONF_END][CONF_KIND])

        if user_input is not None:
            self._data[CONF_END].update(user_input)
            return await self.async_step_window_extras()

        return self.async_show_form(
            step_id="end_value",
            data_schema=boundary_schema(kind, self._data[CONF_END]),
            description_placeholders={"kind": kind.value},
        )

    async def async_step_window_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Optional weekday filter and condition."""
        if user_input is not None:
            self._data.update(user_input)
            return self._finish()

        return self.async_show_form(
            step_id="window_extras", data_schema=window_extras_schema(self._data)
        )

    # -- completion -----------------------------------------------------------

    @callback
    def _finish(self) -> SubentryFlowResult:
        """Create or update, depending on how this flow was entered."""
        title = self._data[CONF_NAME]
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=self._data,
            )
        return self.async_create_entry(
            title=title, data=self._data, unique_id=f"reason:{title}"
        )


class SwitchSubentryFlow(ConfigSubentryFlow):
    """Add or edit one managed switch."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a switch for the arbiter to manage."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_ENTITY_ID],
                data=user_input,
                unique_id=f"switch:{user_input[CONF_ENTITY_ID]}",
            )
        return self.async_show_form(step_id="user", data_schema=switch_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a managed switch."""
        subentry = self._get_reconfigure_subentry()
        current = dict(subentry.data)

        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data={**current, **user_input},
            )

        return self.async_show_form(
            step_id="reconfigure", data_schema=switch_schema(current)
        )


class SourceSubentryFlow(ConfigSubentryFlow):
    """Map an automation onto the reason its commands open or close.

    This is the screen that decides whether "turn off" means *the lights must be
    off* or *my reason has ended* — the difference between an event ending taking
    the lights down mid-morning and leaving the daily schedule holding them up.
    """

    @callback
    def _reason_names(self) -> list[str]:
        """Offer the reasons that already exist, so the mapping cannot dangle."""
        entry = self._get_entry()
        return sorted(
            subentry.data[CONF_NAME]
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_REASON
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a mapping."""
        names = self._reason_names()
        if not names:
            return self.async_abort(reason="no_reasons")

        if user_input is not None:
            return self.async_create_entry(title=_source_title(user_input), data=user_input)

        return self.async_show_form(step_id="user", data_schema=source_schema(names))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a mapping."""
        subentry = self._get_reconfigure_subentry()
        current = dict(subentry.data)

        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=_source_title(user_input),
                data=user_input,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=source_schema(self._reason_names(), current),
        )


def _source_title(data: dict[str, Any]) -> str:
    """Phrase a mapping as the sentence it is."""
    return f"{data[CONF_AUTOMATION]} {data[CONF_ACTION]} {data[CONF_REASON]}"


def _jsonify(value: Any) -> Any:
    """Make voluptuous output storable.

    ``cv.time_period`` yields timedeltas and ``cv.template`` yields Template
    objects; config entries have to round-trip through JSON.
    """
    from datetime import timedelta

    from homeassistant.helpers.template import Template

    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        return {
            "hours": total // 3600,
            "minutes": (total % 3600) // 60,
            "seconds": total % 60,
        }
    if isinstance(value, Template):
        return value.template
    return value

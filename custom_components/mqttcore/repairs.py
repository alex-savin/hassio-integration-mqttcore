"""Repairs: a token without the maintenance right gets a new one by signing in again."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, LOGGER, TOKEN_SCOPES
from .pymqttcore import MqttcoreError, MqttcoreNotFoundError


def token_issue_id(entry_id: str) -> str:
    """The repair raised while the entry's token lacks rights the integration uses."""
    return f"token_rights_{entry_id}"


def missing_rights(scopes: dict[str, str]) -> list[str]:
    """The areas where the token has less than the integration mints today."""
    rank = {"none": 0, "read": 1, "write": 2}
    return [
        area
        for area, want in TOKEN_SCOPES.items()
        if rank.get(scopes.get(area, "none"), 0) < rank[want]
    ]


async def async_check_token(hass: HomeAssistant, entry: Any) -> None:
    """Raise or clear the repair after asking the cluster what the token may do."""
    # Any node knows the token (the cluster shares them): the first one that
    # is reachable by its own route, not the cluster's name.
    node = next(iter(entry.runtime_data.nodes.nodes.values()), None)
    if node is None:
        return
    try:
        token = await node.async_call(lambda c, n: c.get_token_self(node_id=n))
    except MqttcoreNotFoundError:
        return  # a cluster before 0.30.0: it cannot grant the rest yet
    except MqttcoreError as err:
        LOGGER.debug("Could not read the token's scopes: %s", err)
        return
    missing = missing_rights(token.scopes)
    issue_id = token_issue_id(entry.entry_id)
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="token_rights",
        translation_placeholders={"title": entry.title, "areas": ", ".join(missing)},
        data={"entry_id": entry.entry_id},
    )


class TokenRightsRepairFlow(RepairsFlow):
    """Confirm, then start the entry's re-authentication: it mints a new token."""

    def __init__(self, entry_id: str) -> None:
        """Remember the entry to sign in again."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Start at the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask once; on yes, open the sign-in that mints the new token."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if user_input is not None:
            if entry is not None:
                entry.async_start_reauth(self.hass)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"title": entry.title if entry else ""},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The flow for a fixable repair of this integration."""
    entry_id = str((data or {}).get("entry_id", ""))
    return TokenRightsRepairFlow(entry_id)

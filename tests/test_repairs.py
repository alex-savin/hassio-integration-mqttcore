"""Tests for the token repair: a token without the rights the integration uses."""

from __future__ import annotations

import copy

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.repairs import (
    TokenRightsRepairFlow,
    missing_rights,
    token_issue_id,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .common import TOKEN_SELF, URL01, mock_cluster


def test_missing_rights() -> None:
    """Write covers read; what is below what the flow mints is listed."""
    assert missing_rights(TOKEN_SELF["scopes"]) == []
    assert missing_rights({"status": "read", "system": "read"}) == [
        "clients",
        "access",
        "maintenance",
    ]
    assert missing_rights({**TOKEN_SELF["scopes"], "system": "write"}) == []


async def test_old_token_raises_a_fixable_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Integration 0.1's read-only token: a repair, whose fix signs in again."""
    old = copy.deepcopy(TOKEN_SELF)
    old["scopes"] = {
        "status": "read",
        "clients": "none",
        "access": "none",
        "system": "read",
        "maintenance": "none",
    }
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/agents/self"): (200, old)})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        "mqttcore", token_issue_id(mock_config_entry.entry_id)
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {
        "title": mock_config_entry.title,
        "areas": "clients, access, maintenance",
    }
    flow = TokenRightsRepairFlow(mock_config_entry.entry_id)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["step_id"] == "confirm"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == SOURCE_REAUTH
        for f in hass.config_entries.flow.async_progress_by_handler("mqttcore")
    )


async def test_current_token_no_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A token with every right raises nothing."""
    assert (
        ir.async_get(hass).async_get_issue(
            "mqttcore", token_issue_id(init_integration.entry_id)
        )
        is None
    )

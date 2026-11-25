"""Tests for the token repair: a token without the rights the integration uses."""

from __future__ import annotations

import copy

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.repairs import (
    TokenRightsRepairFlow,
    missing_rights,
    retained_issue_id,
    token_issue_id,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .common import TOKEN_SELF, URL01, mock_cluster, refresh_all, view


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


async def test_retained_set_near_full(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The node says the set is near its ceiling: a problem and a repair, until it is not."""
    issues = ir.async_get(hass)
    issue_id = retained_issue_id(init_integration.entry_id)
    assert issues.async_get_issue("mqttcore", issue_id) is None
    cluster = view("mqtt01")
    cluster["raft"]["retained"] |= {"bytes": 955000, "topics": 1030, "near_full": True}
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/cluster"): (200, cluster)})
    await refresh_all(hass, init_integration)
    assert hass.states.get("binary_sensor.mqtt_example_com_retained_set").state == "on"
    assert hass.states.get("sensor.mqtt_example_com_retained_set_used").state == "91.1"
    issue = issues.async_get_issue("mqttcore", issue_id)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_placeholders == {
        "title": init_integration.title,
        "used": "91",
        "kib": "932",
        "ceiling_kib": "1024",
        "topics": "1030",
    }
    # Under 85 % again, as the node says: gone.
    cluster["raft"]["retained"] |= {"bytes": 880000, "near_full": False}
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/cluster"): (200, cluster)})
    await refresh_all(hass, init_integration)
    assert hass.states.get("binary_sensor.mqtt_example_com_retained_set").state == "off"
    assert issues.async_get_issue("mqttcore", issue_id) is None


async def test_retained_set_near_full_before_0_34(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node that does not say near_full: 90 % of its ceiling decides."""
    cluster = view("mqtt01")
    del cluster["raft"]["retained"]["near_full"]
    cluster["raft"]["retained"]["bytes"] = 950000
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/cluster"): (200, cluster)})
    await refresh_all(hass, init_integration)
    assert hass.states.get("binary_sensor.mqtt_example_com_retained_set").state == "on"
    # Without a ceiling (before 0.33.1) there is nothing to say.
    del cluster["raft"]["retained"]["ceiling"]
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/cluster"): (200, cluster)})
    await refresh_all(hass, init_integration)
    assert (
        hass.states.get("binary_sensor.mqtt_example_com_retained_set").state
        == "unavailable"
    )
    assert (
        ir.async_get(hass).async_get_issue(
            "mqttcore", retained_issue_id(init_integration.entry_id)
        )
        is None
    )

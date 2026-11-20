"""Tests for the mqttcore diagnostics."""

from __future__ import annotations

import json

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .common import TOKEN, TOKEN_NAME


async def test_diagnostics_redacted(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """Tokens, names, addresses and the MAC never leave."""
    assert await async_setup_component(hass, "diagnostics", {})
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    text = json.dumps(result)

    for secret in (
        TOKEN,
        TOKEN_NAME,
        "example.com",  # every DNS name, URL, certificate subject and zone
        "192.0.2.",  # every address
        "00:00:5e:00:53:09",  # the board's MAC
        "acct/123456789",  # the ACME account
    ):
        assert secret not in text, secret

    assert result["entry"]["data"]["token"] == REDACTED
    assert result["entry"]["title"] == REDACTED
    assert result["cluster"]["endpoint"] == REDACTED
    member = result["cluster"]["cluster"]["members"][0]
    assert member["address"] == REDACTED
    assert member["host_name"] == REDACTED
    assert member["name"] == "mqttcore-005309"  # node names stay: they help
    assert result["cluster"]["tls"]["dns_names"] == REDACTED
    assert result["cluster"]["acme"]["state"] == "ok"
    assert len(result["nodes"]) == 3
    board = result["nodes"]["node_0"]
    assert board["available"] is True
    assert board["device"]["mac"] == REDACTED
    assert board["stats"]["connections"] == 12

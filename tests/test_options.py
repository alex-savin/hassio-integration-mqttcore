"""Tests for the listeners options flow (phase 4): the OTA password each time."""

from __future__ import annotations

import json
from typing import Any

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .common import OTA_PASSWORD, URL01, load

FORM: dict[str, Any] = {
    "mqtt_enabled": True,
    "mqtt_port": 1883,
    "mqtts_enabled": True,
    "mqtts_port": 8883,
    "ws_enabled": False,
    "ws_port": 8083,
    "wss_enabled": True,
    "wss_port": 8084,
    "http_enabled": True,
    "http_port": 80,
    "https_enabled": True,
    "https_port": 443,
    "ws_path": "/mqtt",
}


async def _open(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"ota_password": OTA_PASSWORD}
    )


async def test_change_one_listener(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Only what changed is sent, with the OTA password and no token."""
    aioclient_mock.get(f"{URL01}/api/config", json=load("config.json"))
    aioclient_mock.post(
        f"{URL01}/api/config",
        json={
            "status": "ok",
            "message": "listener settings stored; used from the next start",
        },
    )
    result = await _open(hass, init_integration)
    assert result["step_id"] == "listeners"
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORM)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "listeners_saved"
    (_, _, body, headers) = next(
        c
        for c in aioclient_mock.mock_calls
        if c[0] == "POST" and str(c[1]).endswith("/api/config")
    )
    assert json.loads(body) == {"listeners": {"ws": {"enabled": False, "port": 8083}}}
    assert headers["X-OTA-Password"] == OTA_PASSWORD
    assert "Authorization" not in headers
    assert init_integration.options == {}  # the password is kept nowhere


async def test_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The cluster's reason shows on the form."""
    aioclient_mock.get(f"{URL01}/api/config", json=load("config.json"))
    aioclient_mock.post(
        f"{URL01}/api/config",
        status=400,
        json={"error": "listeners: mqtt and ws are both on port 1883"},
    )
    result = await _open(hass, init_integration)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**FORM, "ws_enabled": True, "ws_port": 1883}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "listeners_refused"}
    assert result["description_placeholders"] == {
        "error": "listeners: mqtt and ws are both on port 1883"
    }


async def test_wrong_password(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A wrong OTA password stays on the first step."""
    aioclient_mock.get(
        f"{URL01}/api/config", status=401, json={"error": "wrong OTA password"}
    )
    result = await _open(hass, init_integration)
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_nothing_changed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Submitting the same settings sends nothing."""
    aioclient_mock.get(f"{URL01}/api/config", json=load("config.json"))
    result = await _open(hass, init_integration)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**FORM, "ws_enabled": True}
    )
    assert result["reason"] == "listeners_unchanged"
    assert not any(
        c[0] == "POST" and str(c[1]).endswith("/api/config")
        for c in aioclient_mock.mock_calls
    )

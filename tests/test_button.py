"""Tests for the restart and certificate buttons (mqttcore 0.30.0, `maintenance`)."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .common import URL01

RESTART = "button.mqtt01_example_com_restart"
RENEW = "button.mqtt_example_com_renew_certificate"


@pytest.mark.usefixtures("init_integration")
async def test_buttons(hass: HomeAssistant) -> None:
    """A restart button for the board only, and the cluster's renewal."""
    assert hass.states.get(RESTART) is not None
    assert hass.states.get("button.mqtt04_example_com_restart") is None
    state = hass.states.get(RENEW)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE


async def test_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Pressing restart asks the board itself."""
    aioclient_mock.post(f"{URL01}/api/restart", status=202, json={"status": "ok"})
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: RESTART}, blocking=True
    )
    assert any(str(c[1]) == f"{URL01}/api/restart" for c in aioclient_mock.mock_calls)


async def test_renew_certificate(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Pressing renew orders a certificate; a refusal says why."""
    aioclient_mock.post(
        f"{URL01}/api/acme/issue",
        status=409,
        json={"error": "an issuance is already running"},
    )
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: RENEW}, blocking=True
        )
    assert err.value.translation_key == "maintenance_failed"
    assert err.value.translation_placeholders == {
        "error": "an issuance is already running"
    }

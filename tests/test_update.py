"""Tests for the boards' firmware update entities (mqttcore 0.30.0)."""

from __future__ import annotations

import copy
import json

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .common import NODE01, URL01, load, mock_cluster

ENTITY = "update.mqtt01_example_com_firmware"


def _ota(**changes: object) -> dict[str, object]:
    data = copy.deepcopy(load("ota_mqtt01.json"))
    data.update(changes)
    return data


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    ota: dict[str, object] | None = None,
) -> None:
    overrides = {} if ota is None else {(URL01, "/api/ota"): (200, ota)}
    mock_cluster(aioclient_mock, overrides=overrides)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_boards_only(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The board has one, offered 0.30.1; the Linux nodes have none."""
    await _setup(hass, mock_config_entry, aioclient_mock)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INSTALLED_VERSION] == "0.30.0"
    assert state.attributes[ATTR_LATEST_VERSION] == "0.30.1"
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert hass.states.get("update.mqtt04_example_com_firmware") is None
    assert hass.states.get("update.mqtt05_example_com_firmware") is None


async def test_install_and_progress(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Install asks the board for the offered version, then follows its progress."""
    await _setup(hass, mock_config_entry, aioclient_mock)
    aioclient_mock.post(
        f"{URL01}/api/update/install", status=202, json={"status": "started"}
    )
    firmware = hass.data[UPDATE_DOMAIN].get_entity(ENTITY).coordinator
    # The board downloads, then runs the new version.
    downloading = _ota(
        update={
            "state": "downloading",
            "version": "0.30.1",
            "message": "",
            "bytes": 443392,
            "total": 1773568,
            "seconds": 3,
        }
    )
    aioclient_mock._mocks = [
        m
        for m in aioclient_mock._mocks
        if not str(m.url).endswith("/api/ota") and "path=/api/ota" not in str(m.url)
    ]
    aioclient_mock.get(f"{URL01}/api/ota", json=downloading)
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    await hass.async_block_till_done()
    installs = [
        c
        for c in aioclient_mock.mock_calls
        if str(c[1]).endswith("/api/update/install")
    ]
    assert len(installs) == 1
    assert json.loads(installs[0][2]) == {"version": "0.30.1"}
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes[ATTR_IN_PROGRESS] is True
    assert state.attributes[ATTR_UPDATE_PERCENTAGE] == 25

    aioclient_mock._mocks = [
        m for m in aioclient_mock._mocks if not str(m.url).endswith("/api/ota")
    ]
    aioclient_mock.get(f"{URL01}/api/ota", json=_ota(version="0.30.1"))
    await firmware.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == "0.30.1"


async def test_install_refused_without_maintenance(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A token without maintenance write gets a clear error, not a stack trace."""
    await _setup(hass, mock_config_entry, aioclient_mock)
    aioclient_mock.post(
        f"{URL01}/api/update/install",
        status=403,
        json={"error": "this API token's scopes do not reach this route"},
    )
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert err.value.translation_key == "token_cannot_maintain"


async def test_only_the_offered_version(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No version but the offered one can be asked for."""
    await _setup(hass, mock_config_entry, aioclient_mock)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: ENTITY, "version": "0.29.0"},
            blocking=True,
        )
    # Home Assistant refuses it: the entity installs no version but the offered one.
    assert err.value.translation_key == "specific_version_not_supported"


async def test_release_notes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The notes come from the update server, without credentials."""
    await _setup(hass, mock_config_entry, aioclient_mock)
    aioclient_mock.get(
        "https://ota.example.com/p4/0.30.1/notes.md",
        text="**MQTT Core 0.30.1**\n- fixes",
    )
    entity = hass.data[UPDATE_DOMAIN].get_entity(ENTITY)
    assert await entity.async_release_notes() == "**MQTT Core 0.30.1**\n- fixes"
    notes = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("notes.md")]
    assert notes
    assert "Authorization" not in (notes[0][3] or {})


async def test_rolled_back_raises_a_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A rolled-back update is a repair until a later update succeeds."""
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        ota=_ota(rolled_back={"partition": "ota_0", "version": "0.30.1"}),
    )
    issue_id = f"rolled_back_{mock_config_entry.entry_id}_{NODE01}"
    issue = ir.async_get(hass).async_get_issue("mqttcore", issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {
        "node": "mqtt01.example.com",
        "version": "0.30.1",
        "running": "0.30.0",
    }


async def test_nothing_newer(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With nothing newer, the entity is off at the running version."""
    current = copy.deepcopy(load("update_check_mqtt01.json"))
    current.update(update_available=False, latest_version="0.30.0", verdict="current")
    aioclient_mock.post(f"{URL01}/api/update/check", json=current)
    await _setup(hass, mock_config_entry, aioclient_mock)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_LATEST_VERSION] == "0.30.0"

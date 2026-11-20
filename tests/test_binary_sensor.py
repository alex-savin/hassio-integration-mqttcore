"""Tests for the mqttcore binary sensors."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from .common import NODE01, URL01, load, mock_cluster, refresh_all, view


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    """An entity's state, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("binary_sensor.mqtt_example_com_raft", "off"),
        ("binary_sensor.mqtt_example_com_shared_settings", "off"),
        ("binary_sensor.mqtt_example_com_certificate", "off"),
        ("binary_sensor.mqtt_example_com_load_balancing", "on"),
        ("binary_sensor.mqtt01_example_com_alive", "on"),
        ("binary_sensor.mqtt01_example_com_leader", "on"),
        ("binary_sensor.mqtt04_example_com_leader", "off"),
        ("binary_sensor.mqtt01_example_com_draining", "off"),
        ("binary_sensor.mqtt01_example_com_crash_dump", "off"),
        ("binary_sensor.mqtt01_example_com_redirecting_new_clients", "off"),
        ("binary_sensor.mqtt04_example_com_redirecting_new_clients", "on"),
        # Each node's own links: mqtt04's link to mqtt05 is still saying hello.
        ("binary_sensor.mqtt01_example_com_link_to_mqtt04", "on"),
        ("binary_sensor.mqtt01_example_com_link_to_mqtt05", "on"),
        ("binary_sensor.mqtt04_example_com_link_to_mqtt01", "on"),
        ("binary_sensor.mqtt04_example_com_link_to_mqtt05", "off"),
        ("binary_sensor.mqtt05_example_com_link_to_mqtt04", "on"),
    ],
)
async def test_states(hass: HomeAssistant, entity_id: str, expected: str) -> None:
    """Each binary sensor reads its field."""
    assert state_of(hass, entity_id) == expected


@pytest.mark.usefixtures("init_integration")
async def test_board_only(hass: HomeAssistant) -> None:
    """The crash dump exists on boards only."""
    assert hass.states.get("binary_sensor.mqtt04_example_com_crash_dump") is None


@pytest.mark.usefixtures("init_integration")
async def test_device_classes(hass: HomeAssistant) -> None:
    """Problems are problems, links and members are connectivity."""
    for entity_id, device_class in (
        ("binary_sensor.mqtt_example_com_raft", BinarySensorDeviceClass.PROBLEM),
        ("binary_sensor.mqtt_example_com_certificate", BinarySensorDeviceClass.PROBLEM),
        (
            "binary_sensor.mqtt01_example_com_alive",
            BinarySensorDeviceClass.CONNECTIVITY,
        ),
        (
            "binary_sensor.mqtt01_example_com_link_to_mqtt04",
            BinarySensorDeviceClass.CONNECTIVITY,
        ),
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes[ATTR_DEVICE_CLASS] == device_class


async def test_problems(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No Raft leader, settings out of sync, a failed renewal, a dead member."""
    cluster = view("mqtt01")
    cluster["raft"]["leader"] = None
    cluster["leader"] = None
    for member in cluster["members"]:
        if member["name"] == "mqttcore-bfbfb5":
            member["config_state"] = "diverged"
        if member["name"] == "mqttcore-e05e05":
            member["state"] = "dead"
    acme = load("acme_mqtt01.json") | {"state": "failed", "error": "DNS-01 failed"}
    # The certificate is read every ten minutes: force it now.
    init_integration.runtime_data.cluster._certificate_at = None
    aioclient_mock.clear_requests()
    mock_cluster(
        aioclient_mock,
        overrides={
            (URL01, "/api/cluster"): (200, cluster),
            (URL01, "/api/acme"): (200, acme),
        },
    )
    await refresh_all(hass, init_integration)
    assert state_of(hass, "binary_sensor.mqtt_example_com_raft") == "on"
    assert state_of(hass, "binary_sensor.mqtt_example_com_shared_settings") == "on"
    assert state_of(hass, "binary_sensor.mqtt_example_com_certificate") == "on"
    assert state_of(hass, "binary_sensor.mqtt05_example_com_alive") == "off"
    assert state_of(hass, "sensor.mqtt_example_com_members_alive") == "2"
    assert state_of(hass, "sensor.mqtt_example_com_leader") == "unknown"
    assert state_of(hass, "sensor.mqtt_example_com_certificate_renewal") == "failed"


async def test_member_entities_when_the_cluster_is_unreachable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without any member answering, the member-list entities go unavailable."""
    aioclient_mock.clear_requests()
    mock_cluster(
        aioclient_mock,
        down={URL01, "https://mqtt04.example.com", "https://mqtt05.example.com:8443"},
    )
    await refresh_all(hass, init_integration)
    assert state_of(hass, "binary_sensor.mqtt01_example_com_alive") == STATE_UNAVAILABLE
    assert state_of(hass, "binary_sensor.mqtt_example_com_raft") == STATE_UNAVAILABLE
    assert (
        init_integration.runtime_data.nodes.nodes[NODE01].last_update_success is False
    )

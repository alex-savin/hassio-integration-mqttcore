"""Tests for the mqttcore sensors."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.const import DOMAIN
from homeassistant.components.sensor import (
    ATTR_OPTIONS,
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import (
    NODE01,
    URL01,
    URL04,
    URL05,
    Clock,
    cluster_unique_id,
    load,
    mock_cluster,
    prepend,
    refresh_all,
)


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    """An entity's state, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        # the cluster
        ("sensor.mqtt_example_com_members_alive", "3"),
        ("sensor.mqtt_example_com_members", "3"),
        ("sensor.mqtt_example_com_leader", "mqtt01.example.com"),
        ("sensor.mqtt_example_com_connections", "22"),
        ("sensor.mqtt_example_com_retained_messages", "212"),
        ("sensor.mqtt_example_com_failed_logins", "8"),
        ("sensor.mqtt_example_com_certificate_expiry", "2027-01-30T08:11:59+00:00"),
        ("sensor.mqtt_example_com_certificate_renewal", "ok"),
        (
            "sensor.mqtt_example_com_last_certificate_renewal",
            "2026-11-01T08:12:00+00:00",
        ),
        ("sensor.mqtt_example_com_messages_received", STATE_UNKNOWN),
        # the board
        ("sensor.mqtt01_example_com_connections", "12"),
        ("sensor.mqtt01_example_com_sessions", "16"),
        ("sensor.mqtt01_example_com_subscriptions", "36"),
        ("sensor.mqtt01_example_com_retained_messages", "212"),
        ("sensor.mqtt01_example_com_failed_logins", "5"),
        ("sensor.mqtt01_example_com_dropped_messages", "3"),
        ("sensor.mqtt01_example_com_version", "0.29.1"),
        ("sensor.mqtt01_example_com_internal_memory_free", "179.0"),
        ("sensor.mqtt01_example_com_psram_free", "26.0"),
        ("sensor.mqtt01_example_com_raft_store_errors", "0"),
        ("sensor.mqtt01_example_com_member_state", "alive"),
        # a Linux node
        ("sensor.mqtt04_example_com_connections", "7"),
        ("sensor.mqtt04_example_com_raft_store_errors", "2"),
        ("sensor.mqtt05_example_com_failed_logins", "1"),
    ],
)
async def test_states(hass: HomeAssistant, entity_id: str, expected: str) -> None:
    """Each sensor reads its field."""
    assert state_of(hass, entity_id) == expected


@pytest.mark.usefixtures("init_integration")
async def test_host_has_no_memory_sensors(hass: HomeAssistant) -> None:
    """The host reports free memory as SIZE_MAX: no sensor for it."""
    assert hass.states.get("sensor.mqtt04_example_com_internal_memory_free") is None
    assert hass.states.get("sensor.mqtt05_example_com_psram_free") is None


@pytest.mark.usefixtures("init_integration")
async def test_attributes(hass: HomeAssistant) -> None:
    """Units, classes and categories."""
    rate = hass.states.get("sensor.mqtt01_example_com_messages_received")
    assert rate is not None
    assert rate.attributes[ATTR_UNIT_OF_MEASUREMENT] == "msg/s"
    assert rate.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT

    logins = hass.states.get("sensor.mqtt_example_com_failed_logins")
    assert logins is not None
    assert logins.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING

    expiry = hass.states.get("sensor.mqtt_example_com_certificate_expiry")
    assert expiry is not None
    assert expiry.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP

    renewal = hass.states.get("sensor.mqtt_example_com_certificate_renewal")
    assert renewal is not None
    assert renewal.attributes[ATTR_OPTIONS] == ["idle", "running", "ok", "failed"]

    psram = hass.states.get("sensor.mqtt01_example_com_psram_free")
    assert psram is not None
    assert psram.attributes[ATTR_UNIT_OF_MEASUREMENT] == "MiB"

    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get("sensor.mqtt01_example_com_version")
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("init_integration")
async def test_raft_term_disabled_by_default(hass: HomeAssistant) -> None:
    """The Raft term is there, but off until wanted."""
    entity_registry = er.async_get(hass)
    entity_id = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, cluster_unique_id("raft_term")
    )
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_rates(
    hass: HomeAssistant,
    clock: Clock,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Rates come from two readings: per node, and summed for the cluster."""
    later: dict[tuple[str, str], tuple[int, Any]] = {}
    for short, url, extra in (
        ("mqtt01", URL01, 3000),
        ("mqtt04", URL04, 1500),
        ("mqtt05", URL05, 300),
    ):
        stats = load(f"stats_{short}.json")
        stats["uptime_s"] += 30
        stats["messages_in"] += extra
        stats["messages_out"] += 2 * extra
        stats["bytes_in"] += 120 * extra
        later[(url, "/api/stats")] = (200, stats)
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides=later)
    clock.now += 30
    await refresh_all(hass, init_integration)

    assert state_of(hass, "sensor.mqtt01_example_com_messages_received") == "100.0"
    assert state_of(hass, "sensor.mqtt01_example_com_messages_sent") == "200.0"
    assert state_of(hass, "sensor.mqtt04_example_com_messages_received") == "50.0"
    # 120 bytes a message, 100 messages a second: 12 kB/s.
    assert state_of(hass, "sensor.mqtt01_example_com_data_received") == "12.0"
    assert state_of(hass, "sensor.mqtt01_example_com_data_sent") == "0.0"
    assert state_of(hass, "sensor.mqtt_example_com_messages_received") == "160.0"
    assert state_of(hass, "sensor.mqtt_example_com_messages_sent") == "320.0"


async def test_uptime_keeps_still(
    hass: HomeAssistant,
    clock: Clock,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The start time does not move with each reading, only with a restart."""
    before = state_of(hass, "sensor.mqtt04_example_com_uptime")
    stats = load("stats_mqtt04.json")
    stats["uptime_s"] += 7  # a few seconds of jitter against the wall clock
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL04, "/api/stats"): (200, stats)})
    await refresh_all(hass, init_integration)
    assert state_of(hass, "sensor.mqtt04_example_com_uptime") == before

    stats["uptime_s"] = 5  # restarted
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL04, "/api/stats"): (200, stats)})
    await refresh_all(hass, init_integration)
    assert state_of(hass, "sensor.mqtt04_example_com_uptime") != before


async def test_node_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node that answers neither directly nor through the relay goes unavailable."""
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, down={URL04})
    await refresh_all(hass, init_integration)
    assert state_of(hass, "sensor.mqtt04_example_com_connections") == STATE_UNAVAILABLE
    # The cluster's totals carry on with the nodes that answered; the
    # connections come from the gossip, so they still count mqtt04.
    assert state_of(hass, "sensor.mqtt_example_com_failed_logins") == "6"
    assert state_of(hass, "sensor.mqtt_example_com_connections") == "22"
    # The member list still says how mqtt04 stands.
    assert state_of(hass, "sensor.mqtt04_example_com_member_state") == "alive"


async def test_certificate_without_system_scope(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A token without system read: no certificate values, the rest works."""
    refused = (403, {"error": "this API token's scopes do not reach this route"})
    mock_cluster(
        aioclient_mock,
        overrides={(URL01, "/api/tls/status"): refused, (URL01, "/api/acme"): refused},
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        state_of(hass, "sensor.mqtt_example_com_certificate_expiry")
        == STATE_UNAVAILABLE
    )
    assert (
        state_of(hass, "sensor.mqtt_example_com_certificate_renewal")
        == STATE_UNAVAILABLE
    )
    assert state_of(hass, "sensor.mqtt_example_com_members_alive") == "3"


async def test_certificate_from_a_board_through_the_relay(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Read through a Linux node, the certificate is asked of the board."""
    mock_cluster(aioclient_mock, down={URL01})
    # mqtt01's API is unreachable from Home Assistant, but its peer link is up.
    for path, name in (
        ("/api/tls/status", "tls_status_mqtt01.json"),
        ("/api/acme", "acme_mqtt01.json"),
    ):
        prepend(
            aioclient_mock,
            f"{URL04}/api/peer?node={NODE01}&path={path}",
            json=load(name),
        )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        state_of(hass, "sensor.mqtt_example_com_certificate_expiry")
        == "2027-01-30T08:11:59+00:00"
    )
    assert state_of(hass, "sensor.mqtt_example_com_certificate_renewal") == "ok"

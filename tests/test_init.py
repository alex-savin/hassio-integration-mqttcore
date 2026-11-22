"""Tests for setting up mqttcore and following the cluster."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientConnectionError
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.const import CONF_KNOWN_URLS, DOMAIN, LOCAL_NODE
from custom_components.mqttcore.coordinator import MqttcoreNodeCoordinator
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .common import (
    AT_ADDRESS,
    NODE01,
    NODE04,
    NODE05,
    UNIQUE_ID,
    URL01,
    URL04,
    URL05,
    Clock,
    entry_unique_id,
    get_device,
    load,
    mock_cluster,
    prepend,
    refresh_all,
    view,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A cluster device and one device per node, the nodes pointing to it."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED

    cluster = get_device(hass, entry, f"cluster-{UNIQUE_ID}")
    assert cluster is not None
    assert cluster.name == UNIQUE_ID
    assert cluster.model == "Cluster"

    board = get_device(hass, entry, NODE01)
    assert board is not None
    assert board.name == "mqtt01.example.com"
    assert board.model == "ESP32-P4 board"
    assert board.model_id == "esp32p4"
    assert board.hw_version == "v3.2"
    assert board.sw_version == "0.29.1"
    assert board.via_device_id == cluster.id
    assert board.configuration_url == "https://mqtt01.example.com/"
    assert (dr.CONNECTION_NETWORK_MAC, "00:00:5e:00:53:09") in board.connections

    for node_id, name, url in (
        (NODE04, "mqtt04.example.com", "https://mqtt04.example.com/"),
        (NODE05, "mqtt05.example.com", "https://mqtt05.example.com:8443/"),
    ):
        host = get_device(hass, entry, node_id)
        assert host is not None
        assert host.name == name
        assert host.model == "Linux node"
        assert host.configuration_url == url
        assert host.via_device_id == cluster.id

    nodes = entry.runtime_data.nodes
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not nodes.nodes


async def test_setup_retry_when_no_member_answers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Every member down: try again later."""
    mock_cluster(aioclient_mock, down={URL01, URL04, URL05})
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("status", [401, 403])
async def test_token_rejected_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    status: int,
) -> None:
    """A token every member refuses (revoked, or not for this network) asks for reauth."""
    mock_cluster(
        aioclient_mock,
        overrides={
            (url, "/api/cluster"): (status, {"error": "no"})
            for url in (URL01, URL04, URL05)
        },
    )
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize("status", [401, 403])
async def test_one_member_refusing_the_token_is_not_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """One member refusing the token is that member's: the cluster is read elsewhere.

    A container behind Docker Desktop's NAT sees Home Assistant from its
    gateway, outside the token's networks; a member whose copy of the token
    has not arrived yet answers 401. Neither means the token is gone.
    """
    refusal = {"error": "this API token is not for this network"}
    mock_cluster(aioclient_mock, overrides={(URL01, "/api/cluster"): (status, refusal)})
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    cluster = mock_config_entry.runtime_data.cluster
    assert cluster.active_url == f"{URL04}/"
    assert cluster.candidate_urls()[-1] == f"{URL01}/"  # asked last for a while
    assert caplog.text.count("refuses the API token") == 1
    await cluster.async_refresh()
    assert (
        caplog.text.count("refuses the API token") == 1
    )  # said once, not every refresh


async def test_one_refusal_with_the_rest_unreachable_waits(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One member refuses and the others do not answer: retry later, no reauth."""
    refusal = {"error": "this API token is not for this network"}
    mock_cluster(
        aioclient_mock,
        down={URL04, URL05},
        overrides={(URL01, "/api/cluster"): (403, refusal)},
    )
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_failover_to_another_member(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With the configured node down, the cluster is read through another member."""
    mock_cluster(aioclient_mock, down={URL01})
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    runtime = mock_config_entry.runtime_data
    assert runtime.cluster.active_url == f"{URL04}/"
    # mqtt01 itself is down; the others answer.
    assert not runtime.nodes.nodes[NODE01].last_update_success
    assert runtime.nodes.nodes[NODE04].last_update_success
    state = hass.states.get("binary_sensor.mqtt01_example_com_alive")
    assert state is not None
    assert state.state == "on"  # as the others see it: the gossip says alive here
    state = hass.states.get("sensor.mqtt01_example_com_connections")
    assert state is not None
    assert state.state == "unavailable"
    # The certificate is a board's: asked of mqtt01 through mqtt04's relay, which fails.
    assert runtime.cluster.data.tls is None


async def test_failed_url_is_tried_last(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
) -> None:
    """A node that failed waits at the end of the queue, then comes first again."""
    mock_cluster(aioclient_mock, down={URL01})
    await _setup(hass, mock_config_entry)
    cluster = mock_config_entry.runtime_data.cluster
    assert cluster.candidate_urls()[-1] == f"{URL01}/"
    clock.now += 301
    assert cluster.candidate_urls()[0] == f"{URL01}/"


async def test_node_read_through_relay(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node Home Assistant cannot reach directly is read through the relay."""
    # mqtt05 is unreachable from Home Assistant, but linked to mqtt01.
    mock_cluster(aioclient_mock)
    for base in (URL05, AT_ADDRESS[URL05]):
        for path in ("/api/cluster", "/api/stats", "/api/device"):
            prepend(aioclient_mock, f"{base}{path}", exc=ClientConnectionError())
    await _setup(hass, mock_config_entry)
    node = mock_config_entry.runtime_data.nodes.nodes[NODE05]
    assert node.last_update_success
    assert node.data.relayed
    state = hass.states.get("sensor.mqtt05_example_com_connections")
    assert state is not None
    assert state.state == "3"
    # Once failed, the relay comes first.
    routes = node._routes()
    assert routes[0].relay_node == NODE05


async def test_known_urls_follow_the_members(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The entry keeps the members' URLs for the next start."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_KNOWN_URLS: []}
    )
    mock_cluster(aioclient_mock)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.data[CONF_KNOWN_URLS] == [
        "https://mqtt01.example.com/",
        "https://mqtt04.example.com/",
        "https://mqtt05.example.com:8443/",
    ]


async def test_member_joins_and_leaves(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
) -> None:
    """A member that joins gets a device; one gone for half an hour loses it."""
    entry = mock_config_entry
    two = {
        (url, "/api/cluster"): (200, view(short, drop=[NODE05]))
        for short, url in (("mqtt01", URL01), ("mqtt04", URL04))
    }
    mock_cluster(aioclient_mock, overrides=two)
    await _setup(hass, mock_config_entry)
    runtime = mock_config_entry.runtime_data
    entity_registry = er.async_get(hass)
    assert set(runtime.nodes.nodes) == {NODE01, NODE04}
    assert get_device(hass, entry, NODE05) is None
    assert hass.states.get("binary_sensor.mqtt01_example_com_link_to_mqtt05") is None

    # mqtt05 joins.
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock)
    await refresh_all(hass, mock_config_entry)
    assert set(runtime.nodes.nodes) == {NODE01, NODE04, NODE05}
    assert get_device(hass, entry, NODE05) is not None
    state = hass.states.get("sensor.mqtt05_example_com_connections")
    assert state is not None
    assert state.state == "3"
    for entity_id in (
        "binary_sensor.mqtt01_example_com_link_to_mqtt05",
        "binary_sensor.mqtt05_example_com_link_to_mqtt04",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"

    # mqtt05 is gone: kept for a while...
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides=two)
    await refresh_all(hass, mock_config_entry)
    assert NODE05 in runtime.nodes.nodes
    clock.now += 600
    await refresh_all(hass, mock_config_entry)
    assert NODE05 in runtime.nodes.nodes

    # ...then removed, with the links to it.
    clock.now += 1300
    await refresh_all(hass, mock_config_entry)
    assert set(runtime.nodes.nodes) == {NODE01, NODE04}
    assert get_device(hass, entry, NODE05) is None
    assert (
        entity_registry.async_get_entity_id(
            Platform.BINARY_SENSOR, DOMAIN, entry_unique_id(NODE01, f"link_{NODE05}")
        )
        is None
    )
    assert hass.states.get("binary_sensor.mqtt01_example_com_link_to_mqtt05") is None

    # It comes back: everything again, links included.
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock)
    await refresh_all(hass, mock_config_entry)
    state = hass.states.get("binary_sensor.mqtt01_example_com_link_to_mqtt05")
    assert state is not None
    assert state.state == "on"


async def test_lone_node(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node without a cluster: one node device, no cluster-only values."""
    entry = mock_config_entry
    mock_cluster(
        aioclient_mock,
        overrides={(URL01, "/api/cluster"): (200, load("cluster_disabled.json"))},
    )
    await _setup(hass, mock_config_entry)
    runtime = mock_config_entry.runtime_data
    assert set(runtime.nodes.nodes) == {LOCAL_NODE}
    device = get_device(hass, entry, f"{UNIQUE_ID}-{LOCAL_NODE}")
    assert device is not None
    assert device.name == "mqttcore-005309"
    assert device.model == "ESP32-P4 board"
    state = hass.states.get("sensor.mqtt_example_com_members_alive")
    assert state is not None
    assert state.state == "unavailable"
    state = hass.states.get("sensor.mqtt_example_com_connections")
    assert state is not None
    assert state.state == "12"
    # Without a cluster the retained count is the node's.
    state = hass.states.get("sensor.mqtt_example_com_retained_messages")
    assert state is not None
    assert state.state == "212"
    assert hass.states.get("sensor.mqttcore_005309_member_state") is None


async def test_device_registry_follows_an_update(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A new firmware version reaches the node's device entry."""
    entry = init_integration
    newer: dict[str, Any] = load("device_mqtt01.json") | {"version": "0.30.0"}
    cluster = view("mqtt01")
    for member in cluster["members"]:
        if member["node_id"] == NODE01:
            member["version"] = "0.30.0"
    aioclient_mock.clear_requests()
    mock_cluster(
        aioclient_mock,
        overrides={
            (URL01, "/api/device"): (200, newer),
            (URL01, "/api/cluster"): (200, cluster),
        },
    )
    await refresh_all(hass, init_integration)
    device = get_device(hass, entry, NODE01)
    assert device is not None
    assert device.sw_version == "0.30.0"


async def test_remove_stale_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Only a node the cluster no longer lists may be removed by hand."""
    entry = init_integration
    assert await async_setup_component(hass, "config", {})
    device_registry = dr.async_get(hass)
    from custom_components.mqttcore import async_remove_config_entry_device  # noqa: PLC0415

    current = get_device(hass, entry, NODE04)
    cluster = get_device(hass, entry, f"cluster-{UNIQUE_ID}")
    assert current is not None
    assert cluster is not None
    stale = device_registry.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "0" * 32)}
    )
    assert not await async_remove_config_entry_device(hass, init_integration, current)
    assert not await async_remove_config_entry_device(hass, init_integration, cluster)
    assert await async_remove_config_entry_device(hass, init_integration, stale)


async def test_rates_after_a_restart(
    hass: HomeAssistant,
    clock: Clock,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Counters that went down (a restart) give no rate, not a negative one."""
    node: MqttcoreNodeCoordinator = init_integration.runtime_data.nodes.nodes[NODE04]
    restarted = load("stats_mqtt04.json") | {"uptime_s": 10, "messages_in": 5}
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(URL04, "/api/stats"): (200, restarted)})
    clock.now += 30
    await node.async_refresh()
    assert node.data.rates.messages_in is None
    assert node.data.stats.uptime_s == 10


async def test_configured_by_the_cluster_name(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Set up by the cluster's name (any member answers it): nodes are read by their own names."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, "host": "mqtt.example.com"}
    )
    mock_cluster(aioclient_mock)
    aioclient_mock.get("https://mqtt.example.com/api/cluster", json=view("mqtt04"))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = mock_config_entry.runtime_data
    assert runtime.cluster.active_url == "https://mqtt.example.com/"
    assert all(n.last_update_success for n in runtime.nodes.nodes.values())
    assert not any(n.data.relayed for n in runtime.nodes.nodes.values())
    # Only the cluster view was asked of the cluster's name.
    asked = {
        (url.host, url.path)
        for _, url, _, _ in aioclient_mock.mock_calls
        if url.host == "mqtt.example.com"
    }
    assert asked == {("mqtt.example.com", "/api/cluster")}
    # The certificate came from the board, through mqtt04's relay at mqtt04's own name.
    assert runtime.cluster.data.tls is not None
    assert runtime.cluster.data.tls.has_details


async def test_another_node_answering_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node URL that reaches another member is not taken for the node."""
    mock_cluster(aioclient_mock)
    # mqtt05's name reaches mqtt04 (a stale DNS record, say); its address is right.
    prepend(aioclient_mock, f"{URL05}/api/cluster", json=view("mqtt04"))
    await _setup(hass, mock_config_entry)
    node = mock_config_entry.runtime_data.nodes.nodes[NODE05]
    assert node.last_update_success
    assert not node.data.relayed
    assert node.data.cluster is not None
    assert node.data.cluster.node_id == NODE05
    assert any(
        str(url).startswith(f"{AT_ADDRESS[URL05]}/api/stats")
        for _, url, _, _ in aioclient_mock.mock_calls
    )


async def test_another_node_at_both_is_read_through_relay(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """When the name and the address both reach another member, the relay serves."""
    mock_cluster(aioclient_mock)
    for base in (URL05, AT_ADDRESS[URL05]):
        prepend(aioclient_mock, f"{base}/api/cluster", json=view("mqtt04"))
    await _setup(hass, mock_config_entry)
    node = mock_config_entry.runtime_data.nodes.nodes[NODE05]
    assert node.last_update_success
    assert node.data.relayed
    assert node.data.cluster is not None
    assert node.data.cluster.node_id == NODE05


async def test_member_names_that_do_not_resolve(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Members whose names the resolver does not know are read at their addresses.

    The cluster's own name resolves, and reaches mqtt04, a Linux node: the
    certificate comes from the board through mqtt04's relay, at mqtt04's
    address, and every node answers on the first refresh.
    """
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_HOST: UNIQUE_ID, CONF_KNOWN_URLS: []},
    )
    mock_cluster(aioclient_mock, unresolved=[URL01, URL04, URL05])
    aioclient_mock.get(f"https://{UNIQUE_ID}/api/cluster", json=view("mqtt04"))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    expiry = hass.states.get("sensor.mqtt_example_com_certificate_expiry")
    assert expiry is not None
    assert expiry.state == "2027-01-30T08:11:59+00:00"
    problem = hass.states.get("binary_sensor.mqtt_example_com_certificate")
    assert problem is not None
    assert problem.state == "off"
    assert any(
        str(url).startswith(f"{AT_ADDRESS[URL04]}/api/peer")
        and url.query.get("node") == NODE01
        and url.query.get("path") == "/api/tls/status"
        for _, url, _, _ in aioclient_mock.mock_calls
    )
    for node in mock_config_entry.runtime_data.nodes.nodes.values():
        assert node.last_update_success
        assert not node.data.relayed  # each at its address
    assert "Error fetching" not in caplog.text


async def test_certificate_comes_again_on_the_next_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A certificate reading that reached no node is tried again at once."""
    mock_cluster(aioclient_mock)
    for base in (URL01, AT_ADDRESS[URL01]):
        for path in ("/api/tls/status", "/api/acme"):
            prepend(aioclient_mock, f"{base}{path}", exc=ClientConnectionError())
    await _setup(hass, mock_config_entry)
    expiry = hass.states.get("sensor.mqtt_example_com_certificate_expiry")
    assert expiry is not None
    assert expiry.state == "unavailable"

    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock)
    await refresh_all(hass, mock_config_entry)
    expiry = hass.states.get("sensor.mqtt_example_com_certificate_expiry")
    assert expiry is not None
    assert expiry.state == "2027-01-30T08:11:59+00:00"

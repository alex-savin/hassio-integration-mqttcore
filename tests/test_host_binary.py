"""Against the real broker: the mqttcore host binary, started by the test.

Skipped unless `MQTTCORE_BIN` names the binary (built from the mqttcore
repository's `esp32-p4-firmware/host` with CMake). The nodes run on
loopback over plain HTTP, where mqttcore accepts the OTA password and API
tokens from the node's own host only: no certificate is needed.

`test_one_node` runs a cluster of one (a few seconds). `test_three_nodes`
runs a cluster of three and waits for its leader and for the shared
settings to reach every member (about 70 to 90 s); it needs
`MQTTCORE_CLUSTER_TEST=1` as well.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import suppress
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

import pytest

from custom_components.mqttcore.const import (
    CONF_OTA_PASSWORD,
    CONF_RESTRICT_NETWORK,
    DOMAIN,
    SECTION_CONNECTION,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

MQTTCORE_BIN = os.environ.get("MQTTCORE_BIN", "")
OTA_PASSWORD = "integration-test-password"

pytestmark = [
    pytest.mark.host_binary,
    pytest.mark.skipif(
        not MQTTCORE_BIN or not Path(MQTTCORE_BIN).is_file(),
        reason="set MQTTCORE_BIN to the mqttcore host binary",
    ),
]


def _free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 (loopback, http)
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def _wait(what: str, check: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with suppress(OSError, ValueError):
            if check():
                return
        time.sleep(0.25)
    raise TimeoutError(f"mqttcore: {what} within {timeout:.0f} s")


@dataclass
class Node:
    """One running host binary."""

    admin_port: int
    gossip_port: int
    setup_token: str
    process: subprocess.Popen[bytes]
    log: Path

    @property
    def url(self) -> str:
        """The admin API's base URL."""
        return f"http://127.0.0.1:{self.admin_port}"

    def get(self, path: str, **headers: str) -> tuple[int, Any]:
        """GET a route."""
        return _http("GET", f"{self.url}{path}", headers=headers)


@dataclass
class Cluster:
    """Host binaries on loopback."""

    root: Path
    secret: Path
    nodes: list[Node] = field(default_factory=list)

    def start(self) -> Node:
        """Start one more node, seeded with the first."""
        index = len(self.nodes) + 1
        data = self.root / f"n{index}"
        data.mkdir()
        token_file = data / "setup-token"
        setup_token = secrets.token_hex(32)
        token_file.write_text(setup_token + "\n")
        admin, mqtt, peer = _free_port(), _free_port(), _free_port()
        gossip = _free_port(socket.SOCK_DGRAM)
        args = [
            MQTTCORE_BIN,
            "--data-dir", str(data / "data"),
            "--log-level", "warn",
            "--admin-listen", f"127.0.0.1:{admin}",
            "--admin-token-file", str(token_file),
            "--listen", f"127.0.0.1:{mqtt}",
            "--cluster-name", "itest",
            "--cluster-secret-file", str(self.secret),
            "--cluster-insecure",
            "--gossip-listen", f"127.0.0.1:{gossip}",
            "--peer-listen", f"127.0.0.1:{peer}",
            "--advertise", "127.0.0.1",
        ]  # fmt: skip
        if self.nodes:
            args += ["--seed", f"127.0.0.1:{self.nodes[0].gossip_port}"]
        log = data / "log"
        with log.open("wb") as out:
            process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
        node = Node(admin, gossip, setup_token, process, log)
        self.nodes.append(node)
        _wait(
            f"node {index} answers", lambda: node.get("/api/auth/status")[0] == 200, 15
        )
        return node

    def stop(self) -> None:
        """Stop every node."""
        for node in self.nodes:
            node.process.terminate()
        for node in self.nodes:
            try:
                node.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                node.process.kill()


@pytest.fixture
def cluster(socket_enabled: None) -> Generator[Cluster]:
    """A place for host binaries; stopped and removed after the test."""
    # A short path: the binary takes a data directory of under 120 bytes.
    root = Path(tempfile.mkdtemp(prefix="mqc-"))
    secret = root / "secret"
    secret.write_text(secrets.token_hex(32) + "\n")
    running = Cluster(root, secret)
    try:
        yield running
    finally:
        running.stop()
        shutil.rmtree(root, ignore_errors=True)


def _setup(node: Node) -> None:
    status, body = _http(
        "POST",
        f"{node.url}/api/auth/setup",
        {"setup_token": node.setup_token, "ota_password": OTA_PASSWORD},
    )
    assert status == 200, body


async def _configure(hass: HomeAssistant, node: Node) -> Any:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: node.admin_port,
            SECTION_CONNECTION: {CONF_SSL: False, CONF_VERIFY_SSL: False},
        },
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "credentials", result
    # An address is suggested (the tests' network helper answers a fixed one).
    restrict = next(
        k for k in result["data_schema"].schema if k == CONF_RESTRICT_NETWORK
    )
    assert restrict.description["suggested_value"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: "127.0.0.1"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return result["result"]


def _state(hass: HomeAssistant, platform: Platform, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


async def test_one_node(hass: HomeAssistant, cluster: Cluster) -> None:
    """Set up against a real node: the token is minted, the entities read it."""
    node = cluster.start()
    _setup(node)
    status, version = node.get("/api/device", **{"X-OTA-Password": OTA_PASSWORD})
    assert status == 200

    entry = await _configure(hass, node)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "cluster:itest"
    assert entry.title == "itest"

    # The token is in the node, limited to loopback: it reads, maintains
    # (no expiry: mqttcore 0.30.0), and changes users, the ACL and clients.
    status, agents = node.get("/api/agents", **{"X-OTA-Password": OTA_PASSWORD})
    assert status == 200
    (token,) = agents["tokens"]
    assert token["name"].startswith("home-assistant-")
    assert token["scopes"] == {
        "status": "read",
        "clients": "write",
        "access": "write",
        "system": "read",
        "maintenance": "write",
    }
    assert token["networks"] == ["127.0.0.1/32"]
    assert token["expires_at"] == 0

    status, view = node.get(
        "/api/cluster", Authorization=f"Bearer {entry.data['token']}"
    )
    assert status == 200
    node_id = view["node_id"]
    cluster_key = "cluster-cluster:itest"
    assert _state(hass, Platform.SENSOR, f"{cluster_key}_members_alive") == "1"
    assert _state(hass, Platform.SENSOR, f"{node_id}_version") == version["version"]
    assert _state(hass, Platform.SENSOR, f"{node_id}_connections") == "0"
    assert _state(hass, Platform.BINARY_SENSOR, f"{node_id}_alive") == "on"
    # The host has no certificate details and no ACME: unavailable, not wrong.
    assert (
        _state(hass, Platform.SENSOR, f"{cluster_key}_certificate_expiry")
        == "unavailable"
    )
    # The host reports no free memory: no sensor for it.
    assert (
        er.async_get(hass).async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{node_id}_memory_internal_free"
        )
        is None
    )

    # Revoked behind Home Assistant's back: the next refresh asks for reauth.
    status, _ = _http(
        "POST",
        f"{node.url}/api/agents",
        {"action": "revoke", "name": token["name"]},
        {"X-OTA-Password": OTA_PASSWORD},
    )
    assert status == 200
    await entry.runtime_data.cluster.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.skipif(
    os.environ.get("MQTTCORE_CLUSTER_TEST") != "1",
    reason="set MQTTCORE_CLUSTER_TEST=1 for the three-node test (about 90 s)",
)
async def test_three_nodes(hass: HomeAssistant, cluster: Cluster) -> None:
    """A cluster of three: one entry, a device per node, the others read through the relay."""
    first = cluster.start()
    cluster.start()
    cluster.start()
    _wait(
        "three members alive",
        lambda: (
            sum(m["state"] == "alive" for m in first.get("/api/cluster")[1]["members"])
            == 3
        ),
        30,
    )
    _wait("a leader", lambda: first.get("/api/cluster")[1]["leader"] is not None, 150)
    _setup(first)
    _wait(
        "the OTA password on every member",
        lambda: all(
            not n.get("/api/auth/status")[1]["setup_needed"] for n in cluster.nodes
        ),
        60,
    )

    entry = await _configure(hass, first)
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert len(runtime.nodes.nodes) == 3

    # The others have no HTTPS URL here: they are read through the first's relay.
    for node in runtime.nodes.nodes.values():
        await node.async_refresh()
    await hass.async_block_till_done()
    relayed = [
        n for n in runtime.nodes.nodes.values() if n.data is not None and n.data.relayed
    ]
    assert len(relayed) == 2
    assert all(n.last_update_success for n in runtime.nodes.nodes.values())

    cluster_key = "cluster-cluster:itest"
    assert _state(hass, Platform.SENSOR, f"{cluster_key}_members_alive") == "3"
    assert _state(hass, Platform.SENSOR, f"{cluster_key}_leader") != "unknown"
    links_up = [
        _state(hass, Platform.BINARY_SENSOR, f"{a}_link_{b}")
        for a in runtime.nodes.nodes
        for b in runtime.nodes.nodes
        if a != b
    ]
    assert links_up == ["on"] * 6

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

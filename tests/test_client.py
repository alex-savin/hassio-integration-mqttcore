"""Tests for pymqttcore, the client library (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
from typing import Any
from unittest.mock import Mock

from aiohttp import (
    ClientConnectionError,
    ClientConnectorCertificateError,
    ClientSession,
)
import pytest
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.pymqttcore import (
    AcmeStatus,
    ClusterStatus,
    Device,
    MqttcoreAuthenticationError,
    MqttcoreBadRequestError,
    MqttcoreClient,
    MqttcoreConflictError,
    MqttcoreConnectionError,
    MqttcoreForbiddenError,
    MqttcoreHostNotAllowedError,
    MqttcoreInvalidResponseError,
    MqttcoreNotFoundError,
    MqttcoreNotSupportedError,
    MqttcoreRateLimitedError,
    MqttcoreResponseError,
    MqttcoreTlsError,
    MqttcoreUnavailableError,
    Stats,
    TlsStatus,
    TokenInfo,
    build_url,
)

from .common import NODE01, NODE04, NODE05, TOKEN, load

BASE = "https://mqtt01.example.com"


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """A session bound to the mocker."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def client(session: ClientSession) -> MqttcoreClient:
    """A client with a token."""
    return MqttcoreClient(session, f"{BASE}/", token=TOKEN)


@pytest.mark.parametrize(
    ("host", "port", "ssl", "expected"),
    [
        ("mqtt01.example.com", 443, True, "https://mqtt01.example.com/"),
        ("mqtt01.example.com", None, True, "https://mqtt01.example.com/"),
        ("mqtt05.example.com", 8443, True, "https://mqtt05.example.com:8443/"),
        ("127.0.0.1", 80, False, "http://127.0.0.1/"),
        ("127.0.0.1", 18081, False, "http://127.0.0.1:18081/"),
        ("2001:db8::1", 443, True, "https://[2001:db8::1]/"),
        ("[2001:db8::1]", 8443, True, "https://[2001:db8::1]:8443/"),
        (" mqtt01.example.com/ ", 443, True, "https://mqtt01.example.com/"),
    ],
)
def test_build_url(host: str, port: int | None, ssl: bool, expected: str) -> None:
    """Base URLs, with the port only when it is not the scheme's own."""
    assert build_url(host, port, ssl=ssl) == expected


async def test_get_with_token(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Status routes send the token and parse the answer."""
    aioclient_mock.get(f"{BASE}/api/stats", json=load("stats_mqtt01.json"))
    stats = await client.get_stats()
    assert isinstance(stats, Stats)
    assert stats.connections == 12
    assert stats.dropped == 3
    assert stats.memory_hot_free == 183296
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert str(url) == f"{BASE}/api/stats"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-OTA-Password" not in headers


async def test_relay(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """`node_id` goes through the node's relay, the path as a query value."""
    aioclient_mock.get(
        f"{BASE}/api/peer?node={NODE04}&path=/api/device",
        json=load("device_mqtt04.json"),
    )
    device = await client.get_device(node_id=NODE04)
    assert not device.is_board
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.path == "/api/peer"
    assert url.query["node"] == NODE04
    assert url.query["path"] == "/api/device"


async def test_open_route_sends_no_credentials(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The gate refuses a token on an open route: none is sent."""
    aioclient_mock.get(f"{BASE}/api/auth/status", json=load("auth_status.json"))
    status = await client.get_auth_status()
    assert status.login
    assert not status.setup_needed
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert "Authorization" not in headers


async def test_ota_password_replaces_the_token(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """With the OTA password, the token stays home."""
    aioclient_mock.get(f"{BASE}/api/config", json=load("config.json"))
    config = await client.get_config(ota_password="secret password")
    assert config.server_name == "mqtt.example.com"
    assert config.host_name == "mqtt01.example.com"
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["X-OTA-Password"] == "secret password"
    assert "Authorization" not in headers


async def test_tokens(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """List, create (with its body) and revoke."""
    aioclient_mock.get(f"{BASE}/api/agents", json=load("agents.json"))
    aioclient_mock.post(f"{BASE}/api/agents", json={"status": "ok", "token": TOKEN})
    tokens = await client.list_tokens(ota_password="pw")
    assert tokens == [
        TokenInfo(
            name="prometheus",
            scopes={
                "status": "read",
                "clients": "none",
                "access": "none",
                "system": "none",
            },
            expires_at=None,
            created=1793000000,
            networks=("192.0.2.50/32",),
        )
    ]
    token = await client.create_token(
        ota_password="pw",
        name="ha",
        scopes={"status": "read"},
        networks=["192.0.2.10/32"],
        expires_at=1800000000,
    )
    assert token == TOKEN
    await client.revoke_token(ota_password="pw", name="ha")
    create, revoke = aioclient_mock.mock_calls[1:]
    assert json.loads(create[2]) == {
        "action": "create",
        "name": "ha",
        "scopes": {"status": "read"},
        "networks": ["192.0.2.10/32"],
        "expires_at": 1800000000,
    }
    assert create[3]["Content-Type"] == "application/json"
    assert json.loads(revoke[2]) == {"action": "revoke", "name": "ha"}


async def test_create_token_without_token(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An answer without a token is not a success."""
    aioclient_mock.post(f"{BASE}/api/agents", json={"status": "ok"})
    with pytest.raises(MqttcoreInvalidResponseError):
        await client.create_token(ota_password="pw", name="ha", scopes={})


async def test_list_tokens_odd_answer(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A tokens member that is not a list is no tokens."""
    aioclient_mock.get(f"{BASE}/api/agents", json={"tokens": "none"})
    assert await client.list_tokens(ota_password="pw") == []


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, MqttcoreBadRequestError),
        (401, MqttcoreAuthenticationError),
        (403, MqttcoreForbiddenError),
        (404, MqttcoreNotFoundError),
        (409, MqttcoreConflictError),
        (421, MqttcoreHostNotAllowedError),
        (500, MqttcoreResponseError),
        (501, MqttcoreNotSupportedError),
        (502, MqttcoreUnavailableError),
        (503, MqttcoreUnavailableError),
        (504, MqttcoreUnavailableError),
    ],
)
async def test_error_statuses(
    client: MqttcoreClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[MqttcoreResponseError],
) -> None:
    """Each status has its class; the node's sentence is kept."""
    aioclient_mock.get(f"{BASE}/api/acme", status=status, json={"error": "a sentence"})
    with pytest.raises(error) as info:
        await client.get_acme()
    assert info.value.status == status
    assert info.value.message == "a sentence"
    assert info.value.path == "/api/acme"


async def test_rate_limited(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """429 keeps Retry-After."""
    aioclient_mock.get(
        f"{BASE}/api/config",
        status=429,
        json={"error": "too many wrong passwords"},
        headers={"Retry-After": "8"},
    )
    with pytest.raises(MqttcoreRateLimitedError) as info:
        await client.get_config()
    assert info.value.retry_after == 8


async def test_rate_limited_odd_header(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A Retry-After that is not a number is ignored."""
    aioclient_mock.get(
        f"{BASE}/api/config",
        status=429,
        text="slow down",
        headers={"Retry-After": "soon"},
    )
    with pytest.raises(MqttcoreRateLimitedError) as info:
        await client.get_config()
    assert info.value.retry_after is None
    assert info.value.message == "slow down"


async def test_error_without_json(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """413 and 411 come from the HTTP server itself, without JSON."""
    aioclient_mock.get(f"{BASE}/api/stats", status=413, text="")
    with pytest.raises(MqttcoreResponseError) as info:
        await client.get_stats()
    assert info.value.message == "no message"


@pytest.mark.parametrize("body", ["<html>a router</html>", "", "[1, 2]", "42"])
async def test_not_json(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker, body: str
) -> None:
    """A 200 that is not a JSON object is not mqttcore."""
    aioclient_mock.get(f"{BASE}/api/cluster", text=body)
    with pytest.raises(MqttcoreInvalidResponseError):
        await client.get_cluster()


async def test_connection_errors(
    client: MqttcoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Timeouts, refused connections and bad certificates."""
    aioclient_mock.get(f"{BASE}/api/stats", exc=TimeoutError())
    with pytest.raises(MqttcoreConnectionError, match="no answer in time"):
        await client.get_stats()
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{BASE}/api/stats",
        exc=ClientConnectorCertificateError(Mock(), Exception("expired")),
    )
    with pytest.raises(MqttcoreTlsError):
        await client.get_stats()


async def test_url_and_host(session: ClientSession) -> None:
    """The client says where it talks to."""
    client = MqttcoreClient(session, f"{BASE}/")
    assert client.url == f"{BASE}/"
    assert client.host == "mqtt01.example.com"


# ---- models ------------------------------------------------------------------------------


def test_cluster_model() -> None:
    """The cluster view as the firmware writes it."""
    cluster = ClusterStatus.from_dict(load("cluster_mqtt04.json"))
    assert cluster.enabled
    assert cluster.cluster == "house"
    assert cluster.node_id == NODE04
    assert cluster.self_member is not None
    assert cluster.self_member.node_id == NODE04
    assert cluster.leader is not None
    assert cluster.leader.label == "mqtt01.example.com"
    board = cluster.member(NODE01)
    assert board is not None
    assert board.can_order is True
    assert board.url == "https://mqtt01.example.com/"
    assert board.certificate is not None
    assert board.certificate.fingerprint == "5e1f0a77"
    app = cluster.member(NODE05)
    assert app is not None
    assert app.url == "https://mqtt05.example.com:8443/"
    assert app.label == "mqtt05.example.com"
    assert cluster.member("0" * 32) is None
    assert cluster.link_up(NODE01)
    assert not cluster.link_up(NODE05)  # still saying hello
    assert cluster.raft is not None
    assert cluster.raft.store_errors == 2
    assert cluster.raft.leader is not None
    assert cluster.raft.leader.name == "mqttcore-005309"
    assert cluster.raft.retained_topics == 212
    assert len(cluster.raft.members) == 3
    assert cluster.balance is not None
    assert cluster.balance.redirecting
    assert cluster.drain is not None
    assert not cluster.drain.on
    assert cluster.dns_state == "published"
    assert cluster.shared_generation == 42


def test_cluster_disabled_and_old_members() -> None:
    """No cluster; members of old versions with nulls."""
    off = ClusterStatus.from_dict(
        {"enabled": False, "reason": "no address to advertise"}
    )
    assert not off.enabled
    assert off.reason == "no address to advertise"
    assert off.members == ()
    assert off.raft is None
    assert off.self_member is None

    old: dict[str, Any] = {
        "enabled": True,
        "members": [
            {
                "node_id": NODE01,
                "name": "mqttcore-005309",
                "state": "suspect",
                "host_name": None,
                "web_port": None,
                "can_order": None,
                "generation": None,
                "certificate": None,
            }
        ],
        "leader": None,
    }
    cluster = ClusterStatus.from_dict(old)
    member = cluster.members[0]
    assert member.live
    assert not member.alive
    assert member.url is None
    assert member.label == "mqttcore-005309"
    assert member.can_order is None
    assert cluster.leader is None
    assert ClusterStatus.from_dict("nonsense").enabled is False


def test_stats_on_the_host() -> None:
    """The host's SIZE_MAX free memory means nothing: None."""
    stats = Stats.from_dict(load("stats_mqtt04.json"))
    assert stats.memory_hot_free is None
    assert stats.memory_bulk_free is None
    assert stats.retain_mode == "raft"


def test_device_models() -> None:
    """A board, and the host's nulls."""
    board = Device.from_dict(load("device_mqtt01.json"))
    assert board.is_board
    assert board.mac == "00:00:5e:00:53:09"
    assert board.chip is not None
    assert board.chip.revision == "v3.2"
    assert board.last_reset == "software"
    assert board.coredump_stored is False
    host = Device.from_dict(load("device_mqtt04.json"))
    assert not host.is_board
    assert host.chip is None
    assert host.last_reset is None
    assert host.coredump_stored is None


def test_tls_models() -> None:
    """The board reads the certificate; the host does not."""
    board = TlsStatus.from_dict(load("tls_status_mqtt01.json"))
    assert board.has_details
    assert board.not_after_epoch == 1801296719
    assert board.days_left == 71
    assert board.expired is False
    assert "mqtt05.example.com" in board.dns_names
    host = TlsStatus.from_dict(load("tls_status_host.json"))
    assert host.certificate
    assert not host.has_details
    no_clock = TlsStatus.from_dict(load("tls_status_mqtt01.json") | {"days_left": -1})
    assert no_clock.days_left is None


def test_acme_model() -> None:
    """Never is None; unknown days are None."""
    acme = AcmeStatus.from_dict(load("acme_mqtt01.json"))
    assert acme.state == "ok"
    assert acme.last_success == 1793520720
    assert acme.last_failure is None
    assert acme.days_until_expiry == 71
    assert acme.name_missing is None
    unknown = AcmeStatus.from_dict({"days_until_expiry": -1, "state": "idle"})
    assert unknown.days_until_expiry is None
    assert unknown.last_success is None


async def test_at_an_address(
    session: ClientSession, aioclient_mock: AiohttpClientMocker
) -> None:
    """An address client connects there, and still names the node it means."""
    aioclient_mock.get(
        "https://192.0.2.15:8443/api/stats", json=load("stats_mqtt05.json")
    )
    client = MqttcoreClient(
        session, "https://mqtt05.example.com:8443/", token=TOKEN, address="192.0.2.15"
    )
    assert client.url == "https://mqtt05.example.com:8443/"
    assert client.address == "192.0.2.15"
    await client.get_stats()
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert str(url) == "https://192.0.2.15:8443/api/stats"
    assert headers["Host"] == "mqtt05.example.com:8443"


def test_at_an_address_names_the_server_for_tls(session: ClientSession) -> None:
    """TLS gets the name (SNI and the certificate's check); the default port is left out."""
    client = MqttcoreClient(session, f"{BASE}/", address="192.0.2.11")
    headers: dict[str, str] = {}
    target, extra = client._at_address(client._target("/api/device", None), headers)
    assert str(target) == "https://192.0.2.11/api/device"
    assert headers == {"Host": "mqtt01.example.com"}
    assert extra == {"server_hostname": "mqtt01.example.com"}
    relayed, _ = client._at_address(client._target("/api/stats", NODE04), {})
    assert relayed.host == "192.0.2.11"
    assert relayed.query["node"] == NODE04


def test_without_an_address_nothing_changes(client: MqttcoreClient) -> None:
    """A client without an address leaves the request as it was."""
    headers: dict[str, str] = {}
    target, extra = client._at_address(client._target("/api/device", None), headers)
    assert str(target) == f"{BASE}/api/device"
    assert headers == {}
    assert extra == {}


async def test_an_address_that_fails_says_so(
    session: ClientSession, aioclient_mock: AiohttpClientMocker
) -> None:
    """A connection error names the node and the address tried."""
    aioclient_mock.get(
        "https://192.0.2.11/api/stats", exc=ClientConnectionError("refused")
    )
    client = MqttcoreClient(session, f"{BASE}/", address="192.0.2.11")
    with pytest.raises(
        MqttcoreConnectionError, match=r"mqtt01\.example\.com \(192\.0\.2\.11\)"
    ):
        await client.get_stats()

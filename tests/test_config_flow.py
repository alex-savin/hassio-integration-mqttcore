"""Tests for the mqttcore config flow."""

from __future__ import annotations

from ipaddress import ip_address
import json
from typing import Any
from unittest.mock import Mock

from aiohttp import ClientConnectionError, ClientConnectorCertificateError
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.mqttcore.config_flow import parse_networks
from custom_components.mqttcore.const import (
    CONF_KNOWN_URLS,
    CONF_OTA_PASSWORD,
    CONF_RESTRICT_NETWORK,
    CONF_TOKEN_NAME,
    DOMAIN,
    SECTION_CONNECTION,
    TOKEN_SCOPES,
)
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .common import NEW_TOKEN, OTA_PASSWORD, TOKEN_NAME, URL01, load, prepend, view

pytestmark = pytest.mark.usefixtures(
    "mock_setup_entry", "mock_instance_id", "no_token_delay"
)

USER_INPUT = {
    CONF_HOST: "mqtt01.example.com",
    CONF_PORT: 443,
    SECTION_CONNECTION: {CONF_SSL: True, CONF_VERIFY_SSL: True},
}


def mock_node(
    aioclient_mock: AiohttpClientMocker,
    url: str = URL01,
    *,
    tokens: list[dict[str, Any]] | None = None,
    agents_post: Any = None,
    config: dict[str, Any] | None = None,
    cluster: Any = None,
    auth_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One node that answers the flow's calls; returns the bodies POSTed to agents."""
    posted: list[dict[str, Any]] = []

    async def agents(
        method: str, request_url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        body = json.loads(data)
        posted.append(body)
        if agents_post is not None:
            return await agents_post(method, request_url, body)
        answer = {"status": "ok"}
        if body["action"] == "create":
            answer["token"] = NEW_TOKEN
        return AiohttpClientMockResponse(method, request_url, json=answer)

    aioclient_mock.get(
        f"{url}/api/auth/status", json=auth_status or load("auth_status.json")
    )
    aioclient_mock.get(f"{url}/api/config", json=config or load("config.json"))
    if cluster is None:
        aioclient_mock.get(f"{url}/api/cluster", json=view("mqtt01"))
    else:
        aioclient_mock.get(f"{url}/api/cluster", side_effect=cluster)
    aioclient_mock.get(f"{url}/api/device", json=load("device_mqtt01.json"))
    agents_list = load("agents.json")
    if tokens is not None:
        agents_list["tokens"].extend(tokens)
    aioclient_mock.get(f"{url}/api/agents", json=agents_list)
    aioclient_mock.post(f"{url}/api/agents", side_effect=agents)
    return posted


def headers_of(
    aioclient_mock: AiohttpClientMocker, method: str, path: str
) -> list[dict[str, str]]:
    """The headers sent with each matching request."""
    return [
        dict(headers or {})
        for m, url, _, headers in aioclient_mock.mock_calls
        if m.upper() == method and URL(str(url)).path == path
    ]


async def _start_user(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


async def test_user_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The happy path: address, OTA password once, a read-only token."""
    posted = mock_node(aioclient_mock)

    result = await _start_user(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: "192.0.2.80"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "mqtt.example.com"
    assert result["result"].unique_id == "mqtt.example.com"
    assert result["data"] == {
        CONF_HOST: "mqtt01.example.com",
        CONF_PORT: 443,
        CONF_SSL: True,
        CONF_VERIFY_SSL: True,
        CONF_TOKEN: NEW_TOKEN,
        CONF_TOKEN_NAME: TOKEN_NAME,
        CONF_KNOWN_URLS: [
            "https://mqtt01.example.com/",
            "https://mqtt04.example.com/",
            "https://mqtt05.example.com:8443/",
        ],
    }
    # The OTA password is used, never stored.
    assert OTA_PASSWORD not in json.dumps(result["data"])
    assert posted == [
        {
            "action": "create",
            "name": TOKEN_NAME,
            "scopes": TOKEN_SCOPES,
            "networks": ["192.0.2.80/32"],
        }
    ]
    post_headers = headers_of(aioclient_mock, "POST", "/api/agents")
    assert post_headers[0]["X-OTA-Password"] == OTA_PASSWORD
    assert "Authorization" not in post_headers[0]
    # The new token is tried once, with no password.
    token_calls = [
        h
        for h in headers_of(aioclient_mock, "GET", "/api/cluster")
        if "Authorization" in h
    ]
    assert token_calls == [
        {"Accept": "application/json", "Authorization": f"Bearer {NEW_TOKEN}"}
    ]


async def test_user_flow_replaces_own_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token of this Home Assistant from an earlier setup is revoked first."""
    posted = mock_node(
        aioclient_mock,
        tokens=[{"name": TOKEN_NAME, "scopes": {"status": "read"}, "networks": []}],
    )
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [p["action"] for p in posted] == ["revoke", "create"]
    assert posted[0]["name"] == TOKEN_NAME
    assert "networks" not in posted[1]


async def test_credentials_suggests_local_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The network limit is filled in with the address used to reach the node."""
    mock_node(aioclient_mock)
    result = await _start_user(hass)
    schema = result["data_schema"].schema
    restrict = next(k for k in schema if k == CONF_RESTRICT_NETWORK)
    # The tests' network helper answers 10.10.10.10 for every target.
    assert restrict.description == {"suggested_value": "10.10.10.10"}


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"exc": ClientConnectionError()}, "cannot_connect"),
        (
            {"exc": ClientConnectorCertificateError(Mock(), Exception("bad cert"))},
            "invalid_certificate",
        ),
        ({"status": 421, "json": {"error": "unknown host"}}, "host_not_allowed"),
        ({"status": 429, "json": {"error": "slow down"}}, "too_many_attempts"),
        ({"status": 200, "text": "<html>router</html>"}, "not_mqttcore"),
        ({"status": 404, "text": "Not Found"}, "not_mqttcore"),
        ({"status": 500, "json": {"error": "storage"}}, "cannot_connect"),
        ({"json": {"setup_needed": True}}, "setup_needed"),
    ],
)
async def test_user_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: str,
) -> None:
    """The address step says why a node does not do."""
    aioclient_mock.get(f"{URL01}/api/auth/status", **kwargs)
    result = await _start_user(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    # Fixed, it goes on.
    aioclient_mock.clear_requests()
    mock_node(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "credentials"


@pytest.mark.parametrize(
    ("status", "ssl", "error"),
    [
        (401, True, "invalid_auth"),
        (403, True, "invalid_auth"),
        (403, False, "https_required"),
        (429, True, "too_many_attempts"),
        (503, True, "cannot_connect"),
    ],
)
async def test_credentials_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    ssl: bool,
    error: str,
) -> None:
    """A refused OTA password, plain HTTP, throttling."""
    url = URL01 if ssl else "http://mqtt01.example.com"
    aioclient_mock.get(f"{url}/api/auth/status", json=load("auth_status.json"))
    aioclient_mock.get(f"{url}/api/config", status=status, json={"error": "no"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "mqtt01.example.com",
            CONF_PORT: 443 if ssl else 80,
            SECTION_CONNECTION: {CONF_SSL: ssl, CONF_VERIFY_SSL: True},
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: "wrong password"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": error}


async def test_wrong_password_found_when_minting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With the login off, reads pass; the token call checks the password."""

    async def refuse(
        method: str, url: URL, body: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method,
            url,
            status=403,
            json={"error": "this needs the OTA password (X-OTA-Password)"},
        )

    mock_node(aioclient_mock, agents_post=refuse)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: "wrong password"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_token_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The node's own reason shows when it refuses to make the token."""

    async def full(
        method: str, url: URL, body: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method, url, status=400, json={"error": "16 tokens already"}
        )

    mock_node(aioclient_mock, agents_post=full)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["errors"] == {"base": "token_refused"}
    assert result["description_placeholders"]["error"] == "16 tokens already"


async def test_token_taken_after_a_while(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token minted through a follower is refused until the settings reach it."""
    calls = 0

    async def cluster(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        nonlocal calls
        calls += 1
        if calls == 2:  # the first try of the new token
            return AiohttpClientMockResponse(
                method, url, status=401, json={"error": "unknown or expired API token"}
            )
        return AiohttpClientMockResponse(method, url, json=view("mqtt01"))

    mock_node(aioclient_mock, cluster=cluster)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert calls == 3


async def test_token_never_taken(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token the node never accepts fails the step."""
    calls = 0

    async def cluster(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return AiohttpClientMockResponse(method, url, json=view("mqtt01"))
        return AiohttpClientMockResponse(
            method, url, status=401, json={"error": "unknown"}
        )

    mock_node(aioclient_mock, cluster=cluster)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["errors"] == {"base": "token_not_accepted"}


async def test_token_scope_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token the node refuses outright (403) is not retried."""
    calls = 0

    async def cluster(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return AiohttpClientMockResponse(method, url, json=view("mqtt01"))
        return AiohttpClientMockResponse(
            method, url, status=403, json={"error": "network"}
        )

    mock_node(aioclient_mock, cluster=cluster)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["errors"] == {"base": "token_not_accepted"}
    assert calls == 2


async def test_unexpected_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Anything unexpected is a form error, not a crash."""

    async def broken(
        method: str, url: URL, body: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        raise RuntimeError("boom")

    mock_node(aioclient_mock, agents_post=broken)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["errors"] == {"base": "unknown"}


async def test_invalid_network(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The network limit must be addresses or ranges."""
    mock_node(aioclient_mock)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_OTA_PASSWORD: OTA_PASSWORD,
            CONF_RESTRICT_NETWORK: "home-assistant.local",
        },
    )
    assert result["errors"] == {CONF_RESTRICT_NETWORK: "invalid_network"}


def test_parse_networks() -> None:
    """Addresses become /32 or /128; ranges are normalized; at most four."""
    assert parse_networks("") == []
    assert parse_networks("192.0.2.10, 192.0.2.0/24 2001:db8::1") == [
        "192.0.2.10/32",
        "192.0.2.0/24",
        "2001:db8::1/128",
    ]
    assert parse_networks("192.0.2.77/24") == ["192.0.2.0/24"]
    with pytest.raises(ValueError, match="too many"):
        parse_networks("192.0.2.1 192.0.2.2 192.0.2.3 192.0.2.4 192.0.2.5")
    with pytest.raises(ValueError, match="does not appear"):
        parse_networks("nope")


async def test_already_configured(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The cluster is set up once, whichever member is entered; no token is minted."""
    mock_config_entry.add_to_hass(hass)
    posted = mock_node(aioclient_mock)
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert posted == []


async def test_lone_node_unique_id(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A node without a cluster or a server name is known by its MAC."""
    config = load("config.json") | {"server_name": ""}
    mock_node(aioclient_mock, config=config)
    prepend(aioclient_mock, f"{URL01}/api/cluster", json=load("cluster_disabled.json"))
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "node:00:00:5e:00:53:09"
    assert result["title"] == "mqtt01.example.com"
    assert result["data"][CONF_KNOWN_URLS] == []


ZEROCONF = ZeroconfServiceInfo(
    ip_address=ip_address("192.0.2.11"),
    ip_addresses=[ip_address("192.0.2.11")],
    hostname="mqttcore-005309.local.",
    name="mqttcore-005309._mqttcore._tcp.local.",
    port=443,
    type="_mqttcore._tcp.local.",
    properties={
        "id": "00005e005309",
        "ver": "0.29.1",
        "name": "mqtt.example.com",
        "api": "https",
        "path": "/api/",
    },
)


async def test_zeroconf(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A board's announcement fills in the cluster's name, which its certificate carries."""
    mock_node(aioclient_mock, "https://mqtt.example.com")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_HOST] == "mqtt.example.com"
    assert suggested[CONF_PORT] == 443

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {CONF_HOST: "mqtt.example.com"}
    )
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "mqtt.example.com"


async def test_zeroconf_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Every member announces the same cluster name: one entry."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_without_name(hass: HomeAssistant) -> None:
    """Without a server name the board's address is filled in."""
    info = ZeroconfServiceInfo(
        ip_address=ZEROCONF.ip_address,
        ip_addresses=ZEROCONF.ip_addresses,
        hostname=ZEROCONF.hostname,
        name=ZEROCONF.name,
        port=80,
        type=ZEROCONF.type,
        properties={
            "id": "00005e005309",
            "ver": "0.29.1",
            "api": "http",
            "path": "/api/",
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["step_id"] == "user"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_HOST] == "192.0.2.11"
    assert suggested[CONF_PORT] == 80
    connection = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key == SECTION_CONNECTION
    )
    assert {
        str(key): key.description["suggested_value"] for key in connection.schema.schema
    } == {CONF_SSL: False, CONF_VERIFY_SSL: True}


HASSIO = HassioServiceInfo(
    config={
        "host": "mqtt05.example.com",
        "port": 8443,
        "ssl": True,
        "server_name": "mqtt.example.com",
        "addon": "mqttcore",
    },
    name="mqttcore",
    slug="local_mqttcore",
    uuid="1234",
)


async def test_hassio(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The app's Supervisor discovery (a phase 1 stub) leads to the password step."""
    mock_node(aioclient_mock, "https://mqtt05.example.com:8443")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {"addon": "mqttcore"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "mqtt05.example.com"
    assert result["data"][CONF_PORT] == 8443


async def test_hassio_unreachable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The confirmation says when the app's node does not answer."""
    aioclient_mock.get(
        "https://mqtt05.example.com:8443/api/auth/status", exc=ClientConnectionError()
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "hassio_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "config", [{"port": 8443}, {"host": "mqtt05.example.com", "port": "nope"}]
)
async def test_hassio_invalid(hass: HomeAssistant, config: dict[str, Any]) -> None:
    """An announcement without an address is refused."""
    info = HassioServiceInfo(
        config=config, name="mqttcore", slug="local_mqttcore", uuid="1"
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The app's cluster is already set up."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A new token replaces the old one, under the same name."""
    mock_config_entry.add_to_hass(hass)
    posted = mock_node(
        aioclient_mock,
        tokens=[{"name": TOKEN_NAME, "scopes": {"status": "read"}, "networks": []}],
    )
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKEN] == NEW_TOKEN
    assert mock_config_entry.data[CONF_TOKEN_NAME] == TOKEN_NAME
    assert [p["action"] for p in posted] == ["revoke", "create"]


async def test_reauth_through_another_member(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """When the configured node is down, any member it knew will do."""
    mock_config_entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL01}/api/auth/status", exc=ClientConnectionError())
    mock_node(aioclient_mock, "https://mqtt04.example.com")
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_HOST] == "mqtt01.example.com"


async def test_reauth_wrong_cluster(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A node of another cluster cannot renew this entry's token."""
    mock_config_entry.add_to_hass(hass)
    posted = mock_node(
        aioclient_mock, config=load("config.json") | {"server_name": "mqtt.example.net"}
    )
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_cluster"
    assert posted == []

"""Shared test data: a three-node cluster as its admin API answers."""

from __future__ import annotations

from collections.abc import Iterable
import copy
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientConnectionError
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.mqttcore.const import CONF_KNOWN_URLS, CONF_TOKEN_NAME, DOMAIN
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry

FIXTURES = Path(__file__).parent / "fixtures"

URL01 = "https://mqtt01.example.com"
URL04 = "https://mqtt04.example.com"
URL05 = "https://mqtt05.example.com:8443"
NODE01 = "5c9309a1b2c3d4e5f60718293a5c9309"
NODE04 = "bfbfb5c4d3e2f1a0b9c8d7e6f5bfbfb5"
NODE05 = "e05e05f1e2d3c4b5a6978877e0e05e05"
NODES = {
    NODE01: ("mqtt01", URL01),
    NODE04: ("mqtt04", URL04),
    NODE05: ("mqtt05", URL05),
}

TOKEN = "mqc_" + "T" * 43
NEW_TOKEN = "mqc_" + "N" * 43
TOKEN_NAME = "home-assistant-0123abcd"
OTA_PASSWORD = "correct horse battery"
UNIQUE_ID = "mqtt.example.com"

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: "mqtt01.example.com",
    CONF_PORT: 443,
    CONF_SSL: True,
    CONF_VERIFY_SSL: True,
    CONF_TOKEN: TOKEN,
    CONF_TOKEN_NAME: TOKEN_NAME,
    CONF_KNOWN_URLS: [f"{URL01}/", f"{URL04}/", f"{URL05}/"],
}


class Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def refresh_all(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Refresh the cluster, then every node."""
    runtime = entry.runtime_data
    await runtime.cluster.async_refresh()
    await hass.async_block_till_done()
    for node in list(runtime.nodes.nodes.values()):
        await node.async_refresh()
    await hass.async_block_till_done()


def load(name: str) -> Any:
    """A fixture file's JSON."""
    return json.loads((FIXTURES / name).read_text())


def view(short: str, *, drop: Iterable[str] = ()) -> dict[str, Any]:
    """A node's view of the cluster, without the members in `drop`."""
    data = copy.deepcopy(load(f"cluster_{short}.json"))
    gone = set(drop)
    data["members"] = [m for m in data["members"] if m["node_id"] not in gone]
    data["links"] = [link for link in data["links"] if link["node_id"] not in gone]
    return data


def node_routes(short: str) -> dict[str, tuple[int, Any]]:
    """What one node answers, by path."""
    board = short == "mqtt01"
    return {
        "/api/auth/status": (200, load("auth_status.json")),
        "/api/cluster": (200, view(short)),
        "/api/stats": (200, load(f"stats_{short}.json")),
        "/api/device": (200, load(f"device_{short}.json")),
        "/api/tls/status": (
            200,
            load("tls_status_mqtt01.json" if board else "tls_status_host.json"),
        ),
        "/api/acme": (
            (200, load("acme_mqtt01.json"))
            if board
            else (
                501,
                {
                    "error": "certificates from Let's Encrypt are issued on the board only"
                },
            )
        ),
    }


def mock_cluster(
    aioclient_mock: AiohttpClientMocker,
    *,
    down: Iterable[str] = (),
    overrides: dict[tuple[str, str], tuple[int, Any]] | None = None,
) -> None:
    """Register the three nodes, directly and through each other's relay.

    `down` lists base URLs that do not answer. `overrides` replaces single
    answers, keyed by (base URL, path).
    """
    down = set(down)
    overrides = overrides or {}
    for node_id, (short, url) in NODES.items():
        routes = node_routes(short)
        for path, answer in routes.items():
            status, body = overrides.get((url, path), answer)
            if url in down:
                aioclient_mock.get(f"{url}{path}", exc=ClientConnectionError("down"))
            else:
                aioclient_mock.get(f"{url}{path}", status=status, json=body)
            # The same answer through every other node's relay.
            for other_id, (_, other_url) in NODES.items():
                if other_id == node_id:
                    continue
                relay = f"{other_url}/api/peer?node={node_id}&path={path}"
                if other_url in down:
                    aioclient_mock.get(relay, exc=ClientConnectionError("down"))
                elif url in down:
                    aioclient_mock.get(relay, status=502, json={"error": "not linked"})
                else:
                    aioclient_mock.get(relay, status=status, json=body)


def get_device(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: str
) -> DeviceEntry | None:
    """The entry's device with this identifier."""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )


def prepend(aioclient_mock: AiohttpClientMocker, url: str, **kwargs: Any) -> None:
    """Answer a GET before every answer registered so far (the first match wins)."""
    aioclient_mock._mocks.insert(
        0, AiohttpClientMockResponse("get", URL(url), **kwargs)
    )


def entry_unique_id(node_id: str, key: str) -> str:
    """A node entity's unique ID."""
    return f"{node_id}_{key}"


def cluster_unique_id(key: str) -> str:
    """A cluster entity's unique ID."""
    return f"cluster-{UNIQUE_ID}_{key}"


__all__ = ["DOMAIN"]

"""Tests for the actions on users, the ACL and clients (phase 4)."""

from __future__ import annotations

import json

import probatio
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .common import URL01, URL04, URL05

DOMAIN = "mqttcore"


def posted(
    aioclient_mock: AiohttpClientMocker, path: str
) -> list[tuple[str, object, dict]]:
    """(URL, body, headers) of each POST to path."""
    return [
        (str(url), data, dict(headers or {}))
        for method, url, data, headers in aioclient_mock.mock_calls
        if method.upper() == "POST" and str(url).endswith(path)
    ]


async def test_users(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Add, set a password, remove: one POST each, with the token."""
    aioclient_mock.post(
        f"{URL01}/api/access/users", json={"status": "ok", "message": "done"}
    )
    entry_id = init_integration.entry_id
    await hass.services.async_call(
        DOMAIN,
        "add_user",
        {"config_entry_id": entry_id, "name": "zigbee04", "password": "long-enough-1"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "set_password",
        {"config_entry_id": entry_id, "name": "zigbee04", "password": "long-enough-2"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "remove_user",
        {"config_entry_id": entry_id, "name": "zigbee04"},
        blocking=True,
    )
    bodies = [
        json.loads(str(b)) for _, b, _ in posted(aioclient_mock, "/api/access/users")
    ]
    assert bodies == [
        {"action": "add", "name": "zigbee04", "password": "long-enough-1"},
        {"action": "set_password", "name": "zigbee04", "password": "long-enough-2"},
        {"action": "remove", "name": "zigbee04"},
    ]
    headers = posted(aioclient_mock, "/api/access/users")[0][2]
    assert headers["Authorization"].startswith("Bearer mqc_")


async def test_short_password_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The action checks a password's length before anything is sent."""
    with pytest.raises(probatio.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "add_user",
            {
                "config_entry_id": init_integration.entry_id,
                "name": "x",
                "password": "short",
            },
            blocking=True,
        )


async def test_acl(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Read the ACL; replace it with text; the cluster's refusal comes back in its words."""
    aioclient_mock.get(
        f"{URL01}/api/access/acl",
        json={
            "acl": True,
            "mode": "report",
            "mode_line": 1,
            "text": "mode report\nallow all subscribe a\n",
            "rules": [{"line": 2}],
        },
    )
    answer = await hass.services.async_call(
        DOMAIN,
        "get_acl",
        {"config_entry_id": init_integration.entry_id},
        blocking=True,
        return_response=True,
    )
    assert answer == {
        "acl": True,
        "mode": "report",
        "rules": 1,
        "text": "mode report\nallow all subscribe a\n",
    }
    aioclient_mock.post(
        f"{URL01}/api/access/acl",
        json={"status": "ok", "rules": 2, "subscriptions_revoked": 1, "saved": True},
    )
    answer = await hass.services.async_call(
        DOMAIN,
        "set_acl",
        {
            "config_entry_id": init_integration.entry_id,
            "acl": "allow all subscribe a\n",
        },
        blocking=True,
        return_response=True,
    )
    assert answer == {"rules": 2, "subscriptions_revoked": 1, "saved": True}
    _, body, headers = posted(aioclient_mock, "/api/access/acl")[0]
    assert body == "allow all subscribe a\n"
    assert headers["Content-Type"].startswith("text/plain")


async def test_acl_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A text the cluster refuses is a validation error with its reason."""
    aioclient_mock.post(
        f"{URL01}/api/access/acl",
        status=400,
        json={"error": 'line 1: "x" is not allow or deny'},
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_acl",
            {"config_entry_id": init_integration.entry_id, "acl": "x\n"},
            blocking=True,
        )
    assert err.value.translation_placeholders == {
        "error": 'line 1: "x" is not allow or deny'
    }


async def test_clients(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Every node's clients, each with its node; kicking finds the node that has it."""
    aioclient_mock.get(
        f"{URL01}/api/clients", json=[{"client_id": "ha", "identity": "homeassistant"}]
    )
    aioclient_mock.get(
        f"{URL04}/api/clients", json=[{"client_id": "z2m", "identity": "zigbee01"}]
    )
    aioclient_mock.get(f"{URL05}/api/clients", json=[])
    answer = await hass.services.async_call(
        DOMAIN,
        "list_clients",
        {"config_entry_id": init_integration.entry_id},
        blocking=True,
        return_response=True,
    )
    assert answer == {
        "clients": [
            {
                "client_id": "ha",
                "identity": "homeassistant",
                "node": "mqtt01.example.com",
            },
            {"client_id": "z2m", "identity": "zigbee01", "node": "mqtt04.example.com"},
        ],
        "unanswered": [],
    }
    for url in (URL01, URL05):
        aioclient_mock.post(
            f"{url}/api/clients/kick", status=404, json={"error": "no such client"}
        )
    aioclient_mock.post(f"{URL04}/api/clients/kick", json={"status": "ok"})
    await hass.services.async_call(
        DOMAIN,
        "kick_client",
        {"config_entry_id": init_integration.entry_id, "client_id": "z2m"},
        blocking=True,
    )
    assert [u for u, _, _ in posted(aioclient_mock, "/api/clients/kick")][
        -1
    ] == f"{URL04}/api/clients/kick"
    aioclient_mock._mocks = [
        m for m in aioclient_mock._mocks if not str(m.url).endswith("/api/clients/kick")
    ]
    for url in (URL01, URL04, URL05):
        aioclient_mock.post(
            f"{url}/api/clients/kick", status=404, json={"error": "no such client"}
        )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "kick_client",
            {"config_entry_id": init_integration.entry_id, "client_id": "nobody"},
            blocking=True,
        )
    assert err.value.translation_key == "client_not_connected"


async def test_token_without_rights(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 403 says the token lacks the right, and how to get it."""
    aioclient_mock.post(
        f"{URL01}/api/access/users",
        status=403,
        json={"error": "this API token's scopes do not reach this route"},
    )
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "remove_user",
            {"config_entry_id": init_integration.entry_id, "name": "x"},
            blocking=True,
        )
    assert err.value.translation_key == "token_cannot_change"


async def test_unknown_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An entry that is not a loaded cluster is a validation error."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "get_acl",
            {"config_entry_id": "nope"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "entry_not_found"

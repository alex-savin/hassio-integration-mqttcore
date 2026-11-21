"""Actions on a cluster: its users, its ACL and its clients (phase 4).

Each action names the cluster's config entry. Users and the ACL are the
cluster's: any node takes the change and a follower passes it to the
leader. Clients are each node's: listing asks every node, disconnecting
asks each node until one has the client. The token needs `access` and
`clients` write; the setup flow mints it with them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.json import JsonValueType

from .const import DOMAIN
from .pymqttcore import (
    MqttcoreBadRequestError,
    MqttcoreClient,
    MqttcoreConflictError,
    MqttcoreError,
    MqttcoreForbiddenError,
    MqttcoreNotFoundError,
)

ATTR_CONFIG_ENTRY = "config_entry_id"
ATTR_NAME = "name"
ATTR_PASSWORD = "password"  # noqa: S105 (a field name, not a password)
ATTR_ACL = "acl"
ATTR_CLIENT_ID = "client_id"

SERVICE_ADD_USER = "add_user"
SERVICE_SET_PASSWORD = "set_password"  # noqa: S105 (an action name)
SERVICE_REMOVE_USER = "remove_user"
SERVICE_GET_ACL = "get_acl"
SERVICE_SET_ACL = "set_acl"
SERVICE_LIST_CLIENTS = "list_clients"
SERVICE_KICK_CLIENT = "kick_client"

_ENTRY = {probatio.Required(ATTR_CONFIG_ENTRY): cv.string}
_NAME = probatio.All(cv.string, probatio.Length(min=1, max=64))
_PASSWORD = probatio.All(cv.string, probatio.Length(min=8, max=128))

SCHEMA_ENTRY = probatio.Schema(_ENTRY)
SCHEMA_USER = probatio.Schema({**_ENTRY, probatio.Required(ATTR_NAME): _NAME})
SCHEMA_USER_PASSWORD = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_NAME): _NAME,
        probatio.Required(ATTR_PASSWORD): _PASSWORD,
    }
)
SCHEMA_ACL = probatio.Schema({**_ENTRY, probatio.Required(ATTR_ACL): cv.string})
SCHEMA_CLIENT = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_CLIENT_ID): probatio.All(
            cv.string, probatio.Length(min=1)
        ),
    }
)


def _entry(hass: HomeAssistant, call: ServiceCall) -> Any:
    entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY])
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_found"
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


def _client(entry: Any) -> MqttcoreClient:
    client: MqttcoreClient | None = entry.runtime_data.cluster.active_client
    if client is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cluster_unreachable",
            translation_placeholders={"error": "no node answers"},
        )
    return client


def _refused(err: MqttcoreError) -> HomeAssistantError:
    """What a failed call becomes: the cluster's own words where it gave some."""
    if isinstance(err, MqttcoreForbiddenError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="token_cannot_change",
            translation_placeholders={"error": err.message},
        )
    if isinstance(
        err, (MqttcoreBadRequestError, MqttcoreConflictError, MqttcoreNotFoundError)
    ):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders={"error": err.message},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={"error": str(err)},
    )


async def _call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except MqttcoreError as err:
        raise _refused(err) from err


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions (once, for every entry)."""

    def _user(action: str) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
        async def handle(call: ServiceCall) -> None:
            client = _client(_entry(hass, call))
            await _call(
                client.change_user(
                    action, call.data[ATTR_NAME], call.data.get(ATTR_PASSWORD)
                )
            )

        return handle

    async def get_acl(call: ServiceCall) -> ServiceResponse:
        acl = await _call(_client(_entry(hass, call)).get_acl())
        rules = acl.get("rules")
        return {
            "acl": bool(acl.get("acl")),
            "mode": acl.get("mode"),
            "rules": len(rules) if isinstance(rules, list) else 0,
            "text": acl.get("text") or "",
        }

    async def set_acl(call: ServiceCall) -> ServiceResponse:
        answer = await _call(_client(_entry(hass, call)).set_acl(call.data[ATTR_ACL]))
        return {
            "rules": answer.get("rules", 0),
            "subscriptions_revoked": answer.get("subscriptions_revoked", 0),
            "saved": bool(answer.get("saved", False)),
        }

    async def list_clients(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        clients: list[JsonValueType] = []
        unanswered: list[JsonValueType] = []
        for node in entry.runtime_data.nodes.nodes.values():
            member = node.member
            name = (
                (member.host_name or member.name)
                if member is not None
                else node.node_id
            )
            try:
                found = await node.async_call(lambda c, n: c.get_clients(node_id=n))
            except MqttcoreError:
                unanswered.append(name)
                continue
            clients.extend({**c, "node": name} for c in found)
        return {"clients": clients, "unanswered": unanswered}

    async def kick_client(call: ServiceCall) -> None:
        entry = _entry(hass, call)
        client_id = call.data[ATTR_CLIENT_ID]
        for node in entry.runtime_data.nodes.nodes.values():
            try:
                await node.async_call(lambda c, n: c.kick_client(client_id, node_id=n))
            except MqttcoreNotFoundError:
                continue  # not connected to this node
            except MqttcoreError as err:
                raise _refused(err) from err
            return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="client_not_connected",
            translation_placeholders={"client_id": client_id},
        )

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_USER, _user("add"), SCHEMA_USER_PASSWORD
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PASSWORD, _user("set_password"), SCHEMA_USER_PASSWORD
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_USER, _user("remove"), SCHEMA_USER
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ACL,
        get_acl,
        SCHEMA_ENTRY,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ACL,
        set_acl,
        SCHEMA_ACL,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_CLIENTS,
        list_clients,
        SCHEMA_ENTRY,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_KICK_CLIENT, kick_client, SCHEMA_CLIENT
    )

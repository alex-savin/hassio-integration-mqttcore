"""Diagnostics for mqttcore, with every token, address and name redacted."""

from __future__ import annotations

import ipaddress
from typing import Any

from yarl import URL

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import MqttcoreConfigEntry
from .const import CONF_KNOWN_URLS, CONF_TOKEN_NAME
from .coordinator import debug_snapshot

# Tokens and passwords; addresses, DNS names and URLs; client IDs, user
# names and ACL text (none of which phase 2 reads, kept here so a later
# phase cannot leak them by accident); and the MAC.
TO_REDACT = {
    CONF_TOKEN,
    CONF_TOKEN_NAME,
    CONF_HOST,
    CONF_KNOWN_URLS,
    "password",
    "ota_password",
    "setup_token",
    "secret",
    "tsig_secret",
    "cloudflare_token",
    # addresses and names
    "address",
    "advertise",
    "seeds",
    "peer_listener",
    "gossip_socket",
    "server",
    "reference",
    "host",
    "host_name",
    "server_name",
    "url",
    "endpoint",
    "title",
    "unique_id",
    "dns_names",
    "subject",
    "domain",
    "acme_domain",
    "zone",
    "records",
    "names",
    "names_left_out",
    "name_missing",
    "seed_srv",
    "targets",
    "why",
    "account_url",
    "acme_email",
    "networks",
    "trusted_networks",
    "mac",
    # clients and access
    "client_id",
    "clientid",
    "username",
    "user",
    "users",
    "acl",
    "rules",
    "topic",
}


def _names(entry: MqttcoreConfigEntry) -> set[str]:
    """Every DNS name and address the entry knows, and the domains of the names."""
    found: set[str] = {str(entry.data.get(CONF_HOST) or ""), entry.title}
    found.add(entry.unique_id or "")
    found.update(URL(url).host or "" for url in entry.data.get(CONF_KNOWN_URLS, []))
    data = entry.runtime_data.cluster.data
    if data is not None:
        for member in data.cluster.members:
            found.update((member.host_name or "", member.address))
        if data.tls is not None:
            found.update(data.tls.dns_names)
    domains = set()
    for name in found:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if name.count(".") >= 2:
                domains.add(name.split(".", 1)[1])
    return {n for n in found | domains if len(n) >= 4}


def _scrub(value: Any, names: set[str]) -> Any:
    """Replace the known names inside strings that key redaction let through."""
    if isinstance(value, dict):
        return {k: _scrub(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, names) for v in value]
    if isinstance(value, str) and value != REDACTED:
        for name in sorted(names, key=len, reverse=True):
            value = value.replace(name, REDACTED)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MqttcoreConfigEntry
) -> dict[str, Any]:
    """Return what the entry knows, redacted."""
    runtime = entry.runtime_data
    cluster = runtime.cluster
    data = cluster.data
    redacted = async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
            },
            "cluster": {
                "available": cluster.last_update_success,
                "endpoint": cluster.active_url,
                "cluster": data.cluster.raw if data is not None else None,
                "tls": data.tls.raw if data is not None and data.tls else None,
                "acme": data.acme.raw if data is not None and data.acme else None,
            },
            "nodes": {
                f"node_{index}": debug_snapshot(node)
                for index, node in enumerate(runtime.nodes.nodes.values())
            },
        },
        TO_REDACT,
    )
    return _scrub(redacted, _names(entry))  # type: ignore[no-any-return]

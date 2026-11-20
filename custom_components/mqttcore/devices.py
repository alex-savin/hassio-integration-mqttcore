"""Device registry details for the cluster and its nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
)

from .const import (
    DOMAIN,
    LOCAL_NODE,
    MANUFACTURER,
    MODEL_BOARD,
    MODEL_CLUSTER,
    MODEL_HOST,
)
from .pymqttcore import ClusterMember, Device

if TYPE_CHECKING:
    from . import MqttcoreConfigEntry


def node_identifier(entry: MqttcoreConfigEntry, node_id: str) -> str:
    """A node's device identifier: its node ID, or the entry's for a lone node."""
    if node_id == LOCAL_NODE:
        return f"{entry.unique_id or entry.entry_id}-{LOCAL_NODE}"
    return node_id


def cluster_identifier(entry: MqttcoreConfigEntry) -> str:
    """The cluster device's identifier."""
    return f"cluster-{entry.unique_id or entry.entry_id}"


def node_is_board(device: Device | None, member: ClusterMember | None) -> bool:
    """The node is a board: its device says so, or it can order certificates.

    Only boards run ACME (`can_order`), so the member list tells a board
    apart even while the node itself has not answered yet.
    """
    if device is not None:
        return device.is_board
    return member is not None and member.can_order is True


def node_model(device: Device | None, member: ClusterMember | None) -> str:
    """The model to show for a node.

    `GET /api/device` says `esp32p4` or `posix`; it does not say whether a
    host is a Linux node or the Home Assistant app, so both read "Linux node".
    """
    return MODEL_BOARD if node_is_board(device, member) else MODEL_HOST


def node_name(
    entry: MqttcoreConfigEntry,
    node_id: str,
    member: ClusterMember | None,
    device: Device | None,
) -> str:
    """A node's device name: its DNS name, else its node name."""
    if member is not None:
        return member.label
    if device is not None and device.node:
        return device.node
    if node_id == LOCAL_NODE:
        return str(entry.data.get(CONF_HOST, entry.title))
    return node_id


def short_name(member: ClusterMember | None, fallback: str) -> str:
    """A short label for a member: the first label of its DNS name, or its name."""
    if member is None:
        return fallback
    if member.host_name:
        return member.host_name.split(".", 1)[0]
    return member.name or fallback


def node_device_details(device: Device) -> dict[str, Any]:
    """What `GET /api/device` adds to a node's device entry."""
    details: dict[str, Any] = {
        "model": node_model(device, None),
        "sw_version": device.version or None,
    }
    if device.chip is not None:
        details["hw_version"] = device.chip.revision
        details["model_id"] = device.chip.model or None
    return details


def node_device_info(
    entry: MqttcoreConfigEntry,
    node_id: str,
    member: ClusterMember | None,
    device: Device | None,
    via_device_id: str | None,
) -> DeviceInfo:
    """The device info of a node's entities."""
    info = DeviceInfo(
        identifiers={(DOMAIN, node_identifier(entry, node_id))},
        name=node_name(entry, node_id, member, device),
        manufacturer=MANUFACTURER,
        model=node_model(device, member),
        via_device_id=via_device_id,
    )
    if member is not None and member.url:
        info.configuration_url = member.url
    if device is not None:
        for key, value in node_device_details(device).items():
            setattr(info, key, value)
        if device.mac:
            info.connections = {(CONNECTION_NETWORK_MAC, device.mac)}
    elif member is not None and member.version:
        info.sw_version = member.version
    return info


def cluster_device_kwargs(
    entry: MqttcoreConfigEntry, url: str | None
) -> dict[str, Any]:
    """The cluster device, as `DeviceRegistry.async_get_or_create` takes it."""
    return {
        "identifiers": {(DOMAIN, cluster_identifier(entry))},
        "name": entry.title,
        "manufacturer": MANUFACTURER,
        "model": MODEL_CLUSTER,
        "entry_type": DeviceEntryType.SERVICE,
        "configuration_url": url,
    }

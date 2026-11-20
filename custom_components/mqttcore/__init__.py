"""The MQTT Core integration: monitor an MQTT Core broker cluster over its admin API."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import AnyDeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import MqttcoreClusterCoordinator, MqttcoreNodeManager
from .devices import cluster_device_kwargs, cluster_identifier, node_identifier

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class MqttcoreRuntimeData:
    """What a loaded entry holds."""

    cluster: MqttcoreClusterCoordinator
    nodes: MqttcoreNodeManager
    cluster_device_id: str


type MqttcoreConfigEntry = ConfigEntry[MqttcoreRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (config entries only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MqttcoreConfigEntry) -> None:
    """Set up one cluster from a config entry."""
    cluster = MqttcoreClusterCoordinator(hass, entry)
    await cluster.async_config_entry_first_refresh()

    # The cluster device first: every node's device points to it.
    device_registry = dr.async_get(hass)
    cluster_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **cluster_device_kwargs(entry, cluster.data.endpoint),
    )

    nodes = MqttcoreNodeManager(hass, entry, cluster)
    entry.runtime_data = MqttcoreRuntimeData(
        cluster=cluster, nodes=nodes, cluster_device_id=cluster_device.id
    )
    await nodes.async_setup()
    entry.async_on_unload(nodes.async_shutdown)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(hass: HomeAssistant, entry: MqttcoreConfigEntry) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: MqttcoreConfigEntry, device: AnyDeviceEntry
) -> bool:
    """Let people remove a node's device once the cluster no longer lists it."""
    current = {(DOMAIN, cluster_identifier(entry))}
    data = entry.runtime_data.cluster.data
    if data is not None:
        current.update((DOMAIN, node_identifier(entry, n)) for n in data.node_ids())
    return not device.identifiers.intersection(current)

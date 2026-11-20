"""Base entities: the cluster's, and a node's (fed by its own or the cluster's data)."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    MqttcoreClusterCoordinator,
    MqttcoreNodeCoordinator,
    MqttcoreNodeManager,
    NodeData,
)
from .devices import cluster_identifier, node_device_info, node_identifier
from .pymqttcore import ClusterMember

if TYPE_CHECKING:
    from . import MqttcoreConfigEntry


def node_unique_id(entry: MqttcoreConfigEntry, node_id: str, key: str) -> str:
    """The unique ID of a node's entity."""
    return f"{node_identifier(entry, node_id)}_{key}"


def _node_device(node: MqttcoreNodeCoordinator) -> DeviceInfo:
    entry = node.config_entry
    device = node.data.device if node.data is not None else None
    return node_device_info(
        entry, node.node_id, node.member, device, entry.runtime_data.cluster_device_id
    )


class MqttcoreClusterEntity(CoordinatorEntity[MqttcoreClusterCoordinator]):
    """An entity of the cluster device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MqttcoreClusterCoordinator, key: str) -> None:
        """Attach the entity to the cluster device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{cluster_identifier(entry)}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cluster_identifier(entry))}
        )


class MqttcoreClusterTotalEntity(MqttcoreClusterEntity):
    """A cluster entity summed over the nodes: it follows every node coordinator."""

    def __init__(
        self,
        coordinator: MqttcoreClusterCoordinator,
        nodes: MqttcoreNodeManager,
        key: str,
    ) -> None:
        """Follow the cluster and the node manager."""
        super().__init__(coordinator, key)
        self._nodes = nodes

    @property
    def node_data(self) -> list[NodeData]:
        """The latest data of every node that answered its last refresh."""
        return [
            node.data
            for node in self._nodes.nodes.values()
            if node.data is not None and node.last_update_success
        ]

    @override
    async def async_added_to_hass(self) -> None:
        """Also update when any node refreshes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._nodes.async_add_update_listener(self._handle_coordinator_update)
        )

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster answers and at least one node did."""
        return super().available and bool(self.node_data)


class MqttcoreMemberEntity(CoordinatorEntity[MqttcoreClusterCoordinator]):
    """An entity of a node's device, fed by the cluster's member list.

    It stays available while the node itself does not answer: the other
    members still say how it stands.
    """

    _attr_has_entity_name = True

    def __init__(self, node: MqttcoreNodeCoordinator, key: str) -> None:
        """Attach the entity to the node's device."""
        super().__init__(node.cluster)
        self.node_id = node.node_id
        self._attr_unique_id = node_unique_id(node.config_entry, node.node_id, key)
        self._attr_device_info = _node_device(node)

    @property
    def member(self) -> ClusterMember | None:
        """The node's record in the member list."""
        data = self.coordinator.data
        return None if data is None else data.member(self.node_id)

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster answers and lists the node."""
        return super().available and self.member is not None


class MqttcoreNodeEntity(CoordinatorEntity[MqttcoreNodeCoordinator]):
    """An entity of a node's device, fed by the node itself."""

    _attr_has_entity_name = True

    def __init__(self, node: MqttcoreNodeCoordinator, key: str) -> None:
        """Attach the entity to the node's device."""
        super().__init__(node)
        self._attr_unique_id = node_unique_id(node.config_entry, node.node_id, key)
        self._attr_device_info = _node_device(node)

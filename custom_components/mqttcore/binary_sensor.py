"""Binary sensors: the cluster's health, each member's state and its links."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MqttcoreConfigEntry
from .coordinator import (
    ClusterData,
    MqttcoreClusterCoordinator,
    MqttcoreNodeCoordinator,
    NodeData,
)
from .devices import short_name
from .entity import MqttcoreClusterEntity, MqttcoreMemberEntity, MqttcoreNodeEntity
from .pymqttcore import ClusterMember
from .sensor import NodeKind, node_kind

PARALLEL_UPDATES = 0

SETTINGS_PROBLEM_STATES = frozenset({"out-of-sync", "diverged"})


# ---- the cluster device ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MqttcoreClusterBinarySensorDescription(BinarySensorEntityDescription):
    """A cluster binary sensor read from the cluster coordinator."""

    is_on_fn: Callable[[ClusterData], bool | None]
    available_fn: Callable[[ClusterData], bool] = lambda _: True


def _raft_problem(data: ClusterData) -> bool | None:
    raft = data.cluster.raft
    if raft is None or not raft.on:
        return None
    return raft.leader is None or raft.stuck


def _settings_problem(data: ClusterData) -> bool:
    return any(
        m.live and m.config_state in SETTINGS_PROBLEM_STATES
        for m in data.cluster.members
    )


def _certificate_problem(data: ClusterData) -> bool:
    tls, acme = data.tls, data.acme
    expired = tls is not None and tls.expired is True
    renewal = acme is not None and (
        acme.state == "failed" or acme.name_missing is not None
    )
    return expired or renewal


CLUSTER_BINARY_SENSORS: tuple[MqttcoreClusterBinarySensorDescription, ...] = (
    MqttcoreClusterBinarySensorDescription(
        key="raft_problem",
        translation_key="raft_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_raft_problem,
        available_fn=lambda d: (
            d.clustered and d.cluster.raft is not None and d.cluster.raft.on
        ),
    ),
    MqttcoreClusterBinarySensorDescription(
        key="retained_set_problem",
        translation_key="retained_set_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d.retained_near_full(),
        available_fn=lambda d: d.retained_used() is not None,
    ),
    MqttcoreClusterBinarySensorDescription(
        key="settings_problem",
        translation_key="settings_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_settings_problem,
        available_fn=lambda d: d.clustered,
    ),
    MqttcoreClusterBinarySensorDescription(
        key="certificate_problem",
        translation_key="certificate_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_certificate_problem,
        available_fn=lambda d: (
            (d.tls is not None and d.tls.has_details) or d.acme is not None
        ),
    ),
    MqttcoreClusterBinarySensorDescription(
        key="balancing",
        translation_key="balancing",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.cluster.balance.enabled if d.cluster.balance else None,
        available_fn=lambda d: d.clustered and d.cluster.balance is not None,
    ),
)


# ---- a node's device ---------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MqttcoreMemberBinarySensorDescription(BinarySensorEntityDescription):
    """A node binary sensor read from the cluster's member list."""

    is_on_fn: Callable[[ClusterMember], bool]


MEMBER_BINARY_SENSORS: tuple[MqttcoreMemberBinarySensorDescription, ...] = (
    MqttcoreMemberBinarySensorDescription(
        key="alive",
        translation_key="alive",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda m: m.alive,
    ),
    MqttcoreMemberBinarySensorDescription(
        key="leader", translation_key="leader", is_on_fn=lambda m: m.is_leader
    ),
    MqttcoreMemberBinarySensorDescription(
        key="draining",
        translation_key="draining",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda m: m.draining,
    ),
)


@dataclass(frozen=True, kw_only=True)
class MqttcoreNodeBinarySensorDescription(BinarySensorEntityDescription):
    """A node binary sensor read from the node itself."""

    is_on_fn: Callable[[NodeData], bool | None]
    exists_fn: Callable[[NodeKind], bool] = lambda _: True


NODE_BINARY_SENSORS: tuple[MqttcoreNodeBinarySensorDescription, ...] = (
    MqttcoreNodeBinarySensorDescription(
        key="redirecting",
        translation_key="redirecting",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: (
            d.cluster.balance.redirecting
            if d.cluster is not None and d.cluster.balance is not None
            else None
        ),
        exists_fn=lambda kind: kind.clustered,
    ),
    MqttcoreNodeBinarySensorDescription(
        key="crash_dump",
        translation_key="crash_dump",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.device.coredump_stored if d.device is not None else None,
        exists_fn=lambda kind: kind.board,
    ),
)


def _node_entities(node: MqttcoreNodeCoordinator) -> list[BinarySensorEntity]:
    kind = node_kind(node)
    entities: list[BinarySensorEntity] = [
        MqttcoreNodeBinarySensor(node, description)
        for description in NODE_BINARY_SENSORS
        if description.exists_fn(kind)
    ]
    if node.clustered:
        entities.extend(
            MqttcoreMemberBinarySensor(node, d) for d in MEMBER_BINARY_SENSORS
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MqttcoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the cluster's binary sensors, each node's, and a link sensor per pair."""
    runtime = entry.runtime_data
    links: set[tuple[str, str]] = set()

    def _new_links() -> list[BinarySensorEntity]:
        nodes = [n for n in runtime.nodes.nodes.values() if n.clustered]
        found: list[BinarySensorEntity] = []
        for node in nodes:
            for peer in nodes:
                pair = (node.node_id, peer.node_id)
                if peer is node or pair in links:
                    continue
                links.add(pair)
                found.append(MqttcoreLinkBinarySensor(node, peer.node_id))
        return found

    entities: list[BinarySensorEntity] = [
        MqttcoreClusterBinarySensor(runtime.cluster, description)
        for description in CLUSTER_BINARY_SENSORS
    ]
    for node in runtime.nodes.nodes.values():
        entities.extend(_node_entities(node))
    entities.extend(_new_links())
    async_add_entities(entities)

    @callback
    def _async_add_nodes(nodes: list[MqttcoreNodeCoordinator]) -> None:
        added = [e for node in nodes for e in _node_entities(node)]
        async_add_entities([*added, *_new_links()])

    @callback
    def _async_node_removed(node_id: str) -> None:
        for pair in [p for p in links if node_id in p]:
            links.discard(pair)

    entry.async_on_unload(runtime.nodes.async_add_node_listener(_async_add_nodes))
    entry.async_on_unload(runtime.nodes.async_add_remove_listener(_async_node_removed))


class MqttcoreClusterBinarySensor(MqttcoreClusterEntity, BinarySensorEntity):
    """A binary sensor of the cluster device."""

    entity_description: MqttcoreClusterBinarySensorDescription

    def __init__(
        self,
        coordinator: MqttcoreClusterCoordinator,
        description: MqttcoreClusterBinarySensorDescription,
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster answers and has the value."""
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """The state."""
        return self.entity_description.is_on_fn(self.coordinator.data)


class MqttcoreMemberBinarySensor(MqttcoreMemberEntity, BinarySensorEntity):
    """A node binary sensor read from the member list."""

    entity_description: MqttcoreMemberBinarySensorDescription

    def __init__(
        self,
        node: MqttcoreNodeCoordinator,
        description: MqttcoreMemberBinarySensorDescription,
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        """The state."""
        member = self.member
        return None if member is None else self.entity_description.is_on_fn(member)


class MqttcoreNodeBinarySensor(MqttcoreNodeEntity, BinarySensorEntity):
    """A node binary sensor read from the node itself."""

    entity_description: MqttcoreNodeBinarySensorDescription

    def __init__(
        self,
        node: MqttcoreNodeCoordinator,
        description: MqttcoreNodeBinarySensorDescription,
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        """The state."""
        data = self.coordinator.data
        return None if data is None else self.entity_description.is_on_fn(data)


class MqttcoreLinkBinarySensor(MqttcoreNodeEntity, BinarySensorEntity):
    """Whether a node's peer link to another member is up, as the node says."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "link"

    def __init__(self, node: MqttcoreNodeCoordinator, peer_id: str) -> None:
        """Watch the link from `node` to `peer_id`."""
        super().__init__(node, f"link_{peer_id}")
        self.peer_id = peer_id
        peer = node.cluster.data.member(peer_id) if node.cluster.data else None
        self._attr_translation_placeholders = {"peer": short_name(peer, peer_id[:8])}

    @property
    @override
    def is_on(self) -> bool | None:
        """The link is up."""
        data = self.coordinator.data
        if data is None or data.cluster is None:
            return None
        return data.cluster.link_up(self.peer_id)

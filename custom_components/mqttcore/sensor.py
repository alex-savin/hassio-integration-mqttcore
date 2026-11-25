"""Sensors: the cluster's totals and state, and each node's counters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import MqttcoreConfigEntry
from .coordinator import (
    ClusterData,
    MqttcoreClusterCoordinator,
    MqttcoreNodeCoordinator,
    MqttcoreNodeManager,
    NodeData,
)
from .devices import node_is_board
from .entity import (
    MqttcoreClusterEntity,
    MqttcoreClusterTotalEntity,
    MqttcoreMemberEntity,
    MqttcoreNodeEntity,
)
from .pymqttcore import ClusterMember

PARALLEL_UPDATES = 0

UNIT_MESSAGES_PER_SECOND = "msg/s"
ACME_STATES = ["idle", "running", "ok", "failed"]
MEMBER_STATES = ["alive", "suspect", "dead", "left"]

# A start time that moves by less than this between readings is jitter.
UPTIME_TOLERANCE = timedelta(minutes=1)


def _epoch(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


def _sum_or_none(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


# ---- the cluster device ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MqttcoreClusterSensorDescription(SensorEntityDescription):
    """A cluster sensor read from the cluster coordinator."""

    value_fn: Callable[[ClusterData], StateType | datetime]
    available_fn: Callable[[ClusterData], bool] = lambda _: True


def _leader(data: ClusterData) -> str | None:
    leader = data.cluster.leader
    if leader is None:
        return None
    member = data.cluster.member(leader.node_id)
    return member.label if member is not None else leader.label


def _has_certificate(data: ClusterData) -> bool:
    return data.tls is not None and data.tls.has_details


def _certificate_expiry(data: ClusterData) -> datetime | None:
    return _epoch(data.tls.not_after_epoch) if data.tls is not None else None


def _acme_state(data: ClusterData) -> str | None:
    if data.acme is None or data.acme.state not in ACME_STATES:
        return None
    return data.acme.state


def _round1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


CLUSTER_SENSORS: tuple[MqttcoreClusterSensorDescription, ...] = (
    MqttcoreClusterSensorDescription(
        key="members_alive",
        translation_key="members_alive",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: sum(1 for m in d.cluster.members if m.alive),
        available_fn=lambda d: d.clustered,
    ),
    MqttcoreClusterSensorDescription(
        key="members",
        translation_key="members",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: sum(1 for m in d.cluster.members if m.state != "left"),
        available_fn=lambda d: d.clustered,
    ),
    MqttcoreClusterSensorDescription(
        key="leader",
        translation_key="leader",
        value_fn=_leader,
        available_fn=lambda d: d.clustered,
    ),
    MqttcoreClusterSensorDescription(
        key="retained_set_used",
        translation_key="retained_set_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _round1(d.retained_used()),
        available_fn=lambda d: d.retained_used() is not None,
    ),
    MqttcoreClusterSensorDescription(
        key="raft_term",
        translation_key="raft_term",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.cluster.raft.term if d.cluster.raft is not None else None,
        available_fn=lambda d: d.clustered and d.cluster.raft is not None,
    ),
    MqttcoreClusterSensorDescription(
        key="certificate_expiry",
        translation_key="certificate_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_certificate_expiry,
        available_fn=_has_certificate,
    ),
    MqttcoreClusterSensorDescription(
        key="acme_state",
        translation_key="acme_state",
        device_class=SensorDeviceClass.ENUM,
        options=ACME_STATES,
        value_fn=_acme_state,
        available_fn=lambda d: d.acme is not None,
    ),
    MqttcoreClusterSensorDescription(
        key="acme_last_success",
        translation_key="acme_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _epoch(d.acme.last_success) if d.acme is not None else None,
        available_fn=lambda d: d.acme is not None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class MqttcoreTotalSensorDescription(SensorEntityDescription):
    """A cluster sensor summed over the nodes."""

    value_fn: Callable[[ClusterData, list[NodeData]], StateType]


def _connections(cluster: ClusterData, nodes: list[NodeData]) -> int:
    if cluster.clustered:
        # Every member's open MQTT connections, as the gossip carries them:
        # this counts the nodes Home Assistant cannot reach too.
        return sum(m.load for m in cluster.cluster.members if m.live)
    return sum(n.stats.connections for n in nodes)


def _retained(cluster: ClusterData, nodes: list[NodeData]) -> int | None:
    raft = cluster.cluster.raft
    if raft is not None and raft.retained_state != "off":
        return raft.retained_topics  # the cluster's one set, through Raft
    return max((n.stats.retained for n in nodes), default=None)


TOTAL_SENSORS: tuple[MqttcoreTotalSensorDescription, ...] = (
    MqttcoreTotalSensorDescription(
        key="connections",
        translation_key="connections",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_connections,
    ),
    MqttcoreTotalSensorDescription(
        key="messages_in_rate",
        translation_key="messages_in_rate",
        native_unit_of_measurement=UNIT_MESSAGES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _, nodes: _sum_or_none([n.rates.messages_in for n in nodes]),
    ),
    MqttcoreTotalSensorDescription(
        key="messages_out_rate",
        translation_key="messages_out_rate",
        native_unit_of_measurement=UNIT_MESSAGES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _, nodes: _sum_or_none([n.rates.messages_out for n in nodes]),
    ),
    MqttcoreTotalSensorDescription(
        key="retained",
        translation_key="retained",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_retained,
    ),
    MqttcoreTotalSensorDescription(
        key="failed_logins",
        translation_key="failed_logins",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _, nodes: sum(n.stats.auth_failures for n in nodes),
    ),
)


# ---- a node's device ---------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MqttcoreMemberSensorDescription(SensorEntityDescription):
    """A node sensor read from the cluster's member list."""

    value_fn: Callable[[ClusterMember], StateType]


MEMBER_SENSORS: tuple[MqttcoreMemberSensorDescription, ...] = (
    MqttcoreMemberSensorDescription(
        key="member_state",
        translation_key="member_state",
        device_class=SensorDeviceClass.ENUM,
        options=MEMBER_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.state if m.state in MEMBER_STATES else None,
    ),
)


@dataclass(frozen=True, slots=True)
class NodeKind:
    """What a node is, for deciding which entities it gets."""

    board: bool
    clustered: bool


@dataclass(frozen=True, kw_only=True)
class MqttcoreNodeSensorDescription(SensorEntityDescription):
    """A node sensor read from the node itself."""

    value_fn: Callable[[NodeData], StateType | datetime]
    exists_fn: Callable[[NodeKind], bool] = lambda _: True
    start_time: bool = False


NODE_SENSORS: tuple[MqttcoreNodeSensorDescription, ...] = (
    MqttcoreNodeSensorDescription(
        key="connections",
        translation_key="connections",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.stats.connections,
    ),
    MqttcoreNodeSensorDescription(
        key="sessions",
        translation_key="sessions",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.stats.sessions,
    ),
    MqttcoreNodeSensorDescription(
        key="subscriptions",
        translation_key="subscriptions",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.stats.subscriptions,
    ),
    MqttcoreNodeSensorDescription(
        key="retained",
        translation_key="retained",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.stats.retained,
    ),
    MqttcoreNodeSensorDescription(
        key="messages_in_rate",
        translation_key="messages_in_rate",
        native_unit_of_measurement=UNIT_MESSAGES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.rates.messages_in,
    ),
    MqttcoreNodeSensorDescription(
        key="messages_out_rate",
        translation_key="messages_out_rate",
        native_unit_of_measurement=UNIT_MESSAGES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.rates.messages_out,
    ),
    MqttcoreNodeSensorDescription(
        key="bytes_in_rate",
        translation_key="bytes_in_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.rates.bytes_in,
    ),
    MqttcoreNodeSensorDescription(
        key="bytes_out_rate",
        translation_key="bytes_out_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.rates.bytes_out,
    ),
    MqttcoreNodeSensorDescription(
        key="failed_logins",
        translation_key="failed_logins",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.stats.auth_failures,
    ),
    MqttcoreNodeSensorDescription(
        key="dropped_messages",
        translation_key="dropped_messages",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.stats.dropped,
    ),
    MqttcoreNodeSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: dt_util.utcnow() - timedelta(seconds=d.stats.uptime_s),
        start_time=True,
    ),
    MqttcoreNodeSensorDescription(
        key="version",
        translation_key="version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.device.version if d.device is not None else None,
    ),
    MqttcoreNodeSensorDescription(
        key="memory_internal_free",
        translation_key="memory_internal_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.stats.memory_hot_free,
        exists_fn=lambda kind: kind.board,
    ),
    MqttcoreNodeSensorDescription(
        key="memory_psram_free",
        translation_key="memory_psram_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.stats.memory_bulk_free,
        exists_fn=lambda kind: kind.board,
    ),
    MqttcoreNodeSensorDescription(
        key="raft_store_errors",
        translation_key="raft_store_errors",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: (
            d.cluster.raft.store_errors
            if d.cluster is not None and d.cluster.raft is not None
            else None
        ),
        exists_fn=lambda kind: kind.clustered,
    ),
)


def node_kind(node: MqttcoreNodeCoordinator) -> NodeKind:
    """What the node is, from its device or, until it answers, its member record."""
    device = node.data.device if node.data is not None else None
    return NodeKind(board=node_is_board(device, node.member), clustered=node.clustered)


def _node_entities(node: MqttcoreNodeCoordinator) -> list[SensorEntity]:
    kind = node_kind(node)
    entities: list[SensorEntity] = [
        MqttcoreNodeSensor(node, description)
        for description in NODE_SENSORS
        if description.exists_fn(kind)
    ]
    if node.clustered:
        entities.extend(MqttcoreMemberSensor(node, d) for d in MEMBER_SENSORS)
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MqttcoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the cluster's sensors and each node's, now and as members join."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [
        MqttcoreClusterSensor(runtime.cluster, description)
        for description in CLUSTER_SENSORS
    ]
    entities.extend(
        MqttcoreTotalSensor(runtime.cluster, runtime.nodes, description)
        for description in TOTAL_SENSORS
    )
    for node in runtime.nodes.nodes.values():
        entities.extend(_node_entities(node))
    async_add_entities(entities)

    @callback
    def _async_add_nodes(nodes: list[MqttcoreNodeCoordinator]) -> None:
        async_add_entities([e for node in nodes for e in _node_entities(node)])

    entry.async_on_unload(runtime.nodes.async_add_node_listener(_async_add_nodes))


class MqttcoreClusterSensor(MqttcoreClusterEntity, SensorEntity):
    """A sensor of the cluster device."""

    entity_description: MqttcoreClusterSensorDescription

    def __init__(
        self,
        coordinator: MqttcoreClusterCoordinator,
        description: MqttcoreClusterSensorDescription,
    ) -> None:
        """Describe the sensor."""
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
    def native_value(self) -> StateType | datetime:
        """The value."""
        return self.entity_description.value_fn(self.coordinator.data)


class MqttcoreTotalSensor(MqttcoreClusterTotalEntity, SensorEntity):
    """A cluster sensor summed over the nodes."""

    entity_description: MqttcoreTotalSensorDescription

    def __init__(
        self,
        coordinator: MqttcoreClusterCoordinator,
        nodes: MqttcoreNodeManager,
        description: MqttcoreTotalSensorDescription,
    ) -> None:
        """Describe the sensor."""
        super().__init__(coordinator, nodes, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """The value over the nodes that answered."""
        return self.entity_description.value_fn(self.coordinator.data, self.node_data)


class MqttcoreMemberSensor(MqttcoreMemberEntity, SensorEntity):
    """A node sensor read from the member list."""

    entity_description: MqttcoreMemberSensorDescription

    def __init__(
        self,
        node: MqttcoreNodeCoordinator,
        description: MqttcoreMemberSensorDescription,
    ) -> None:
        """Describe the sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """The value."""
        member = self.member
        return None if member is None else self.entity_description.value_fn(member)


class MqttcoreNodeSensor(MqttcoreNodeEntity, SensorEntity):
    """A node sensor read from the node itself."""

    entity_description: MqttcoreNodeSensorDescription

    def __init__(
        self, node: MqttcoreNodeCoordinator, description: MqttcoreNodeSensorDescription
    ) -> None:
        """Describe the sensor."""
        super().__init__(node, description.key)
        self.entity_description = description
        self._start: datetime | None = None

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """The value; a start time keeps still unless it really moved."""
        data: NodeData | None = self.coordinator.data  # None until it first answered
        if data is None:
            return None
        value = self.entity_description.value_fn(data)
        if not self.entity_description.start_time or not isinstance(value, datetime):
            return value
        if self._start is None or abs(value - self._start) > UPTIME_TOLERANCE:
            self._start = value
        return self._start

"""Data coordinators for mqttcore: one for the cluster, one per node.

The cluster coordinator reads `GET /api/cluster` from one node: the
configured one when it answers, else any other member at the URL the
cluster gave for it (its `host_name` and `web_port`). The same node also
gives the shared certificate (`GET /api/tls/status`) and the renewal
(`GET /api/acme`), asked of a board, since only boards read certificates
and run ACME.

Each node has its own coordinator for its `GET /api/stats`, its own view
of the cluster (its links, its Raft store) and, now and then,
`GET /api/device`. It asks the node directly at its URL, or through the
answering node's relay when that fails.

`MqttcoreNodeManager` follows the member list: it adds a node coordinator
(and its entities) when a member joins, and removes a member's device when
it has been gone for a while.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CERTIFICATE_REFRESH_INTERVAL,
    CLUSTER_SCAN_INTERVAL,
    CONF_KNOWN_URLS,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
    ENDPOINT_BACKOFF,
    LOCAL_NODE,
    LOGGER,
    NODE_REMOVE_AFTER,
    NODE_SCAN_INTERVAL,
)
from .devices import node_device_details, node_identifier, node_is_board
from .pymqttcore import (
    AcmeStatus,
    ClusterMember,
    ClusterStatus,
    Device,
    MqttcoreAuthenticationError,
    MqttcoreClient,
    MqttcoreConnectionError,
    MqttcoreError,
    MqttcoreForbiddenError,
    MqttcoreNotSupportedError,
    Stats,
    TlsStatus,
    build_url,
)

if TYPE_CHECKING:
    from . import MqttcoreConfigEntry


def _now() -> float:
    return time.monotonic()


@dataclass(slots=True)
class ClusterData:
    """What the cluster coordinator knows after a refresh."""

    cluster: ClusterStatus
    endpoint: str
    tls: TlsStatus | None = None
    acme: AcmeStatus | None = None

    @property
    def clustered(self) -> bool:
        """The answering node runs a cluster."""
        return self.cluster.enabled

    def node_ids(self) -> list[str]:
        """The members that get a device: every one that has not left."""
        if not self.cluster.enabled:
            return [LOCAL_NODE]
        return [
            m.node_id for m in self.cluster.members if m.state != "left" and m.node_id
        ]

    def member(self, node_id: str) -> ClusterMember | None:
        """A member's record, as the answering node sees it."""
        if node_id == LOCAL_NODE:
            return None
        return self.cluster.member(node_id)


@dataclass(slots=True)
class Rates:
    """Per-second rates from two readings of a node's counters."""

    messages_in: float | None = None
    messages_out: float | None = None
    bytes_in: float | None = None
    bytes_out: float | None = None


@dataclass(slots=True)
class NodeData:
    """What a node coordinator knows after a refresh."""

    stats: Stats
    device: Device | None
    cluster: ClusterStatus | None
    rates: Rates = field(default_factory=Rates)
    relayed: bool = False


def _certificate_source(cluster: ClusterStatus) -> str | None:
    """The member to ask for the certificate and ACME: a board, the leader first.

    None means the answering node itself. Only boards read certificates and
    run ACME (`can_order`); every member holds the same certificate.
    """
    if not cluster.enabled:
        return None
    me = cluster.self_member
    if me is not None and me.can_order:
        return None
    boards = [m for m in cluster.members if m.alive and m.can_order and not m.is_self]
    for member in boards:
        if member.is_leader:
            return member.node_id
    return boards[0].node_id if boards else None


class MqttcoreClusterCoordinator(DataUpdateCoordinator[ClusterData]):
    """Reads the cluster from whichever member answers."""

    config_entry: MqttcoreConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MqttcoreConfigEntry) -> None:
        """Set up the coordinator from the entry's connection data."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cluster",
            update_interval=CLUSTER_SCAN_INTERVAL,
        )
        data = entry.data
        self._verify_ssl: bool = data[CONF_VERIFY_SSL]
        self._token: str = data[CONF_TOKEN]
        self._session = async_get_clientsession(hass, verify_ssl=self._verify_ssl)
        self.configured_url = build_url(
            data[CONF_HOST], data[CONF_PORT], ssl=data[CONF_SSL]
        )
        self._clients: dict[str, MqttcoreClient] = {}
        self._backoff_until: dict[str, float] = {}
        self._certificate_at: float | None = None
        self._tls: TlsStatus | None = None
        self._acme: AcmeStatus | None = None
        self.active_url: str | None = None

    # ---- endpoints ----------------------------------------------------------------------

    def client_for(self, url: str) -> MqttcoreClient:
        """The client for one node URL (made once)."""
        if (client := self._clients.get(url)) is None:
            client = MqttcoreClient(
                self._session, url, token=self._token, verify_ssl=self._verify_ssl
            )
            self._clients[url] = client
        return client

    @property
    def active_client(self) -> MqttcoreClient | None:
        """The client of the node that answered last."""
        return None if self.active_url is None else self.client_for(self.active_url)

    @property
    def relay_client(self) -> MqttcoreClient | None:
        """A client for the node the cluster was read from, at its own URL.

        Its relay reaches the other members. Its own URL, not the configured
        one: that may be the cluster's name, and a relay asked of the node it
        names fails.
        """
        me = self.data.cluster.self_member if self.data is not None else None
        if me is not None and me.url is not None and not self.in_backoff(me.url):
            return self.client_for(me.url)
        return self.active_client

    @property
    def active_node_id(self) -> str | None:
        """The node ID of the node that answered last (None without a cluster)."""
        if self.data is None or not self.data.cluster.enabled:
            return None
        return self.data.cluster.node_id

    def in_backoff(self, url: str) -> bool:
        """The URL failed recently: try it after the others."""
        return self._backoff_until.get(url, 0.0) > _now()

    def mark_failed(self, url: str) -> None:
        """Put a URL that did not answer at the end of the queue for a while."""
        self._backoff_until[url] = _now() + ENDPOINT_BACKOFF.total_seconds()

    def mark_ok(self, url: str) -> None:
        """A URL answered."""
        self._backoff_until.pop(url, None)

    def candidate_urls(self) -> list[str]:
        """The configured node first, then the other members, failed ones last."""
        urls: list[str] = [self.configured_url]
        if self.data is not None:
            urls.extend(
                url for m in self.data.cluster.members if m.live and (url := m.url)
            )
        urls.extend(self.config_entry.data.get(CONF_KNOWN_URLS, []))
        unique = list(dict.fromkeys(urls))
        return [u for u in unique if not self.in_backoff(u)] + [
            u for u in unique if self.in_backoff(u)
        ]

    # ---- refresh ------------------------------------------------------------------------

    async def _async_update_data(self) -> ClusterData:
        """Read the cluster from the first node that answers."""
        last_error: MqttcoreError | None = None
        for url in self.candidate_urls():
            client = self.client_for(url)
            try:
                cluster = await client.get_cluster()
            except MqttcoreAuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="token_rejected"
                ) from err
            except MqttcoreForbiddenError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="token_refused",
                    translation_placeholders={"error": err.message},
                ) from err
            except MqttcoreError as err:
                LOGGER.debug("%s did not answer: %s", url, err)
                self.mark_failed(url)
                last_error = err
                continue
            self.mark_ok(url)
            if url != self.active_url:
                LOGGER.debug("Reading the cluster from %s", url)
            self.active_url = url
            me = cluster.self_member
            own = me.url if me is not None and me.url else None
            node_client = (
                self.client_for(own) if own and not self.in_backoff(own) else client
            )
            await self._async_refresh_certificate(node_client, cluster)
            self._async_remember_urls(cluster)
            return ClusterData(
                cluster=cluster, endpoint=url, tls=self._tls, acme=self._acme
            )
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="cluster_unreachable",
            translation_placeholders={"error": str(last_error)},
        )

    async def async_issue_certificate(self) -> None:
        """Order a certificate now, from the board that orders them (the leader)."""
        client = self.active_client
        data = self.data
        if client is None or data is None:
            raise MqttcoreConnectionError("the cluster does not answer")
        await client.issue_certificate(node_id=_certificate_source(data.cluster))

    async def _async_refresh_certificate(
        self, client: MqttcoreClient, cluster: ClusterStatus
    ) -> None:
        """Read the shared certificate and the renewal, every few minutes."""
        now = _now()
        if (
            self._certificate_at is not None
            and now - self._certificate_at
            < CERTIFICATE_REFRESH_INTERVAL.total_seconds()
        ):
            return
        self._certificate_at = now
        source = _certificate_source(cluster)
        try:
            self._tls = await client.get_tls_status(node_id=source)
        except MqttcoreForbiddenError as err:
            LOGGER.debug("The token does not read the system area: %s", err)
            self._tls = None
        except MqttcoreError as err:
            LOGGER.debug("Could not read the certificate: %s", err)
        try:
            self._acme = await client.get_acme(node_id=source)
        except MqttcoreNotSupportedError, MqttcoreForbiddenError:
            self._acme = None  # the host binary, or no system scope
        except MqttcoreError as err:
            LOGGER.debug("Could not read the ACME state: %s", err)

    @callback
    def _async_remember_urls(self, cluster: ClusterStatus) -> None:
        """Keep the members' URLs, so a restart can start from any of them."""
        urls = sorted(
            {url for m in cluster.members if m.state != "left" and (url := m.url)}
        )
        entry = self.config_entry
        if urls and urls != sorted(entry.data.get(CONF_KNOWN_URLS, [])):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_KNOWN_URLS: urls}
            )


class MqttcoreNodeCoordinator(DataUpdateCoordinator[NodeData]):
    """Reads one node: directly at its URL, or through the cluster's relay."""

    config_entry: MqttcoreConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MqttcoreConfigEntry,
        cluster: MqttcoreClusterCoordinator,
        node_id: str,
    ) -> None:
        """Follow the node `node_id` (`LOCAL_NODE` without a cluster)."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} node {node_id}",
            update_interval=NODE_SCAN_INTERVAL,
        )
        self.node_id = node_id
        self.cluster = cluster
        self._device: Device | None = None
        self._device_at: float | None = None
        self._previous: tuple[float, Stats] | None = None

    @property
    def member(self) -> ClusterMember | None:
        """The node's record in the cluster's member list."""
        data: ClusterData | None = self.cluster.data  # None before the first refresh
        return None if data is None else data.member(self.node_id)

    @property
    def clustered(self) -> bool:
        """The node belongs to a cluster."""
        return self.node_id != LOCAL_NODE

    def _routes(self) -> list[tuple[MqttcoreClient, str | None, str | None]]:
        """Ways to reach the node: (client, relay node ID, direct URL).

        The node's own URL first; then the relay of the node the cluster was
        read from, at that node's own URL. The configured URL comes last for
        the node that answered on it: it may be the cluster's name, which
        DNS can answer with any member.
        """
        cc = self.cluster
        active = cc.active_client
        if not self.clustered:
            return [] if active is None else [(active, None, cc.active_url)]
        routes: list[tuple[MqttcoreClient, str | None, str | None]] = []
        member = self.member
        direct = member.url if member is not None else None
        if direct is not None:
            routes.append((cc.client_for(direct), None, direct))
        if cc.active_node_id == self.node_id:
            if active is not None and cc.active_url != direct:
                routes.append((active, None, cc.active_url))
            return routes
        if (relay := cc.relay_client) is not None:
            via_relay = (relay, self.node_id, None)
            if direct is not None and cc.in_backoff(direct):
                routes.insert(0, via_relay)
            else:
                routes.append(via_relay)
        return routes

    @property
    def is_board(self) -> bool:
        """The node is a board: it updates and restarts itself."""
        device = self.data.device if self.data is not None else None
        return node_is_board(device, self.member)

    async def async_call[T](
        self, call: Callable[[MqttcoreClient, str | None], Awaitable[T]]
    ) -> T:
        """Make one call on the node, by its first route that is reachable.

        `call(client, relay_node)` gets the client and, through a relay, the
        node's ID for `node_id=`. Only an unreachable route moves on to the
        next; any answer, an error one included, is the node's.
        """
        last_error: MqttcoreConnectionError | None = None
        for client, relay_node, _direct_url in self._routes():
            try:
                return await call(client, relay_node)
            except MqttcoreConnectionError as err:
                LOGGER.debug("Node %s via %s: %s", self.node_id, client.url, err)
                last_error = err
        raise MqttcoreConnectionError(str(last_error or "no route to the node"))

    async def _async_update_data(self) -> NodeData:
        """Read the node's counters, its view of the cluster and its device."""
        last_error: Exception | None = None
        for client, relay_node, direct_url in self._routes():
            try:
                view = await self._async_read_view(client, relay_node)
                stats = await client.get_stats(node_id=relay_node)
            except MqttcoreError as err:
                LOGGER.debug("Node %s via %s: %s", self.node_id, client.url, err)
                if direct_url is not None:
                    self.cluster.mark_failed(direct_url)
                last_error = err
                continue
            if direct_url is not None:
                self.cluster.mark_ok(direct_url)
            await self._async_refresh_device(client, relay_node)
            rates = self._rates(stats)
            return NodeData(
                stats=stats,
                device=self._device,
                cluster=view,
                rates=rates,
                relayed=relay_node is not None,
            )
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="node_unreachable",
            translation_placeholders={"error": str(last_error or "no route")},
        )

    async def _async_read_view(
        self, client: MqttcoreClient, relay_node: str | None
    ) -> ClusterStatus | None:
        """The node's own view of the cluster; it must be this node's."""
        if not self.clustered:
            return None
        view = await client.get_cluster(node_id=relay_node)
        if view.enabled and view.node_id != self.node_id:
            # A name DNS answers with any member (the cluster's) reached another.
            raise MqttcoreError(f"{client.url} answered for {view.node_name}")
        return view

    async def _async_refresh_device(
        self, client: MqttcoreClient, relay_node: str | None
    ) -> None:
        """Read `GET /api/device` at first, now and then, and after an update."""
        member = self.member
        now = _now()
        stale = (
            self._device is None
            or self._device_at is None
            or now - self._device_at >= DEVICE_REFRESH_INTERVAL.total_seconds()
            or (member is not None and member.version != self._device.version)
        )
        if not stale:
            return
        try:
            device = await client.get_device(node_id=relay_node)
        except MqttcoreError as err:
            LOGGER.debug("Node %s: could not read the device: %s", self.node_id, err)
            return
        self._device_at = now
        changed = self._device is None or self._device.raw != device.raw
        self._device = device
        if changed:
            self._async_update_device_registry()

    def _rates(self, stats: Stats) -> Rates:
        """Per-second rates since the last reading; none after a restart."""
        now = _now()
        previous, self._previous = self._previous, (now, stats)
        if previous is None:
            return Rates()
        then, old = previous
        elapsed = now - then
        if (
            elapsed <= 0
            or stats.uptime_s < old.uptime_s
            or stats.messages_in < old.messages_in
            or stats.messages_out < old.messages_out
        ):
            return Rates()  # the broker started again: its counters did too
        return Rates(
            messages_in=(stats.messages_in - old.messages_in) / elapsed,
            messages_out=(stats.messages_out - old.messages_out) / elapsed,
            bytes_in=max(stats.bytes_in - old.bytes_in, 0) / elapsed,
            bytes_out=max(stats.bytes_out - old.bytes_out, 0) / elapsed,
        )

    @callback
    def _async_update_device_registry(self) -> None:
        """Put the model and versions from `GET /api/device` on the node's device."""
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, node_identifier(self.config_entry, self.node_id)),
            self.config_entry.entry_id,
        )
        if device is None or self._device is None:
            return
        details = node_device_details(self._device)
        device_registry.async_update_device(device.id, **details)


type NodeListener = Callable[[list[MqttcoreNodeCoordinator]], None]


class MqttcoreNodeManager:
    """Keeps one node coordinator per member, as the member list changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MqttcoreConfigEntry,
        cluster: MqttcoreClusterCoordinator,
    ) -> None:
        """Follow the members `cluster` reports."""
        self.hass = hass
        self.entry = entry
        self.cluster = cluster
        self.nodes: dict[str, MqttcoreNodeCoordinator] = {}
        self._node_listeners: list[NodeListener] = []
        self._remove_listeners: list[Callable[[str], None]] = []
        self._update_listeners: list[CALLBACK_TYPE] = []
        self._node_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._missing_since: dict[str, float] = {}
        self._unsub_cluster: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Make the first coordinators, read each node once, and start following."""
        if self.cluster.data is not None:
            created = [
                self._create(node_id) for node_id in self.cluster.data.node_ids()
            ]
            await asyncio.gather(*(c.async_refresh() for c in created))
        self._unsub_cluster = self.cluster.async_add_listener(
            self._async_cluster_updated
        )

    async def async_shutdown(self) -> None:
        """Stop every node coordinator."""
        if self._unsub_cluster is not None:
            self._unsub_cluster()
            self._unsub_cluster = None
        for node_id in list(self.nodes):
            await self._async_drop(node_id)

    def _create(self, node_id: str) -> MqttcoreNodeCoordinator:
        coordinator = MqttcoreNodeCoordinator(
            self.hass, self.entry, self.cluster, node_id
        )
        self.nodes[node_id] = coordinator
        self._node_unsubs[node_id] = coordinator.async_add_listener(
            self._async_node_updated
        )
        return coordinator

    async def _async_drop(self, node_id: str) -> None:
        if (unsub := self._node_unsubs.pop(node_id, None)) is not None:
            unsub()
        if (coordinator := self.nodes.pop(node_id, None)) is not None:
            await coordinator.async_shutdown()

    @callback
    def async_add_node_listener(self, listener: NodeListener) -> CALLBACK_TYPE:
        """Call `listener` with the coordinators of members that joined."""
        self._node_listeners.append(listener)
        return lambda: self._node_listeners.remove(listener)

    @callback
    def async_add_remove_listener(
        self, listener: Callable[[str], None]
    ) -> CALLBACK_TYPE:
        """Call `listener` with the node ID of a member whose device was removed."""
        self._remove_listeners.append(listener)
        return lambda: self._remove_listeners.remove(listener)

    @callback
    def async_add_update_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `listener` whenever any node coordinator refreshed (for totals)."""
        self._update_listeners.append(listener)
        return lambda: self._update_listeners.remove(listener)

    @callback
    def _async_node_updated(self) -> None:
        for listener in list(self._update_listeners):
            listener()

    @callback
    def _async_cluster_updated(self) -> None:
        """Add the members that joined; drop those gone for long enough."""
        data = self.cluster.data
        if data is None or not self.cluster.last_update_success:
            return
        current = set(data.node_ids())
        new = [node_id for node_id in current if node_id not in self.nodes]
        if new:
            created = [self._create(node_id) for node_id in new]
            self.entry.async_create_task(
                self.hass, self._async_announce(created), "mqttcore new members"
            )
        now = _now()
        for node_id in current:
            self._missing_since.pop(node_id, None)
        for node_id in list(self.nodes):
            if node_id in current:
                continue
            since = self._missing_since.setdefault(node_id, now)
            if now - since >= NODE_REMOVE_AFTER.total_seconds():
                self._missing_since.pop(node_id, None)
                self.entry.async_create_task(
                    self.hass, self._async_remove(node_id), "mqttcore member gone"
                )

    async def _async_announce(self, created: list[MqttcoreNodeCoordinator]) -> None:
        for coordinator in created:
            await coordinator.async_refresh()
        LOGGER.debug("New members: %s", [c.node_id for c in created])
        for listener in list(self._node_listeners):
            listener(created)

    async def _async_remove(self, node_id: str) -> None:
        """Remove a departed member's coordinator, device and the links to it."""
        LOGGER.info("Member %s left the cluster: removing its device", node_id)
        await self._async_drop(node_id)
        async_remove_node_device(self.hass, self.entry, node_id)
        for listener in list(self._remove_listeners):
            listener(node_id)


@callback
def async_remove_node_device(
    hass: HomeAssistant, entry: MqttcoreConfigEntry, node_id: str
) -> None:
    """Remove a node's device and the other nodes' link entities to it."""
    entity_registry = er.async_get(hass)
    suffix = f"_link_{node_id}"
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id.endswith(suffix):
            entity_registry.async_remove(entity.entity_id)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, node_identifier(entry, node_id)), entry.entry_id
    )
    if device is not None:
        device_registry.async_remove_device(device.id)


def debug_snapshot(coordinator: MqttcoreNodeCoordinator) -> dict[str, Any]:
    """The raw answers a node coordinator holds (diagnostics redact them)."""
    data: NodeData | None = coordinator.data  # None before the first refresh
    if data is None:
        return {"available": False}
    return {
        "available": coordinator.last_update_success,
        "relayed": data.relayed,
        "stats": data.stats.raw,
        "device": data.device.raw if data.device is not None else None,
        "cluster": data.cluster.raw if data.cluster is not None else None,
    }

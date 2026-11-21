"""Typed views of the mqttcore admin API's answers (API version 1).

Each model is built with `from_dict` from the JSON a route returns, as the
firmware's JSON writers produce it (docs/api.md in the mqttcore repository).
The API only ever adds fields, so parsing ignores members it does not know
and tolerates missing ones: a member documented "only when ..." becomes
None. Every model keeps the answer it was built from as `raw`, for
diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The host binary reports free memory as SIZE_MAX: "free memory means
# nothing there". Anything this large is not a real measurement.
_SIZE_MAX_FLOOR = 1 << 63

LIVE_STATES = frozenset({"alive", "suspect"})


def _obj(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _int(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _opt_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _opt_bool(data: dict[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None


def _objects(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _memory(data: dict[str, Any], key: str) -> int | None:
    value = _opt_int(data, key)
    if value is None or value >= _SIZE_MAX_FLOOR:
        return None
    return value


def _positive_or_none(value: int | None) -> int | None:
    """0 means never or unknown in mqttcore's times."""
    if value is None or value <= 0:
        return None
    return value


def _known_or_none(value: int | None) -> int | None:
    """-1 means not known (no clock, no certificate) in day counts."""
    if value is None or value < 0:
        return None
    return value


# ---- GET /api/auth/status ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """What a node says before anyone signs in (an open route)."""

    login: bool
    setup_needed: bool
    trusted_network: bool
    https: bool
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> AuthStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            login=_bool(d, "login"),
            setup_needed=_bool(d, "setup_needed"),
            trusted_network=_bool(d, "trusted_network"),
            https=_bool(d, "https"),
            raw=d,
        )


# ---- GET /api/config (the parts a client needs) -----------------------------------------


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """The few settings that identify a node and its cluster."""

    generation: int
    server_name: str
    host_name: str
    ota_password_set: bool
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> ServerConfig:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            generation=_int(d, "generation"),
            server_name=_str(d, "server_name"),
            host_name=_str(d, "host_name"),
            ota_password_set=_bool(d, "ota_password_set"),
            raw=d,
        )


# ---- GET /api/stats ---------------------------------------------------------------------

_DROPPED_KEYS = (
    "dropped_queue_full",
    "dropped_offline_qos0",
    "dropped_too_large",
    "dropped_expired",
    "dropped_retained_full",
)


@dataclass(frozen=True, slots=True)
class Stats:
    """The broker's counters since it started (they reset with a restart)."""

    uptime_s: int
    connections: int
    sessions: int
    subscriptions: int
    retained: int
    messages_in: int
    messages_out: int
    bytes_in: int
    bytes_out: int
    dropped: int
    auth_failures: int
    authz_denied: int
    protocol_errors: int
    slow_consumers: int
    rate_limited: int
    memory_hot_free: int | None
    memory_bulk_free: int | None
    retain_mode: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> Stats:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            uptime_s=_int(d, "uptime_s"),
            connections=_int(d, "connections"),
            sessions=_int(d, "sessions"),
            subscriptions=_int(d, "subscriptions"),
            retained=_int(d, "retained"),
            messages_in=_int(d, "messages_in"),
            messages_out=_int(d, "messages_out"),
            bytes_in=_int(d, "bytes_in"),
            bytes_out=_int(d, "bytes_out"),
            dropped=sum(_int(d, key) for key in _DROPPED_KEYS),
            auth_failures=_int(d, "auth_failures"),
            authz_denied=_int(d, "authz_denied"),
            protocol_errors=_int(d, "protocol_errors"),
            slow_consumers=_int(d, "slow_consumers"),
            rate_limited=_int(d, "rate_limited"),
            memory_hot_free=_memory(d, "memory_hot_free"),
            memory_bulk_free=_memory(d, "memory_bulk_free"),
            retain_mode=_str(d, "retain_mode", "raft"),
            raw=d,
        )


# ---- GET /api/device --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChipInfo:
    """The board's chip: model, silicon revision and the image's family."""

    model: str
    revision: str | None
    image: str | None

    @classmethod
    def from_dict(cls, data: Any) -> ChipInfo | None:
        """Build from the `chip` object; None for the host's null."""
        if not isinstance(data, dict):
            return None
        return cls(
            model=_str(data, "model"),
            revision=_opt_str(data, "revision"),
            image=_opt_str(data, "image"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """What a node is and what it runs."""

    version: str
    build: str
    node: str
    port: str
    uptime_s: int
    wall_clock: bool
    mac: str | None
    chip: ChipInfo | None
    idf: str | None
    last_reset: str | None
    coredump_stored: bool | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def is_board(self) -> bool:
        """The node is an ESP32-P4 board, not the host binary."""
        return self.port == "esp32p4" or self.chip is not None

    @classmethod
    def from_dict(cls, data: Any) -> Device:
        """Build from the route's JSON."""
        d = _obj(data)
        last_reset = d.get("last_reset")
        coredump = d.get("coredump")
        return cls(
            version=_str(d, "version"),
            build=_str(d, "build"),
            node=_str(d, "node"),
            port=_str(d, "port"),
            uptime_s=_int(d, "uptime_s"),
            wall_clock=_bool(d, "wall_clock"),
            mac=_opt_str(d, "mac"),
            chip=ChipInfo.from_dict(d.get("chip")),
            idf=_opt_str(d, "idf"),
            last_reset=_opt_str(last_reset, "reason")
            if isinstance(last_reset, dict)
            else None,
            coredump_stored=_opt_bool(coredump, "stored")
            if isinstance(coredump, dict)
            else None,
            raw=d,
        )


# ---- GET /api/cluster -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberCertificate:
    """A member's certificate as the gossip carries it."""

    fingerprint: str
    not_before_epoch: int | None

    @classmethod
    def from_dict(cls, data: Any) -> MemberCertificate | None:
        """Build from a member's `certificate`; None when it holds none."""
        if not isinstance(data, dict):
            return None
        return cls(
            fingerprint=_str(data, "fingerprint"),
            not_before_epoch=_positive_or_none(_opt_int(data, "not_before_epoch")),
        )


@dataclass(frozen=True, slots=True)
class ClusterMember:
    """One member as the answering node sees it."""

    node_id: str
    name: str
    state: str
    is_self: bool
    address: str
    version: str
    load: int
    host_name: str | None
    web_port: int | None
    mqtt_port: int
    tls_port: int
    for_s: int
    draining: bool
    config_state: str
    is_leader: bool
    can_order: bool | None
    generation: int | None
    certificate: MemberCertificate | None
    isolated: bool
    synced: bool

    @property
    def alive(self) -> bool:
        """The member answers gossip."""
        return self.state == "alive"

    @property
    def live(self) -> bool:
        """Alive or suspected: not given up on yet."""
        return self.state in LIVE_STATES

    @property
    def url(self) -> str | None:
        """Its HTTPS console and API, by its DNS name; None without both."""
        if not self.host_name or not self.web_port:
            return None
        if self.web_port == 443:
            return f"https://{self.host_name}/"
        return f"https://{self.host_name}:{self.web_port}/"

    @property
    def label(self) -> str:
        """The name people know it by: its DNS name, else its node name."""
        return self.host_name or self.name or self.node_id

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterMember:
        """Build from one entry of `members`."""
        return cls(
            node_id=_str(data, "node_id"),
            name=_str(data, "name"),
            state=_str(data, "state", "unknown"),
            is_self=_bool(data, "self"),
            address=_str(data, "address"),
            version=_str(data, "version"),
            load=_int(data, "load"),
            host_name=_opt_str(data, "host_name"),
            web_port=_positive_or_none(_opt_int(data, "web_port")),
            mqtt_port=_int(data, "mqtt_port"),
            tls_port=_int(data, "tls_port"),
            for_s=_int(data, "for_s"),
            draining=_bool(data, "draining"),
            config_state=_str(data, "config_state", "unknown"),
            is_leader=_bool(data, "leader"),
            can_order=_opt_bool(data, "can_order"),
            generation=_opt_int(data, "generation"),
            certificate=MemberCertificate.from_dict(data.get("certificate")),
            isolated=_bool(data, "isolated"),
            synced=_bool(data, "synced"),
        )


@dataclass(frozen=True, slots=True)
class ClusterLink:
    """One peer link of the answering node."""

    link_id: int
    state: str
    direction: str
    node_id: str | None
    node_name: str
    for_s: int
    unacked_msgs: int
    dropped: int

    @property
    def up(self) -> bool:
        """The link carries traffic."""
        return self.state == "up"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterLink:
        """Build from one entry of `links`."""
        return cls(
            link_id=_int(data, "id"),
            state=_str(data, "state", "unknown"),
            direction=_str(data, "direction"),
            node_id=_opt_str(data, "node_id"),
            node_name=_str(data, "node_name"),
            for_s=_int(data, "for_s"),
            unacked_msgs=_int(data, "unacked_msgs"),
            dropped=_int(data, "dropped"),
        )


@dataclass(frozen=True, slots=True)
class LeaderInfo:
    """Who leads, as the answering node sees it."""

    node_id: str
    name: str
    host_name: str
    url: str | None

    @property
    def label(self) -> str:
        """Its DNS name, else its node name."""
        return self.host_name or self.name or self.node_id

    @classmethod
    def from_dict(cls, data: Any) -> LeaderInfo | None:
        """Build from `leader`; None while nobody leads."""
        if not isinstance(data, dict):
            return None
        return cls(
            node_id=_str(data, "node_id"),
            name=_str(data, "name"),
            host_name=_str(data, "host_name"),
            url=_opt_str(data, "url"),
        )


@dataclass(frozen=True, slots=True)
class RaftMember:
    """One member of the Raft configuration in force."""

    node_id: str
    name: str
    role: str
    weight: int
    is_self: bool
    linked: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RaftMember:
        """Build from one entry of `raft.members`."""
        return cls(
            node_id=_str(data, "node_id"),
            name=_str(data, "name"),
            role=_str(data, "role"),
            weight=_int(data, "weight"),
            is_self=_bool(data, "self"),
            linked=_bool(data, "linked"),
        )


@dataclass(frozen=True, slots=True)
class RaftStatus:
    """Raft on the answering node."""

    on: bool
    durable: bool
    decides_leader: bool
    group: str | None
    role: str
    term: int
    leader: LeaderInfo | None
    is_leader: bool
    stuck: bool
    commit: int
    applied: int
    members: tuple[RaftMember, ...]
    elections: int
    store_errors: int
    retained_state: str
    retained_topics: int
    sessions_state: str
    sessions_entries: int

    @classmethod
    def from_dict(cls, data: Any) -> RaftStatus | None:
        """Build from `raft`; None from a node without it."""
        if not isinstance(data, dict):
            return None
        counters = _obj(data.get("counters"))
        retained = _obj(data.get("retained"))
        sessions = _obj(data.get("sessions"))
        return cls(
            on=_bool(data, "on"),
            durable=_bool(data, "durable"),
            decides_leader=_bool(data, "decides_leader"),
            group=_opt_str(data, "group"),
            role=_str(data, "role"),
            term=_int(data, "term"),
            leader=LeaderInfo.from_dict(data.get("leader")),
            is_leader=_bool(data, "is_leader"),
            stuck=_bool(data, "stuck"),
            commit=_int(data, "commit"),
            applied=_int(data, "applied"),
            members=tuple(RaftMember.from_dict(m) for m in _objects(data, "members")),
            elections=_int(counters, "elections"),
            store_errors=_int(counters, "store_errors"),
            retained_state=_str(retained, "state", "off"),
            retained_topics=_int(retained, "topics"),
            sessions_state=_str(sessions, "state", "off"),
            sessions_entries=_int(sessions, "entries"),
        )


@dataclass(frozen=True, slots=True)
class DrainStatus:
    """The answering node's drain."""

    on: bool
    clients_left: int

    @classmethod
    def from_dict(cls, data: Any) -> DrainStatus | None:
        """Build from `drain`."""
        if not isinstance(data, dict):
            return None
        return cls(on=_bool(data, "on"), clients_left=_int(data, "clients_left"))


@dataclass(frozen=True, slots=True)
class BalanceStatus:
    """Load balancing on the answering node."""

    enabled: bool
    redirecting: bool
    load: int
    mean: int
    threshold_pct: int

    @classmethod
    def from_dict(cls, data: Any) -> BalanceStatus | None:
        """Build from `balance`."""
        if not isinstance(data, dict):
            return None
        return cls(
            enabled=_bool(data, "enabled"),
            redirecting=_bool(data, "redirecting"),
            load=_int(data, "load"),
            mean=_int(data, "mean"),
            threshold_pct=_int(data, "threshold_pct"),
        )


@dataclass(frozen=True, slots=True)
class ClusterStatus:
    """The cluster as one node sees it (`{"enabled": false}` without one)."""

    enabled: bool
    reason: str | None
    cluster: str
    node_id: str
    node_name: str
    joined: bool
    secure: bool
    term: int
    is_leader: bool
    leader: LeaderInfo | None
    members: tuple[ClusterMember, ...]
    links: tuple[ClusterLink, ...]
    raft: RaftStatus | None
    drain: DrainStatus | None
    balance: BalanceStatus | None
    dns_state: str | None
    shared_generation: int | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    def member(self, node_id: str) -> ClusterMember | None:
        """The member with this node ID, if this node knows it."""
        for member in self.members:
            if member.node_id == node_id:
                return member
        return None

    @property
    def self_member(self) -> ClusterMember | None:
        """The answering node's own record."""
        for member in self.members:
            if member.is_self:
                return member
        return None

    def link_up(self, node_id: str) -> bool:
        """A peer link to this member is up."""
        return any(link.up and link.node_id == node_id for link in self.links)

    @classmethod
    def from_dict(cls, data: Any) -> ClusterStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        shared = d.get("shared")
        dns = d.get("dns")
        return cls(
            enabled=_bool(d, "enabled"),
            reason=_opt_str(d, "reason"),
            cluster=_str(d, "cluster"),
            node_id=_str(d, "node_id"),
            node_name=_str(d, "node_name"),
            joined=_bool(d, "joined"),
            secure=_bool(d, "secure"),
            term=_int(d, "term"),
            is_leader=_bool(d, "is_leader"),
            leader=LeaderInfo.from_dict(d.get("leader")),
            members=tuple(ClusterMember.from_dict(m) for m in _objects(d, "members")),
            links=tuple(ClusterLink.from_dict(link) for link in _objects(d, "links")),
            raft=RaftStatus.from_dict(d.get("raft")),
            drain=DrainStatus.from_dict(d.get("drain")),
            balance=BalanceStatus.from_dict(d.get("balance")),
            dns_state=_opt_str(dns, "state") if isinstance(dns, dict) else None,
            shared_generation=_opt_int(shared, "generation")
            if isinstance(shared, dict)
            else None,
            raw=d,
        )


# ---- GET /api/tls/status ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TlsStatus:
    """The stored TLS material; the certificate's details on a board only."""

    certificate: bool
    client_ca: bool
    https: bool
    subject: str | None
    issuer: str | None
    not_before_epoch: int | None
    not_after_epoch: int | None
    days_left: int | None
    expired: bool | None
    dns_names: tuple[str, ...]
    staging: bool | None
    self_issued: bool | None
    certificate_error: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def has_details(self) -> bool:
        """The node read the certificate (the host never does)."""
        return self.not_after_epoch is not None

    @classmethod
    def from_dict(cls, data: Any) -> TlsStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            certificate=_bool(d, "certificate"),
            client_ca=_bool(d, "client_ca"),
            https=_bool(d, "https"),
            subject=_opt_str(d, "subject"),
            issuer=_opt_str(d, "issuer"),
            not_before_epoch=_opt_int(d, "not_before_epoch"),
            not_after_epoch=_opt_int(d, "not_after_epoch"),
            days_left=_known_or_none(_opt_int(d, "days_left")),
            expired=_opt_bool(d, "expired"),
            dns_names=_strings(d, "dns_names"),
            staging=_opt_bool(d, "staging"),
            self_issued=_opt_bool(d, "self_issued"),
            certificate_error=_opt_str(d, "certificate_error"),
            raw=d,
        )


# ---- GET /api/acme ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcmeStatus:
    """Where the Let's Encrypt renewal stands (a board; the host answers 501)."""

    enabled: bool
    staging: bool
    ready: bool
    state: str
    step: str
    error: str
    decision: str
    role: str
    attempts: int
    failures: int
    consecutive_failures: int
    last_success: int | None
    last_failure: int | None
    next_check_in_s: int
    days_until_expiry: int | None
    name_missing: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> AcmeStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            enabled=_bool(d, "enabled"),
            staging=_bool(d, "staging"),
            ready=_bool(d, "ready"),
            state=_str(d, "state", "idle"),
            step=_str(d, "step"),
            error=_str(d, "error"),
            decision=_str(d, "decision"),
            role=_str(d, "role"),
            attempts=_int(d, "attempts"),
            failures=_int(d, "failures"),
            consecutive_failures=_int(d, "consecutive_failures"),
            last_success=_positive_or_none(_opt_int(d, "last_success")),
            last_failure=_positive_or_none(_opt_int(d, "last_failure")),
            next_check_in_s=_int(d, "next_check_in_s"),
            days_until_expiry=_known_or_none(_opt_int(d, "days_until_expiry")),
            name_missing=_opt_str(d, "name_missing"),
            raw=d,
        )


# ---- GET /api/agents --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """One API token as `GET /api/agents` lists it (never the token itself)."""

    name: str
    scopes: dict[str, str]
    expires_at: int | None
    created: int | None
    networks: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenInfo:
        """Build from one entry of `tokens`."""
        scopes = _obj(data.get("scopes"))
        return cls(
            name=_str(data, "name"),
            scopes={k: v for k, v in scopes.items() if isinstance(v, str)},
            expires_at=_positive_or_none(_opt_int(data, "expires_at")),
            created=_positive_or_none(_opt_int(data, "created")),
            networks=_strings(data, "networks"),
        )


# ---- GET /api/ota (a board; the host answers 501) ------------------------------------


@dataclass(frozen=True, slots=True)
class OtaProgress:
    """An install from the update server, as `GET /api/ota` reports it under `update`."""

    state: str
    version: str
    message: str
    bytes: int
    total: int

    @property
    def running(self) -> bool:
        """An install is under way (it ends in a restart, or `failed`)."""
        return self.state in {"starting", "downloading", "checking", "restarting"}

    @property
    def percentage(self) -> int | None:
        """How much of the image is written, 0 to 100; None before the size is known."""
        if self.total <= 0:
            return None
        return max(0, min(100, self.bytes * 100 // self.total))

    @classmethod
    def from_dict(cls, data: Any) -> OtaProgress:
        """Build from `update`."""
        d = _obj(data)
        return cls(
            state=_str(d, "state", "idle"),
            version=_str(d, "version"),
            message=_str(d, "message"),
            bytes=_int(d, "bytes"),
            total=_int(d, "total"),
        )


@dataclass(frozen=True, slots=True)
class OtaState:
    """The board's firmware slots and image state (`GET /api/ota`)."""

    version: str
    image_state: str
    confirmed: bool
    running_partition: str
    upload_running: bool
    last_failure: str
    update: OtaProgress
    rolled_back_version: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> OtaState:
        """Build from the route's JSON."""
        d = _obj(data)
        rolled = d.get("rolled_back")
        rolled_version = (
            _opt_str(rolled, "version") if isinstance(rolled, dict) else None
        )
        return cls(
            version=_str(d, "version"),
            image_state=_str(d, "image_state"),
            confirmed=_bool(d, "confirmed", True),
            running_partition=_str(d, "running_partition"),
            upload_running=_bool(d, "upload_running"),
            last_failure=_str(d, "last_failure"),
            update=OtaProgress.from_dict(d.get("update")),
            rolled_back_version=rolled_version or None,
            raw=d,
        )


# ---- POST /api/update/check (a board) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """What the update server offers (`POST /api/update/check`)."""

    current_version: str
    latest_version: str | None
    update_available: bool
    verdict: str
    why: str
    published_at: str | None
    release_notes_url: str | None
    error: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> UpdateCheck:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            current_version=_str(d, "current_version"),
            latest_version=_opt_str(d, "latest_version") or None,
            update_available=_bool(d, "update_available"),
            verdict=_str(d, "verdict"),
            why=_str(d, "why"),
            published_at=_opt_str(d, "published_at") or None,
            release_notes_url=_opt_str(d, "release_notes_url") or None,
            error=_opt_str(d, "error"),
            raw=d,
        )

# MQTT Core for Home Assistant

A custom Home Assistant integration that monitors an
[MQTT Core](https://github.com/alex-savin/mqttcore) MQTT broker cluster over
its HTTPS admin API: members and leader, clients, message rates, retained
messages, failed logins, the shared certificate and its renewal, and each
node's own counters, memory and links.

It reads the cluster over REST, not over MQTT, so it keeps working when
Home Assistant's own MQTT connection is down, which is when you need it.

- Home Assistant 2026.11 or later (Python 3.14).
- MQTT Core 0.29 or later (API version 1); 0.30.0 or later for board
  updates, restarts and certificate renewal (the token's `maintenance`
  right). Boards (ESP32-P4), the Linux node and the Home Assistant app are
  all supported.
- Status: **phases 2 to 4 of the integration plan:** monitoring, board
  updates and maintenance, and control of users, the ACL, clients and the
  listeners. See [What comes next](#what-comes-next).

## Install

The repository is private while MQTT Core is. Once it is published, add it
to the Marketplace (HACS, built into Home Assistant since 2026.11) as a
custom repository:

1. Settings → Marketplace → ⋮ → Custom repositories.
2. Repository: `https://github.com/alex-savin/hassio-integration-mqttcore`,
   type **Integration**.
3. Install **MQTT Core**, then add it under Settings → Devices & services.

By hand: copy `custom_components/mqttcore` into your configuration
directory's `custom_components/` and restart Home Assistant.

## Set up

Home Assistant finds boards by mDNS (`_mqttcore._tcp`) and offers them
under Settings → Devices & services. Or add **MQTT Core** and enter one
node:

1. **Host and port**: any member's DNS name, the cluster's name, or an IP
   address, and its HTTPS port (443). Home Assistant learns the other
   members from `GET /api/cluster`. Under **Connection**, HTTPS and
   certificate verification are on; turn verification off only for a
   self-signed certificate. Use the name on the certificate, not an IP
   address, when verification is on.
2. **OTA password, once.** Home Assistant uses it to mint its own API token
   (`POST /api/agents`) and forgets it: the OTA password is never stored.
   The token reads the `status` and `system` areas and nothing else, never
   expires, and is named `home-assistant-<8 hex digits of this Home
   Assistant's instance ID>`. **Limit the token to** is filled in with the
   address Home Assistant reaches the node from; leave it, or empty it to
   let the token work from anywhere.

One entry per cluster: every member answers with the same identity (the
cluster's DNS name, `server_name`), so a second member is refused as
already configured, and every board's mDNS announcement is recognised.

If the token stops working (revoked, or the address limit no longer
matches), Home Assistant asks for the OTA password again and mints a new
token under the same name.

Removing the integration does not revoke the token (only the OTA password
can). Revoke it in the MQTT Core console (API tokens) or with
`POST /api/agents {"action": "revoke", "name": "home-assistant-…"}`.

## What you get

One device for the cluster and one per node; each node's device points to
the cluster's. Members that join get their device and entities; a member
that has left (or that the cluster no longer lists) for 30 minutes loses
them.

The integration talks to the configured node first. When it does not
answer, it uses any other member, at the URL the cluster gives for it
(`https://<host_name>[:<web_port>]/`). Each node is read directly at its
own URL, or through the answering node's relay (`/api/peer`) when Home
Assistant cannot reach it.

### The cluster device

| Entity | Type | Source |
|---|---|---|
| Members alive | sensor | `GET /api/cluster` `members[].state == "alive"` |
| Members (diagnostic) | sensor | members not `left` |
| Leader | sensor | `leader` (the member's `host_name`, else its name) |
| Raft term (diagnostic, disabled) | sensor | `raft.term` |
| Connections | sensor | sum of `members[].load` of the live members |
| Messages received / sent | sensor, msg/s | sum of the nodes' rates (below) |
| Retained messages | sensor | `raft.retained.topics` (the cluster's one set); without Raft, the largest node's `retained` |
| Failed logins | sensor, total increasing | sum of the nodes' `GET /api/stats` `auth_failures` |
| Certificate expiry | sensor, timestamp | a board's `GET /api/tls/status` `not_after_epoch` |
| Certificate renewal | sensor, enum | a board's `GET /api/acme` `state` (`idle`, `running`, `ok`, `failed`) |
| Last certificate renewal (diagnostic) | sensor, timestamp | `GET /api/acme` `last_success` |
| Raft | binary sensor, problem | `raft.on` and (`raft.leader` null or `raft.stuck`) |
| Shared settings (diagnostic) | binary sensor, problem | a live member's `config_state` is `out-of-sync` or `diverged` |
| Certificate | binary sensor, problem | `expired`, or ACME `state == "failed"`, or `name_missing` |
| Load balancing (diagnostic) | binary sensor | `balance.enabled` |

The certificate is shared, but only boards read it and run ACME, so it is
asked of a board (the leader first), through the relay when needed, every
ten minutes.

### Each node's device

Model: "ESP32-P4 board" (with the chip as model ID and its silicon revision
as hardware version) or "Linux node"; software version and MAC from
`GET /api/device`.

| Entity | Type | Source |
|---|---|---|
| Alive | binary sensor, connectivity | the member list's `state` (stays available while the node itself does not answer) |
| Leader | binary sensor | the member list's `leader` |
| Draining (diagnostic) | binary sensor | the member list's `draining` |
| Member state (diagnostic) | sensor, enum | `alive`, `suspect`, `dead`, `left` |
| Connections | sensor | `GET /api/stats` `connections` |
| Sessions | sensor | `sessions` |
| Subscriptions (diagnostic) | sensor | `subscriptions` |
| Retained messages | sensor | `retained` |
| Messages received / sent | sensor, msg/s | `messages_in` / `messages_out`, per second between two readings |
| Data received / sent | sensor, data rate | `bytes_in` / `bytes_out`, per second |
| Failed logins | sensor, total increasing | `auth_failures` |
| Dropped messages (diagnostic) | sensor, total increasing | the five `dropped_*` counters |
| Uptime (diagnostic) | sensor, timestamp | the broker's start: now − `uptime_s` |
| Version (diagnostic) | sensor | `GET /api/device` `version` |
| Internal memory free (diagnostic, boards) | sensor, data size | `memory_hot_free` |
| PSRAM free (diagnostic, boards) | sensor, data size | `memory_bulk_free` |
| Raft store errors (diagnostic) | sensor, total increasing | the node's own `GET /api/cluster` `raft.counters.store_errors` |
| Redirecting new clients (diagnostic) | binary sensor | the node's own `balance.redirecting` |
| Crash dump (diagnostic, boards) | binary sensor, problem | `GET /api/device` `coredump.stored` |
| Link to *member* (diagnostic) | binary sensor, connectivity, one per other member | the node's own `links[]`: a link to that member in state `up` |

Rates restart (unknown for one reading) when a node restarts and its
counters start again from zero.

### Maintenance (0.2.0, MQTT Core 0.30.0)

- **Firmware** (an update entity per board): the version the board runs and
  the release its update server offers, with the release notes. Install
  asks the board to fetch that release itself, check its signature and
  restart into it; the entity shows the progress. Only the offered release
  can be installed. Linux nodes have none: the Home Assistant app is
  updated by the Supervisor, a container by its image.
- **Restart** (a button per board) and **Renew certificate** (on the
  cluster device: the board that orders certificates orders one now).
- **Repairs:** a board that rolled an update back (it did not confirm the
  new version), and a token with less than the integration needs (made by
  0.1, or on a cluster before 0.30.0): fixing it signs in again, which
  makes a new token.

### Actions (0.2.0)

Each names the cluster's config entry. `mqttcore.add_user`,
`mqttcore.set_password` and `mqttcore.remove_user` change the cluster's
users; `mqttcore.get_acl` returns the ACL in force and `mqttcore.set_acl`
replaces it; `mqttcore.list_clients` returns every node's clients and
`mqttcore.kick_client` disconnects one from whichever node has it. A
refusal comes back in MQTT Core's own words.

### Listeners (options)

**Configure** on the integration asks for the OTA password, then shows each
server on or off and its port. The change is the cluster's (shared
settings), used by each node from its next start. The password is used for
that one change and kept nowhere: no token reaches these settings.

### Diagnostics

Download diagnostics from the integration's page. Tokens, the token's
name, host names, URLs, addresses, the MAC, certificate names, DNS records
and zones, the ACME account, and (should a later phase read them) client
IDs, user names and ACL text are redacted; any known name or address left
inside other text is replaced too.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

The tests use `pytest-homeassistant` (Home Assistant's own test package,
which replaced `pytest-homeassistant-custom-component` after 2026.10),
pinned to Home Assistant 2026.11.2. The canned answers in `tests/fixtures`
follow the firmware's JSON writers (`admin_cluster.c`, `admin_raft.c`,
`admin_routes.c`, `broker/inspect.c`) and a real node's answers, with
documentation names and addresses.

`tests/test_host_binary.py` runs against the real broker: the MQTT Core host
binary, started on loopback by the test. It is skipped unless
`MQTTCORE_BIN` names the binary:

```sh
cmake -S <mqttcore>/esp32-p4-firmware/host -B /tmp/mqttcore-host -G Ninja \
      -DMQTTCORE_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
ninja -C /tmp/mqttcore-host mqttcore
MQTTCORE_BIN=/tmp/mqttcore-host/mqttcore .venv/bin/pytest tests/test_host_binary.py
# also the three-node cluster (waits for a leader, about 90 s):
MQTTCORE_CLUSTER_TEST=1 MQTTCORE_BIN=... .venv/bin/pytest tests/test_host_binary.py
```

The client for the admin API is in `custom_components/mqttcore/pymqttcore/`.
It has no Home Assistant imports: it is meant to become the `pymqttcore`
library on PyPI unchanged, at which point the manifest lists it in
`requirements`.

CI (`.github/workflows`): hassfest and the HACS action (the Marketplace's
checks) in `validate.yml`; ruff, mypy and pytest, on the pinned release and
on the newest beta, in `tests.yml`. The HACS action also wants the GitHub
repository to have a description and topics.

## What comes next

Phases 2 to 4 are here; phase 1 is MQTT Core's own (its Home Assistant
app announces `mqttcore` discovery with `host`, `port`, `ssl` and
`server_name`, which the `hassio` step takes). From the integration plan
(`docs/hassio-integration-plan.md` in the MQTT Core repository):

- **Still open from phase 3:** repairs for a failing renewal and a member
  gone (the binary sensors show both today).
- **Still open from phase 4:** the balancer and drain.
- **Phase 5:** a public repository and a listing in the Marketplace catalog.

## License

There is no license yet: choosing one is the owner's decision. Apache-2.0
is the recommendation (it keeps the way into Home Assistant core open, and
the Marketplace catalog needs an OSI license GitHub can identify). Until a
`LICENSE` file is added, all rights are reserved.

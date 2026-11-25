# Changelog

What changed in each release of the MQTT Core integration, newest first.

Each release has a section headed `## <version> — <date>`, where the version
is the one in `custom_components/mqttcore/manifest.json`. When a release
reaches `main`, the Release workflow publishes its section as the GitHub
release's notes, and the Marketplace shows them before an update. Changes
that are not released yet go under `## Unreleased`. CONTRIBUTING.md has the
steps.

Versions up to 0.2.3 were installed from the repository before any release
was published. They are listed here for the record.

## Unreleased

- The cluster's Raft retained set shows how full it is ("Retained set
  used", a share of its ceiling), and a "Retained set" problem sensor and a
  repair say when it reaches 90 % (MQTT Core 0.34.0 says so itself; for an
  older cluster the figures decide). At the ceiling a new retained message
  stays on the node it was published to.
- The repair for a token with too few rights explains itself in its fix
  dialog only, as Home Assistant's checks require.

## 0.2.3 — 2026-11-22

- One member refusing the token no longer makes Home Assistant ask you to
  sign in again. The cluster is read through another member, the refusing
  one is asked last for a while, and the log says once which member
  refuses and why. Home Assistant asks you to sign in again only when every
  member that answered refused the token, or two did.

## 0.2.2 — 2026-11-21

- Members are reached even when Home Assistant's DNS does not know their
  names. Each node is read at its address, then through another node,
  and the certificate is still checked against the name. The certificate
  and its renewal are read again on the next refresh, not ten minutes
  later.

## 0.2.1 — 2026-11-21

- MQTT Core's icon and logo.

## 0.2.0 — 2026-11-21

- An update entity per board: the running version, the release its update
  server offers with its notes, and an install with progress (MQTT Core
  0.30.0 or later). Only the offered release can be installed.
- Buttons: restart a board; renew the cluster's certificate.
- Repairs: a board that rolled an update back; a token with fewer rights
  than the integration needs, fixed by signing in again.
- Actions: `add_user`, `set_password`, `remove_user`, `get_acl`, `set_acl`,
  `list_clients` and `kick_client`.
- Options: the cluster's listeners. Changing them asks for the OTA
  password each time.
- The token now has read rights on status and system, and write rights on
  clients, access and maintenance. Setup waits until every live member
  accepts it.

## 0.1.1 — 2026-11-20

- The integration is called MQTT Core. The domain and the entity IDs are
  unchanged.

## 0.1.0 — 2026-11-20

- Monitoring of an MQTT Core cluster over its HTTPS admin API:
  - a cluster device: members alive, leader, Raft term and problems,
    shared settings, connections, message rates, retained messages, failed
    logins, and the certificate's expiry and renewal;
  - a device per node: alive, leader, draining, connections, sessions,
    subscriptions, message and data rates, dropped messages, uptime,
    version, memory on the boards, and a link sensor per other member.
- Setup: sign in once with the OTA password, and Home Assistant makes a
  read-only token, kept without the password. Clusters announced over
  zeroconf are discovered. If the token stops working, Home Assistant asks
  you to sign in again.
- Members that join or leave are added and removed on their own.
- Diagnostics with tokens, names and addresses removed.

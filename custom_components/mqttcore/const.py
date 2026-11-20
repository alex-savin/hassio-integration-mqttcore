"""Constants for the mqttcore integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "mqttcore"
LOGGER = logging.getLogger(__package__)

MANUFACTURER: Final = "mqttcore"
DEFAULT_PORT: Final = 443

# Config entry data (the OTA password is never among them).
CONF_TOKEN_NAME: Final = "token_name"
CONF_KNOWN_URLS: Final = "known_urls"

# Flow-only fields.
CONF_OTA_PASSWORD: Final = "ota_password"
CONF_RESTRICT_NETWORK: Final = "restrict_network"
SECTION_CONNECTION: Final = "connection"

# The token the flow mints: read-only, status and system (the certificate
# and the ACME state live in the system area). Phase 3 (updates, restart)
# needs system write, which mqttcore caps at 30 days today.
TOKEN_NAME_PREFIX: Final = "home-assistant"
TOKEN_SCOPES: Final[dict[str, str]] = {"status": "read", "system": "read"}
TOKEN_NETWORKS_MAX: Final = 4

# A token minted through a follower reaches that follower with the shared
# settings, about a second later: the flow retries the first call.
TOKEN_VERIFY_ATTEMPTS: Final = 15
TOKEN_VERIFY_DELAY: Final = 1.0

CLUSTER_SCAN_INTERVAL: Final = timedelta(seconds=30)
NODE_SCAN_INTERVAL: Final = timedelta(seconds=30)
DEVICE_REFRESH_INTERVAL: Final = timedelta(minutes=10)
CERTIFICATE_REFRESH_INTERVAL: Final = timedelta(minutes=10)

# A node URL that failed is tried last for this long.
ENDPOINT_BACKOFF: Final = timedelta(minutes=5)

# A member that left, or that the cluster no longer lists, keeps its device
# this long before the integration removes it: a restart is not a departure.
NODE_REMOVE_AFTER: Final = timedelta(minutes=30)

# The node key of a node that runs no cluster (it has no node ID).
LOCAL_NODE: Final = "local"

# Model names (GET /api/device says the platform, not the role).
MODEL_BOARD: Final = "ESP32-P4 board"
MODEL_HOST: Final = "Linux node"
MODEL_CLUSTER: Final = "Cluster"

"""An async client for one mqttcore node's admin API.

The client talks to one node, by its base URL. It can reach any other
member of that node's cluster through the node's relay
(`GET /api/peer?node=...&path=...`) by passing `node_id`. It authenticates
with an API token (`Authorization: Bearer mqc_...`) or, for the few calls
that need it, the OTA password (`X-OTA-Password`), which it never keeps.

The caller owns the aiohttp session: the client neither creates nor closes
one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any, Final

import aiohttp
from yarl import URL

from .exceptions import (
    MqttcoreAuthenticationError,
    MqttcoreBadRequestError,
    MqttcoreConflictError,
    MqttcoreConnectionError,
    MqttcoreForbiddenError,
    MqttcoreHostNotAllowedError,
    MqttcoreInvalidResponseError,
    MqttcoreNotFoundError,
    MqttcoreNotSupportedError,
    MqttcoreRateLimitedError,
    MqttcoreResponseError,
    MqttcoreTlsError,
    MqttcoreUnavailableError,
)
from .models import (
    AcmeStatus,
    AuthStatus,
    ClusterStatus,
    Device,
    ServerConfig,
    Stats,
    TlsStatus,
    TokenInfo,
)

DEFAULT_TIMEOUT: Final = 10.0

# Areas a token may be granted, and the access each may have.
TOKEN_AREAS: Final = ("status", "clients", "access", "system")
TOKEN_ACCESS: Final = ("none", "read", "write")


def build_url(host: str, port: int | None = None, *, ssl: bool = True) -> str:
    """The base URL of a node's admin API, such as `https://mqtt01.example.com/`."""
    scheme = "https" if ssl else "http"
    name = host.strip().rstrip("/")
    if ":" in name and not name.startswith("["):
        name = f"[{name}]"  # an IPv6 address
    if port is None or port == (443 if ssl else 80):
        return f"{scheme}://{name}/"
    return f"{scheme}://{name}:{port}/"


def _error_message(body: Any, text: str) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])
    return text.strip()[:200] or "no message"


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _response_error(
    status: int, body: Any, text: str, path: str, headers: Mapping[str, str]
) -> MqttcoreResponseError:
    message = _error_message(body, text)
    if status == 429:
        return MqttcoreRateLimitedError(
            status, message, path=path, retry_after=_retry_after(headers)
        )
    by_status: dict[int, type[MqttcoreResponseError]] = {
        400: MqttcoreBadRequestError,
        401: MqttcoreAuthenticationError,
        403: MqttcoreForbiddenError,
        404: MqttcoreNotFoundError,
        409: MqttcoreConflictError,
        421: MqttcoreHostNotAllowedError,
        501: MqttcoreNotSupportedError,
        502: MqttcoreUnavailableError,
        503: MqttcoreUnavailableError,
        504: MqttcoreUnavailableError,
    }
    return by_status.get(status, MqttcoreResponseError)(status, message, path=path)


class MqttcoreClient:
    """One node's admin API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        token: str | None = None,
        verify_ssl: bool = True,
        request_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Talk to the node at `url` (its base URL) over `session`."""
        self._session = session
        self._base = URL(url)
        self._token = token
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

    @property
    def url(self) -> str:
        """The node's base URL."""
        return str(self._base)

    @property
    def host(self) -> str:
        """The host part of the base URL."""
        return self._base.host or ""

    # ---- transport ----------------------------------------------------------------------

    def _target(self, path: str, node_id: str | None) -> URL:
        if node_id is None:
            return self._base.with_path(path)
        return self._base.with_path("/api/peer").with_query(
            {"node": node_id, "path": path}
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        node_id: str | None = None,
        body: Mapping[str, Any] | None = None,
        ota_password: str | None = None,
        use_token: bool = True,
    ) -> Any:
        """Call a route and return its JSON answer.

        `node_id` relays the call to that member of the node's cluster.
        Raises a `MqttcoreResponseError` subclass for an error status,
        `MqttcoreConnectionError` when the node cannot be reached, and
        `MqttcoreInvalidResponseError` for an answer that is not JSON.
        """
        headers = {"Accept": "application/json"}
        if use_token and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if ota_password is not None:
            headers["X-OTA-Password"] = ota_password
        data: str | None = None
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(dict(body or {}))
        target = self._target(path, node_id)
        try:
            async with self._session.request(
                method,
                target,
                headers=headers,
                data=data,
                timeout=self._timeout,
                allow_redirects=False,
                ssl=self._verify_ssl,
            ) as response:
                status = response.status
                response_headers = dict(response.headers)
                text = await response.text(errors="replace")
        except aiohttp.ClientConnectorCertificateError as err:
            raise MqttcoreTlsError(
                f"{self.host}: certificate not trusted: {err}"
            ) from err
        except aiohttp.ClientSSLError as err:
            raise MqttcoreTlsError(f"{self.host}: TLS failed: {err}") from err
        except TimeoutError as err:
            raise MqttcoreConnectionError(f"{self.host}: no answer in time") from err
        except aiohttp.ClientError as err:
            raise MqttcoreConnectionError(f"{self.host}: {err}") from err

        try:
            decoded: Any = json.loads(text) if text else None
        except ValueError:
            decoded = None
        if 200 <= status < 300:
            if not isinstance(decoded, (dict, list)):
                raise MqttcoreInvalidResponseError(
                    f"{path}: answer {status} is not JSON: is this an mqttcore node?"
                )
            return decoded
        raise _response_error(status, decoded, text, path, response_headers)

    async def _get_object(
        self,
        path: str,
        *,
        node_id: str | None = None,
        ota_password: str | None = None,
        use_token: bool = True,
    ) -> dict[str, Any]:
        answer = await self.request(
            "GET", path, node_id=node_id, ota_password=ota_password, use_token=use_token
        )
        if not isinstance(answer, dict):
            raise MqttcoreInvalidResponseError(f"{path}: expected a JSON object")
        return answer

    # ---- open routes --------------------------------------------------------------------

    async def get_auth_status(self) -> AuthStatus:
        """`GET /api/auth/status`: an open route; sends no credentials."""
        return AuthStatus.from_dict(
            await self._get_object("/api/auth/status", use_token=False)
        )

    # ---- status area (token: status read) -----------------------------------------------

    async def get_config(self, *, ota_password: str | None = None) -> ServerConfig:
        """`GET /api/config`: the settings that identify the node."""
        use_token = ota_password is None
        return ServerConfig.from_dict(
            await self._get_object(
                "/api/config", ota_password=ota_password, use_token=use_token
            )
        )

    async def get_cluster(
        self, *, node_id: str | None = None, ota_password: str | None = None
    ) -> ClusterStatus:
        """`GET /api/cluster`: the cluster as this node (or `node_id`) sees it."""
        use_token = ota_password is None
        return ClusterStatus.from_dict(
            await self._get_object(
                "/api/cluster",
                node_id=node_id,
                ota_password=ota_password,
                use_token=use_token,
            )
        )

    async def get_stats(self, *, node_id: str | None = None) -> Stats:
        """`GET /api/stats`: the broker's counters since it started."""
        return Stats.from_dict(await self._get_object("/api/stats", node_id=node_id))

    async def get_device(
        self, *, node_id: str | None = None, ota_password: str | None = None
    ) -> Device:
        """`GET /api/device`: what the node is and runs."""
        use_token = ota_password is None
        return Device.from_dict(
            await self._get_object(
                "/api/device",
                node_id=node_id,
                ota_password=ota_password,
                use_token=use_token,
            )
        )

    # ---- system area (token: system read) -----------------------------------------------

    async def get_tls_status(self, *, node_id: str | None = None) -> TlsStatus:
        """`GET /api/tls/status`: the certificate (its dates on a board only)."""
        return TlsStatus.from_dict(
            await self._get_object("/api/tls/status", node_id=node_id)
        )

    async def get_acme(self, *, node_id: str | None = None) -> AcmeStatus:
        """`GET /api/acme`: the renewal; `MqttcoreNotSupportedError` on the host."""
        return AcmeStatus.from_dict(
            await self._get_object("/api/acme", node_id=node_id)
        )

    # ---- API tokens (admin area: the OTA password, never a token) -----------------------

    async def list_tokens(self, *, ota_password: str) -> list[TokenInfo]:
        """`GET /api/agents`: the tokens' names, scopes and limits."""
        answer = await self._get_object(
            "/api/agents", ota_password=ota_password, use_token=False
        )
        tokens = answer.get("tokens")
        if not isinstance(tokens, list):
            return []
        return [TokenInfo.from_dict(t) for t in tokens if isinstance(t, dict)]

    async def create_token(
        self,
        *,
        ota_password: str,
        name: str,
        scopes: Mapping[str, str],
        networks: Iterable[str] = (),
        expires_at: int = 0,
    ) -> str:
        """`POST /api/agents` `create`: mint a token and return it (shown once)."""
        body: dict[str, Any] = {
            "action": "create",
            "name": name,
            "scopes": dict(scopes),
        }
        nets = list(networks)
        if nets:
            body["networks"] = nets
        if expires_at:
            body["expires_at"] = expires_at
        answer = await self.request(
            "POST", "/api/agents", body=body, ota_password=ota_password, use_token=False
        )
        token = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token.startswith("mqc_"):
            raise MqttcoreInvalidResponseError("/api/agents: no token in the answer")
        return token

    async def revoke_token(self, *, ota_password: str, name: str) -> None:
        """`POST /api/agents` `revoke`: the token fails from the next request."""
        await self.request(
            "POST",
            "/api/agents",
            body={"action": "revoke", "name": name},
            ota_password=ota_password,
            use_token=False,
        )

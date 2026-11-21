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
    OtaState,
    ServerConfig,
    Stats,
    TlsStatus,
    TokenInfo,
    UpdateCheck,
)

DEFAULT_TIMEOUT: Final = 10.0

# Areas a token may be granted, and the access each may have.
TOKEN_AREAS: Final = ("status", "clients", "access", "system", "maintenance")
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
        address: str | None = None,
    ) -> None:
        """Talk to the node at `url` (its base URL) over `session`.

        `address` connects to that IP address instead of what DNS answers
        for the URL's name (a name the resolver does not know). The node's
        certificate is still checked against the name, and the Host header
        still names it, so the node's host check sees the name it serves.
        """
        self._session = session
        self._base = URL(url)
        self._token = token
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._address = address or None

    @property
    def url(self) -> str:
        """The node's base URL."""
        return str(self._base)

    @property
    def host(self) -> str:
        """The host part of the base URL."""
        return self._base.host or ""

    @property
    def address(self) -> str | None:
        """The IP address connected to instead of the name's, if any."""
        return self._address

    @property
    def _where(self) -> str:
        """The node, for messages: its name, and the address when one is used."""
        return self.host if self._address is None else f"{self.host} ({self._address})"

    def _host_header(self) -> str:
        """The base URL's host and, when not the scheme's default, its port."""
        if self._base.is_default_port():
            return self.host
        return f"{self.host}:{self._base.port}"

    def _at_address(
        self, target: URL, headers: dict[str, str]
    ) -> tuple[URL, dict[str, Any]]:
        """`target` at the address, when one is set, and the request's extras.

        The Host header goes into `headers`; the extras name the server for
        TLS (SNI and the certificate's check).
        """
        if self._address is None or not self.host:
            return target, {}
        headers["Host"] = self._host_header()
        extra: dict[str, Any] = {}
        if self._base.scheme == "https":
            extra["server_hostname"] = self.host
        return target.with_host(self._address), extra

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
        text: str | None = None,
    ) -> Any:
        """Call a route and return its JSON answer.

        `text` sends a plain-text body instead of JSON (the ACL's upload).

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
        if text is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            data = text
        elif method.upper() == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(dict(body or {}))
        target, extra = self._at_address(self._target(path, node_id), headers)
        try:
            async with self._session.request(
                method,
                target,
                headers=headers,
                data=data,
                timeout=self._timeout,
                allow_redirects=False,
                ssl=self._verify_ssl,
                **extra,
            ) as response:
                status = response.status
                response_headers = dict(response.headers)
                text = await response.text(errors="replace")
        except aiohttp.ClientConnectorCertificateError as err:
            raise MqttcoreTlsError(
                f"{self._where}: certificate not trusted: {err}"
            ) from err
        except aiohttp.ClientSSLError as err:
            raise MqttcoreTlsError(f"{self._where}: TLS failed: {err}") from err
        except TimeoutError as err:
            raise MqttcoreConnectionError(f"{self._where}: no answer in time") from err
        except aiohttp.ClientError as err:
            raise MqttcoreConnectionError(f"{self._where}: {err}") from err

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

    async def set_config(
        self, changes: Mapping[str, Any], *, ota_password: str
    ) -> dict[str, Any]:
        """`POST /api/config`: change settings (the OTA password; no token reaches it)."""
        answer = await self.request(
            "POST",
            "/api/config",
            body=changes,
            ota_password=ota_password,
            use_token=False,
        )
        return answer if isinstance(answer, dict) else {}

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

    async def get_token_self(self, *, node_id: str | None = None) -> TokenInfo:
        """`GET /api/agents/self` (0.30.0): the token this client sends, and its scopes."""
        return TokenInfo.from_dict(
            await self._get_object("/api/agents/self", node_id=node_id)
        )

    # ---- maintenance (0.30.0: the `maintenance` area) -----------------------------------

    async def get_ota(self, *, node_id: str | None = None) -> OtaState:
        """`GET /api/ota`: the board's image state and any install's progress."""
        return OtaState.from_dict(await self._get_object("/api/ota", node_id=node_id))

    async def check_update(self, *, node_id: str | None = None) -> UpdateCheck:
        """`POST /api/update/check`: what the update server offers this board."""
        answer = await self.request(
            "POST", "/api/update/check", node_id=node_id, body={}
        )
        return UpdateCheck.from_dict(answer)

    async def install_update(self, version: str, *, node_id: str | None = None) -> None:
        """`POST /api/update/install`: install the release the check offered (202)."""
        await self.request(
            "POST", "/api/update/install", node_id=node_id, body={"version": version}
        )

    async def restart(self, *, node_id: str | None = None) -> None:
        """`POST /api/restart`: the board restarts about 1.5 s after the answer."""
        await self.request("POST", "/api/restart", node_id=node_id, body={})

    async def issue_certificate(self, *, node_id: str | None = None) -> None:
        """`POST /api/acme/issue`: order a certificate now (the leader only)."""
        await self.request("POST", "/api/acme/issue", node_id=node_id, body={})

    async def get_release_notes(self, url: str) -> str:
        """The release notes (Markdown) the update server keeps; no credentials go."""
        try:
            async with self._session.get(
                url, timeout=self._timeout, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise MqttcoreConnectionError(f"{url}: HTTP {response.status}")
                return await response.text(errors="replace")
        except TimeoutError as err:
            raise MqttcoreConnectionError(f"{url}: no answer in time") from err
        except aiohttp.ClientError as err:
            raise MqttcoreConnectionError(f"{url}: {err}") from err

    # ---- users, the ACL and clients (the `access` and `clients` areas) ------------------

    async def get_users(self) -> dict[str, Any]:
        """`GET /api/access/users`: the user names, and whether anonymous clients may connect."""
        return await self._get_object("/api/access/users")

    async def change_user(
        self, action: str, name: str, password: str | None = None
    ) -> dict[str, Any]:
        """`POST /api/access/users`: `add`, `set_password` or `remove` a user."""
        body: dict[str, Any] = {"action": action, "name": name}
        if password is not None:
            body["password"] = password
        answer = await self.request("POST", "/api/access/users", body=body)
        return answer if isinstance(answer, dict) else {}

    async def get_acl(self) -> dict[str, Any]:
        """`GET /api/access/acl`: the ACL in force, as text and as rules."""
        return await self._get_object("/api/access/acl")

    async def set_acl(self, text: str) -> dict[str, Any]:
        """`POST /api/access/acl`: replace the whole ACL with this text."""
        answer = await self.request("POST", "/api/access/acl", text=text)
        return answer if isinstance(answer, dict) else {}

    async def get_clients(self, *, node_id: str | None = None) -> list[dict[str, Any]]:
        """`GET /api/clients`: the clients connected to the node."""
        answer = await self.request("GET", "/api/clients", node_id=node_id)
        return (
            [c for c in answer if isinstance(c, dict)]
            if isinstance(answer, list)
            else []
        )

    async def kick_client(self, client_id: str, *, node_id: str | None = None) -> None:
        """`POST /api/clients/kick`: disconnect the client (MQTT 5: reason 0x98)."""
        await self.request(
            "POST", "/api/clients/kick", node_id=node_id, body={"client_id": client_id}
        )

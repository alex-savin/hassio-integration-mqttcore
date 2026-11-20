"""Errors raised by the mqttcore admin API client.

Programs act on the class (and so on the HTTP status), never on the
message: mqttcore's error sentences are meant for people.
"""

from __future__ import annotations


class MqttcoreError(Exception):
    """Base class for every error this client raises."""


class MqttcoreConnectionError(MqttcoreError):
    """The node could not be reached, or did not answer in time."""


class MqttcoreTlsError(MqttcoreConnectionError):
    """The node's certificate did not verify (wrong name, untrusted, expired)."""


class MqttcoreInvalidResponseError(MqttcoreError):
    """The answer was not the JSON the route promises: probably not mqttcore."""


class MqttcoreResponseError(MqttcoreError):
    """The node answered with an error status.

    `status` is the HTTP status, `message` the node's own sentence (the
    `error` member of its JSON answer) and `path` the route that was asked.
    """

    def __init__(self, status: int, message: str, *, path: str) -> None:
        """Keep the status, the node's message and the route."""
        super().__init__(f"{path}: {status} {message}")
        self.status = status
        self.message = message
        self.path = path


class MqttcoreBadRequestError(MqttcoreResponseError):
    """400: the request was refused as malformed, or a rule refused it."""


class MqttcoreAuthenticationError(MqttcoreResponseError):
    """401: no credentials, wrong ones, or an unknown or expired API token."""


class MqttcoreForbiddenError(MqttcoreResponseError):
    """403: the gate refused (token scope or network, OTA password needed, plain HTTP)."""


class MqttcoreNotFoundError(MqttcoreResponseError):
    """404: no such route, or no such member for the relay."""


class MqttcoreConflictError(MqttcoreResponseError):
    """409: conflict with the node's state (already set up, a follower, ...)."""


class MqttcoreHostNotAllowedError(MqttcoreResponseError):
    """421: the Host header is not a name this node answers to."""


class MqttcoreRateLimitedError(MqttcoreResponseError):
    """429: too many wrong passwords from this address."""

    def __init__(
        self, status: int, message: str, *, path: str, retry_after: int | None
    ) -> None:
        """Keep the seconds the node asked to wait, when it said."""
        super().__init__(status, message, path=path)
        self.retry_after = retry_after


class MqttcoreNotSupportedError(MqttcoreResponseError):
    """501: this build or node cannot do it (the host binary and ACME, say)."""


class MqttcoreUnavailableError(MqttcoreResponseError):
    """502, 503 or 504: the node is stopping, out of memory, or a relay failed."""

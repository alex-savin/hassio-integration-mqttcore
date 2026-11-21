"""A board's firmware: its state, the update server's offer, and installing it.

One coordinator per board (mqttcore 0.30.0, the `maintenance` area):

- `GET /api/ota` every minute: the running version, an install's progress,
  and `rolled_back`, an update the board rejected, which raises a repair;
- `POST /api/update/check` every six hours, and again after the version
  changed: what the update server offers, and its release notes' link;
- `POST /api/update/install` when asked: the board fetches the release from
  its own update server, checks the signature, and restarts into it. While
  it does, the state is read every 5 s, and the board's restart is waited
  out rather than reported as a failure.

Linux nodes have none: the host binary answers these routes 501, and the
Home Assistant app is updated by the Supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    LOGGER,
    UPDATE_CHECK_INTERVAL,
    UPDATE_PROGRESS_INTERVAL,
    UPDATE_SCAN_INTERVAL,
)
from .coordinator import MqttcoreNodeCoordinator, _now
from .pymqttcore import MqttcoreError, MqttcoreForbiddenError, OtaState, UpdateCheck

if TYPE_CHECKING:
    from . import MqttcoreConfigEntry


def rollback_issue_id(entry: MqttcoreConfigEntry, node_id: str) -> str:
    """The repair raised while a board reports a rolled-back update."""
    return f"rolled_back_{entry.entry_id}_{node_id}"


@dataclass(slots=True)
class FirmwareData:
    """What a firmware coordinator knows after a refresh."""

    ota: OtaState
    check: UpdateCheck | None
    check_error: str | None
    installing: str | None


class MqttcoreFirmwareCoordinator(DataUpdateCoordinator[FirmwareData]):
    """Follows one board's firmware, through its node coordinator's routes."""

    config_entry: MqttcoreConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MqttcoreConfigEntry,
        node: MqttcoreNodeCoordinator,
    ) -> None:
        """Follow the firmware of the board `node` reads."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} firmware {node.node_id}",
            update_interval=UPDATE_SCAN_INTERVAL,
        )
        self.node = node
        self._check: UpdateCheck | None = None
        self._check_error: str | None = None
        self._check_at: float | None = None
        self._installing: str | None = None

    @property
    def node_name(self) -> str:
        """The board's name, for messages."""
        member = self.node.member
        if member is None:
            return self.node.node_id
        return member.host_name or member.name or self.node.node_id

    async def _async_update_data(self) -> FirmwareData:
        """Read the image state, and the update server's offer when it is due."""
        try:
            ota = await self.node.async_call(lambda c, n: c.get_ota(node_id=n))
        except MqttcoreForbiddenError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="token_cannot_read_firmware",
                translation_placeholders={"error": err.message},
            ) from err
        except MqttcoreError as err:
            if self._installing is not None and self.data is not None:
                # Restarting into the new image: wait for it.
                LOGGER.debug("%s restarting: %s", self.node_name, err)
                return self.data
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="node_unreachable",
                translation_placeholders={"error": str(err)},
            ) from err
        self._follow_install(ota)
        if self._check_due(ota):
            await self._async_check()
        self._async_rollback_issue(ota)
        busy = self._installing is not None or ota.update.running
        self.update_interval = (
            UPDATE_PROGRESS_INTERVAL if busy else UPDATE_SCAN_INTERVAL
        )
        return FirmwareData(
            ota=ota,
            check=self._check,
            check_error=self._check_error,
            installing=self._installing,
        )

    def _follow_install(self, ota: OtaState) -> None:
        """An install ends when the board runs the version, or reports a failure."""
        if self._installing is None:
            return
        if ota.version == self._installing:
            LOGGER.info("%s now runs %s", self.node_name, ota.version)
            self._installing = None
            self._check_at = None  # ask the update server again
        elif ota.update.state == "failed" and not ota.update.running:
            LOGGER.warning(
                "%s did not install %s: %s",
                self.node_name,
                self._installing,
                ota.update.message,
            )
            self._installing = None

    def _check_due(self, ota: OtaState) -> bool:
        if self._check_at is None or self._check is None:
            return True
        if self._check.current_version != ota.version:
            return True
        return _now() - self._check_at >= UPDATE_CHECK_INTERVAL.total_seconds()

    async def _async_check(self) -> None:
        """Ask the board what its update server offers; keep the last answer on failure."""
        self._check_at = _now()
        try:
            self._check = await self.node.async_call(
                lambda c, n: c.check_update(node_id=n)
            )
        except MqttcoreError as err:
            LOGGER.debug("%s: the update check failed: %s", self.node_name, err)
            self._check_error = str(err)
            return
        self._check_error = self._check.error

    def _async_rollback_issue(self, ota: OtaState) -> None:
        issue_id = rollback_issue_id(self.config_entry, self.node.node_id)
        if ota.rolled_back_version is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="update_rolled_back",
            translation_placeholders={
                "node": self.node_name,
                "version": ota.rolled_back_version,
                "running": ota.version,
            },
        )

    async def async_install(self, version: str) -> None:
        """Have the board install `version`, the one its update server offers."""
        await self.node.async_call(lambda c, n: c.install_update(version, node_id=n))
        self._installing = version
        self.update_interval = UPDATE_PROGRESS_INTERVAL
        await self.async_request_refresh()

    async def async_release_notes(self) -> str | None:
        """The offered release's notes (Markdown), from the update server."""
        url = self._check.release_notes_url if self._check is not None else None
        client = self.node.cluster.active_client
        if url is None or client is None:
            return None
        try:
            return await client.get_release_notes(url)
        except MqttcoreError as err:
            LOGGER.debug("Release notes from %s: %s", url, err)
            return None

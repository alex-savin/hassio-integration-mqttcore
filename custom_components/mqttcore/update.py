"""Each board's firmware as an update entity (mqttcore 0.30.0, `maintenance`).

The board installs only what its own update server offers, signed with the
key the running image trusts; Home Assistant asks for it and shows the
progress the board reports. The Home Assistant app's node is updated by the
Supervisor, and a Linux node by its container: neither has one.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MqttcoreConfigEntry
from .const import DOMAIN
from .coordinator import MqttcoreNodeCoordinator
from .devices import node_device_info
from .entity import async_track_boards, node_unique_id
from .firmware import FirmwareData, MqttcoreFirmwareCoordinator
from .pymqttcore import MqttcoreError, MqttcoreForbiddenError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MqttcoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a firmware update entity for each board, as boards appear."""

    @callback
    def _add(nodes: list[MqttcoreNodeCoordinator]) -> None:
        entities = []
        for node in nodes:
            firmware = MqttcoreFirmwareCoordinator(hass, entry, node)
            entry.async_create_background_task(
                hass, firmware.async_refresh(), f"{DOMAIN} firmware {node.node_id}"
            )
            entities.append(MqttcoreFirmwareUpdate(firmware))
        async_add_entities(entities)

    async_track_boards(entry, _add)


class MqttcoreFirmwareUpdate(
    CoordinatorEntity[MqttcoreFirmwareCoordinator], UpdateEntity
):
    """A board's firmware, from the cluster's update server."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, coordinator: MqttcoreFirmwareCoordinator) -> None:
        """Attach the entity to the board's device."""
        super().__init__(coordinator)
        node = coordinator.node
        entry = coordinator.config_entry
        self._attr_unique_id = node_unique_id(entry, node.node_id, "firmware")
        device = node.data.device if node.data is not None else None
        self._attr_device_info = node_device_info(
            entry,
            node.node_id,
            node.member,
            device,
            entry.runtime_data.cluster_device_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Available once the board's firmware state was read."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        return super().available and data is not None

    @property
    @override
    def installed_version(self) -> str | None:
        """The version the board runs."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        return data.ota.version or None if data is not None else None

    @property
    @override
    def latest_version(self) -> str | None:
        """The release the update server offers, else the running one."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        if data is None:
            return None
        check = data.check
        if check is not None and check.update_available and check.latest_version:
            return check.latest_version
        return self.installed_version

    @property
    @override
    def in_progress(self) -> bool:
        """An install is under way (it ends with the board's restart)."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        return data is not None and (
            data.installing is not None or data.ota.update.running
        )

    @property
    @override
    def update_percentage(self) -> int | None:
        """How much of the image the board has written."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        if data is None or not self.in_progress:
            return None
        return data.ota.update.percentage

    @property
    @override
    def release_summary(self) -> str | None:
        """Why there is nothing to install, when the update server says so."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        if data is None:
            return None
        if data.check_error:
            return data.check_error[:255]
        if data.check is not None and data.check.verdict == "bad":
            return data.check.why[:255]
        return None

    @override
    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Have the board install the release its update server offers."""
        data: FirmwareData | None = (
            self.coordinator.data
        )  # None before the first refresh
        offered = (
            data.check.latest_version
            if data is not None
            and data.check is not None
            and data.check.update_available
            else None
        )
        target = version or offered
        if target is None or target != offered:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_not_offered",
                translation_placeholders={"version": str(target)},
            )
        try:
            await self.coordinator.async_install(target)
        except MqttcoreForbiddenError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="token_cannot_maintain",
                translation_placeholders={"error": err.message},
            ) from err
        except MqttcoreError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="install_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.async_write_ha_state()

    @override
    async def async_release_notes(self) -> str | None:
        """The offered release's notes, from the update server."""
        return await self.coordinator.async_release_notes()

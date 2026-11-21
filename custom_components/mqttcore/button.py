"""Buttons: restart a board, and renew the cluster's certificate (`maintenance`)."""

from __future__ import annotations

from typing import override

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MqttcoreConfigEntry
from .const import DOMAIN
from .coordinator import MqttcoreClusterCoordinator, MqttcoreNodeCoordinator
from .entity import MqttcoreClusterEntity, MqttcoreNodeEntity, async_track_boards
from .pymqttcore import MqttcoreError, MqttcoreForbiddenError, MqttcoreResponseError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MqttcoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the certificate button, and a restart button for each board."""
    async_add_entities([MqttcoreRenewCertificateButton(entry.runtime_data.cluster)])

    @callback
    def _add(nodes: list[MqttcoreNodeCoordinator]) -> None:
        async_add_entities(MqttcoreRestartButton(node) for node in nodes)

    async_track_boards(entry, _add)


def _maintenance_error(err: MqttcoreError) -> HomeAssistantError:
    """The error to show for a refused or failed maintenance call."""
    if isinstance(err, MqttcoreForbiddenError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="token_cannot_maintain",
            translation_placeholders={"error": err.message},
        )
    message = err.message if isinstance(err, MqttcoreResponseError) else str(err)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="maintenance_failed",
        translation_placeholders={"error": message},
    )


class MqttcoreRestartButton(MqttcoreNodeEntity, ButtonEntity):
    """Restart a board (it closes its connections and syncs its store first)."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, node: MqttcoreNodeCoordinator) -> None:
        """Attach the button to the board's device."""
        super().__init__(node, "restart")

    @override
    async def async_press(self) -> None:
        """Ask the board to restart."""
        try:
            await self.coordinator.async_call(lambda c, n: c.restart(node_id=n))
        except MqttcoreError as err:
            raise _maintenance_error(err) from err


class MqttcoreRenewCertificateButton(MqttcoreClusterEntity, ButtonEntity):
    """Order the cluster's certificate now, rather than at the renewal schedule."""

    _attr_translation_key = "renew_certificate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MqttcoreClusterCoordinator) -> None:
        """Attach the button to the cluster device."""
        super().__init__(coordinator, "renew_certificate")

    @property
    @override
    def available(self) -> bool:
        """Available where a board renews the certificate through ACME."""
        data = self.coordinator.data
        return super().available and data is not None and data.acme is not None

    @override
    async def async_press(self) -> None:
        """Ask the board that orders certificates to order one now."""
        try:
            await self.coordinator.async_issue_certificate()
        except MqttcoreError as err:
            raise _maintenance_error(err) from err

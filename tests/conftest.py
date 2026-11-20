"""Fixtures for the mqttcore tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.mqttcore.const import DOMAIN
from homeassistant.core import HomeAssistant

from .common import ENTRY_DATA, UNIQUE_ID, Clock, mock_cluster


@pytest.fixture
def clock() -> Generator[Clock]:
    """Replace the coordinators' monotonic clock with one the test moves."""
    fake = Clock()
    with patch("custom_components.mqttcore.coordinator._now", fake):
        yield fake


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """An entry for the example cluster, set up through mqtt01."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=UNIQUE_ID,
        unique_id=UNIQUE_ID,
        data=dict(ENTRY_DATA),
        version=1,
        minor_version=1,
    )


@pytest.fixture
def mock_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Every node answers."""
    mock_cluster(aioclient_mock)
    return aioclient_mock


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> MockConfigEntry:
    """The example cluster, set up."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep the flows from setting the entry up."""
    with patch(
        "custom_components.mqttcore.async_setup_entry", return_value=None
    ) as mock:
        yield mock


@pytest.fixture
def mock_instance_id() -> Generator[None]:
    """A fixed Home Assistant instance ID, so the token's name is known."""
    with patch(
        "homeassistant.helpers.instance_id.async_get",
        return_value="0123abcd-4567-89ef-0123-456789abcdef",
    ):
        yield


@pytest.fixture
def no_token_delay() -> Generator[None]:
    """No waiting between tries of a new token."""
    with (
        patch("custom_components.mqttcore.config_flow.TOKEN_VERIFY_DELAY", 0),
        patch("custom_components.mqttcore.config_flow.TOKEN_VERIFY_ATTEMPTS", 3),
    ):
        yield

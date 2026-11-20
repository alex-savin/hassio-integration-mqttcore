"""The shipped translations match the source strings and cover every key."""

from __future__ import annotations

import json
from pathlib import Path
import re

from custom_components.mqttcore import binary_sensor, sensor

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "mqttcore"


def _load(name: str) -> dict:
    return json.loads((INTEGRATION / name).read_text())


def test_en_matches_strings() -> None:
    """Custom integrations ship translations/en.json; strings.json is its source."""
    assert _load("strings.json") == _load("translations/en.json")


def test_no_key_references() -> None:
    """A custom integration's translations must be resolved text."""
    assert "[%key:" not in (INTEGRATION / "translations" / "en.json").read_text()


def test_entity_names() -> None:
    """Every entity description has a name in the strings."""
    strings = _load("strings.json")["entity"]
    for platform, descriptions in (
        (
            "sensor",
            [
                *sensor.CLUSTER_SENSORS,
                *sensor.TOTAL_SENSORS,
                *sensor.MEMBER_SENSORS,
                *sensor.NODE_SENSORS,
            ],
        ),
        (
            "binary_sensor",
            [
                *binary_sensor.CLUSTER_BINARY_SENSORS,
                *binary_sensor.MEMBER_BINARY_SENSORS,
                *binary_sensor.NODE_BINARY_SENSORS,
            ],
        ),
    ):
        for description in descriptions:
            assert description.translation_key in strings[platform], description.key
            for option in getattr(description, "options", None) or []:
                assert option in strings[platform][description.translation_key]["state"]
    assert "link" in strings["binary_sensor"]


def test_flow_errors_have_text() -> None:
    """Every error and abort code the flow uses has its sentence."""
    source = (INTEGRATION / "config_flow.py").read_text()
    config = _load("strings.json")["config"]
    codes = set(re.findall(r'return "([a-z_]+)"', source))
    codes |= set(re.findall(r'FlowError\("([a-z_]+)"', source))
    codes |= set(re.findall(r'errors\[[^]]+\] = "([a-z_]+)"', source))
    assert codes, "the patterns found nothing"
    for code in codes - {"mqttcore"}:
        assert code in config["error"], code
    for reason in re.findall(r'reason="([a-z_]+)"', source):
        assert reason in config["abort"], reason

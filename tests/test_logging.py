"""The logging redaction processor strips registered secret values from events."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hivepilot.services import config_provenance
from hivepilot.utils import logging as hp_logging


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def test_processor_redacts_registered_string_fields() -> None:
    marker = "REDACT-ME-LONG-MARKER-123"
    config_provenance.register_secret_value(marker)
    event = {"event": "runner.env", "API_KEY": marker, "note": f"x {marker} y"}
    out = hp_logging._redact_secret_values(None, "info", event)
    assert config_provenance.REDACTED == out["API_KEY"]
    assert marker not in out["note"]
    assert config_provenance.REDACTED in out["note"]


def test_processor_leaves_non_strings_and_clean_strings() -> None:
    event = {"event": "x", "count": 3, "flag": True, "text": "nothing secret"}
    out = hp_logging._redact_secret_values(None, "info", event)
    assert out["count"] == 3
    assert out["flag"] is True
    assert out["text"] == "nothing secret"


def test_processor_redacts_secret_nested_inside_dict_field() -> None:
    """A secret embedded inside a nested dict/list kwarg (not a top-level
    string) must still be redacted — proves the processor recurses."""
    marker = "NESTED-MARKER-abc123-LONG-ENOUGH"
    config_provenance.register_secret_value(marker)
    event = {
        "event": "runner.env",
        "payload": {"env": {"API_KEY": marker}, "extra": [marker, "clean"]},
    }
    out = hp_logging._redact_secret_values(None, "info", event)
    assert out["payload"]["env"]["API_KEY"] == config_provenance.REDACTED
    assert out["payload"]["extra"] == [config_provenance.REDACTED, "clean"]

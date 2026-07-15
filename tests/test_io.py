"""hivepilot.utils.io — write_summary redacts registered secret values before
writing the run summary artifact to disk."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hivepilot.config import settings
from hivepilot.services import config_provenance
from hivepilot.utils.io import write_summary


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def test_write_summary_redacts_nested_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "output_format", "json", raising=False)
    marker = "IO-MARKER-XYZ-do-not-leak"
    config_provenance.register_secret_value(marker)
    summary = {
        "task": "t",
        "results": [{"project": "p", "detail": f"echoed {marker}"}],
    }
    write_summary(tmp_path, summary)
    written = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert marker not in written
    assert config_provenance.REDACTED in written


def test_write_summary_plain_content_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "output_format", "json", raising=False)
    summary = {"task": "t", "results": []}
    write_summary(tmp_path, summary)
    written = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert '"task": "t"' in written

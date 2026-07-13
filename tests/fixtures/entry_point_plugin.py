"""Fixture plugin module used by tests/test_plugin_loading_mechanisms.py.

Not a test module itself (no `test_` prefix) — pytest ignores it during
collection. It exists purely so the entry-point loading test can exercise a
real, importable `register()` callable via a monkeypatched
`importlib.metadata.entry_points()`, without needing to `pip install` a
second package.
"""

from __future__ import annotations

from typing import Any


class FixtureRunner:
    """Minimal fake runner satisfying the BaseRunner Protocol shape."""

    def __init__(self, definition: Any, settings: Any) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload: Any) -> None:
        return None


def register() -> dict[str, Any]:
    return {"runners": {"fixture-kind": FixtureRunner}}

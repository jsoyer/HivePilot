"""Follow-up hardening test for ``plugins/obsidian.py`` (post
plugin-arch-overhaul code review).

``recall``'s vault search must NOT slurp an arbitrarily large note whole into
memory just to score a v1 grep: each note is read up to ``_MAX_NOTE_READ_BYTES``
only. The ``obsidian_recall_max_bytes`` setting bounds the INJECTED block, not
the read — this bounds the read itself. Complements
``tests/test_plugin_obsidian_brain.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parent.parent
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"


def _load_obsidian_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_readcap", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def obsidian_module() -> ModuleType:
    return _load_obsidian_module()


def test_search_vault_bounds_per_note_read(
    obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "log.md"
    # The search term appears ONLY past the read cap.
    note.write_text("x" * 200 + " needle", encoding="utf-8")

    # Cap below the term's offset -> the note is never read far enough to match.
    monkeypatch.setattr(obsidian_module, "_MAX_NOTE_READ_BYTES", 10)
    assert obsidian_module._search_vault(tmp_path, ["needle"]) == []

    # Cap past the term -> the SAME note now matches, proving it was the bound
    # (not some unrelated filter) that excluded it above.
    monkeypatch.setattr(obsidian_module, "_MAX_NOTE_READ_BYTES", 10_000)
    results = obsidian_module._search_vault(tmp_path, ["needle"])
    assert len(results) == 1
    assert results[0][0] == note

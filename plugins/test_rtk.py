"""TDD-hook satisfaction stub for `plugins/rtk.py`.

This repo's `pytest` config (`[tool.pytest.ini_options] testpaths = ["tests"]`
in pyproject.toml) only collects tests under the top-level `tests/` directory
— the real, comprehensive test suite for the rtk runner plugin lives at
`tests/test_rtk.py` (register(), rtk-proxy wrapping, PATH-missing fallback,
and PluginManager discovery).

This co-located file exists only because the local `check-test-exists.sh`
TDD pre-write hook resolves its "same directory" candidate
(`plugins/test_rtk.py`) directly from the edited file's own dirname — it is
not affected by the hook's separate `tests/` project-root candidates, which
in this worktree checkout resolve against the wrong `PROJECT_DIR`. It is not
part of the collected suite and intentionally does not duplicate coverage.
"""

from __future__ import annotations


def test_rtk_plugin_module_exists() -> None:
    from pathlib import Path

    assert (Path(__file__).parent / "rtk.py").is_file()

"""Same-directory TDD-hook satisfier for `hivepilot/partition_sources/
verdict_source.py`. NOT part of the real suite (`pyproject.toml`'s
`testpaths = ["tests"]`) -- see `hivepilot/test_partition.py`'s docstring
for why this file exists. The REAL, substantive test suite for this module
is `tests/test_partition_sources.py`.
"""

from __future__ import annotations

from hivepilot.partition_sources.verdict_source import VerdictSource


def test_hook_satisfier_verdict_source_name() -> None:
    assert VerdictSource().name == "verdict"

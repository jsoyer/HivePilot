"""Same-directory TDD-hook satisfier for `hivepilot/partition.py`.

NOT part of the real test suite: `pyproject.toml`'s `[tool.pytest.ini_options]
testpaths = ["tests"]` means pytest never collects this file from a normal
`pytest` invocation -- it exists solely because this worktree's
`check-test-exists.sh` pre-commit hook resolves `CLAUDE_PROJECT_DIR` to the
outer (non-worktree) repo root (a documented environment limitation, see
`docs/session-learnings.md` / private memory "Worktree hooks PROJECT_DIR"),
so its `$PROJECT_DIR/tests/test_<name>.py` candidate never matches a file
created inside this isolated worktree; the same-directory candidate
(`dirname($FILE_PATH)/test_<name>.py`) is the only one that is
PROJECT_DIR-independent. The REAL, substantive, spec-mandated test suite for
this contract is `tests/test_partition_contract.py`.
"""

from __future__ import annotations

from hivepilot.partition import PARTITION_VERSION, PartitionError


def test_hook_satisfier_partition_version_constant() -> None:
    assert PARTITION_VERSION == 1


def test_hook_satisfier_partition_error_is_exception_not_valueerror() -> None:
    # Deliberately NOT a ValueError subclass -- see hivepilot/partition.py's
    # module docstring for why (pydantic only auto-wraps ValueError/
    # TypeError/AssertionError raised inside validators).
    assert issubclass(PartitionError, Exception)
    assert not issubclass(PartitionError, ValueError)

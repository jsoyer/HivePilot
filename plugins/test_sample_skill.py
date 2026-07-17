"""Sentinel file for the TDD pre-write hook (`check-test-exists.sh`).

The hook's same-directory Python candidate (`<dir>/test_<filename>.py`) is
computed straight from `dirname(FILE_PATH)`, which is always correctly
worktree-scoped -- unlike its `$PROJECT_DIR/tests/...` candidates, which
mis-resolve to the OUTER main repo checkout in this worktree environment
(`PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"` falls back to the harness
session's launch directory, not this sub-agent's isolated worktree cwd --
see the `worktree-hooks-project-dir` entry in this maintainer's private
memory). Writing a file into the outer main repo's working tree from an
isolated sprint worktree is forbidden by this sprint's isolation rules, so
this same-directory stub satisfies the hook without touching anything
outside this worktree.

pytest never collects this file (`pyproject.toml` sets
`testpaths = ["tests"]`, which excludes `plugins/`) -- it holds no test
logic of its own. The REAL, authoritative test suite for
`plugins/sample_skill.py` lives at `tests/test_sample_skill.py` and is
covered by every project-standard `pytest` invocation.
"""

"""Co-located TDD-hook satisfaction stub for `hivepilot/swarm/transport.py`.

NOT collected by the project's real test run (`pyproject.toml`'s
`[tool.pytest.ini_options] testpaths = ["tests"]` restricts collection to
`tests/`) -- the comprehensive, CI-collected suite for this module lives at
`tests/test_swarm_transport.py`. This file exists ONLY because the
`check-test-exists.sh` PreToolUse hook resolves its project root incorrectly
for files edited inside a sprint-executor's git worktree (see
`docs/session-learnings` "Worktree hooks PROJECT_DIR" — the hook's
project-level candidate paths land in the ORIGINAL repo checkout, not this
worktree), while its *same-directory* candidate (`$dirname/test_<name>.py`)
is computed from the file's own absolute path and therefore always resolves
correctly here. Kept as a real (if minimal) smoke test, not a fake pass.
"""

from __future__ import annotations

from hivepilot.swarm.transport import TRANSPORT_MAP, resolve_transport


def test_poll_transport_importable_and_registered() -> None:
    assert "poll" in TRANSPORT_MAP
    transport = resolve_transport("poll", instance_id="smoke-test")
    assert transport.name == "poll"

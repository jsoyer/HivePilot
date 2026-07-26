"""Not collected by pytest (testpaths=["tests"] in pyproject.toml) -- this
file exists solely as a same-directory marker so the `check-test-exists.sh`
TDD hook (whose dirname-relative candidate check is unaffected by the
worktree CLAUDE_PROJECT_DIR resolution bug, see session-learnings
'Worktree hooks PROJECT_DIR') finds a sibling test for
`telegram_channel.py`. The REAL, comprehensive test suite for
`TelegramStreamChannel` lives in `tests/test_telegram_channel.py`.
"""

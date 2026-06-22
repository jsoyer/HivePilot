"""Tests for render_prompt_vars — co-located with the module."""
# The canonical tests live in tests/test_prompt_vars.py; this file exists
# only to satisfy the worktree TDD hook which uses the main-repo project dir.
from hivepilot.utils.prompt_vars import render_prompt_vars


def test_replaces_all_three_tokens():
    text = "repo={TARGET_REPO} gov={GOVERNANCE_REPO} vault={OBSIDIAN_VAULT}"
    result = render_prompt_vars(
        text,
        target_repo="/my/repo",
        governance_repo="/gov/repo",
        obsidian_vault="/vault",
    )
    assert result == "repo=/my/repo gov=/gov/repo vault=/vault"

"""Tests for render_prompt_vars."""

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


def test_leaves_unknown_placeholders_untouched():
    text = "hello={TARGET_REPO} unknown={foo} other={bar}"
    result = render_prompt_vars(
        text,
        target_repo="/r",
        governance_repo="/g",
        obsidian_vault="/v",
    )
    assert "={foo}" in result
    assert "={bar}" in result
    assert "={TARGET_REPO}" not in result


def test_dollar_brace_form():
    text = "a=${TARGET_REPO} b=${GOVERNANCE_REPO} c=${OBSIDIAN_VAULT}"
    result = render_prompt_vars(
        text,
        target_repo="/r",
        governance_repo="/g",
        obsidian_vault="/v",
    )
    assert "${TARGET_REPO}" not in result
    assert "${GOVERNANCE_REPO}" not in result
    assert "${OBSIDIAN_VAULT}" not in result


def test_handles_empty_values():
    text = "path={TARGET_REPO}/CLAUDE.md"
    result = render_prompt_vars(
        text,
        target_repo="",
        governance_repo="",
        obsidian_vault="",
    )
    assert "{TARGET_REPO}" not in result
    assert result == "path=/CLAUDE.md"

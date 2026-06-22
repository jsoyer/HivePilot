"""Runtime prompt variable substitution."""

from __future__ import annotations


def render_prompt_vars(
    text: str,
    *,
    target_repo: str,
    governance_repo: str,
    obsidian_vault: str,
) -> str:
    """Replace {TARGET_REPO}, {GOVERNANCE_REPO}, {OBSIDIAN_VAULT} (and ${VAR} forms)
    in *text*. Any other {placeholder} is left untouched."""
    replacements = {
        "TARGET_REPO": target_repo,
        "GOVERNANCE_REPO": governance_repo,
        "OBSIDIAN_VAULT": obsidian_vault,
    }
    for var, value in replacements.items():
        text = text.replace(f"{{{var}}}", value)
        text = text.replace(f"${{{var}}}", value)
    return text

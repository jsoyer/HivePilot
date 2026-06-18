"""
Agent rules registry for HivePilot V4.

Each role is mapped to the canonical rule source files it MUST read before acting.
Rule files are referenced BY PATH only — content is never copied here.
This prevents drift: the canonical source is always the authoritative version.

Design:
- Unknown role → KeyError (mirrors roles.get_role behaviour).
- CROSS_CUTTING_RULES: enforced statements that every role inherits.
  These are short natural-language policy statements, NOT file paths.
- ROLE_RULES: role-name → ordered list of absolute file paths to read.
  Per-repo CLAUDE.md (e.g. noxys-api, noxys-agent, noxys-extension) is loaded
  on demand at runtime when an agent works in that repo; it is NOT baked in here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical source roots (absolute, never drift)
# ---------------------------------------------------------------------------

_NOXYS_ROOT = "/home/jeromesoyer/Documents/Github/noxys"
_VAULT_SECURITY = "/home/jeromesoyer/Documents/Github/jsoyer/obsidian-vault/Noxys/08 - Security"

# ---------------------------------------------------------------------------
# Noxys monorepo root rule files (all 6)
# ---------------------------------------------------------------------------

NOXYS_CLAUDE_MD = f"{_NOXYS_ROOT}/CLAUDE.md"
NOXYS_AGENTS_MD = f"{_NOXYS_ROOT}/AGENTS.md"
NOXYS_CURSORRULES = f"{_NOXYS_ROOT}/.cursorrules"
NOXYS_WINDSURFRULES = f"{_NOXYS_ROOT}/.windsurfrules"
NOXYS_GEMINI_MD = f"{_NOXYS_ROOT}/GEMINI.md"
NOXYS_AGENT_GOVERNANCE = f"{_NOXYS_ROOT}/AGENT-GOVERNANCE.md"

# ---------------------------------------------------------------------------
# Vault canonical security / git rules
# ---------------------------------------------------------------------------

VAULT_DETECTION_FABRIC = f"{_VAULT_SECURITY}/AGENT-DETECTION-FABRIC.md"
VAULT_GIT_BRANCH_RULES = f"{_VAULT_SECURITY}/AGENT-GIT-BRANCH-RULES.md"

# ---------------------------------------------------------------------------
# Cross-cutting enforced rules (policy statements, not file paths)
# ---------------------------------------------------------------------------
# Every role inherits these.  Kept as short, machine-searchable statements so
# that callers can scan for specific markers (e.g. "English", "detection-fabric").
# ---------------------------------------------------------------------------

CROSS_CUTTING_RULES: list[str] = [
    "All artifacts must be written in English (no other language).",
    "Use code-review-graph MCP before Grep/Glob/Read for code navigation.",
    "detection-fabric is mandatory: run AGENT-DETECTION-FABRIC checks before any write.",
    "European-sovereign-first: prefer EU-hosted infrastructure and EU-governed data.",
    "Privacy-by-design: never log or surface raw prompt content.",
]

# ---------------------------------------------------------------------------
# Per-role rule source paths
# ---------------------------------------------------------------------------
# Order matters: roles read governance first, then security, then repo-specific rules.
# ---------------------------------------------------------------------------

_STRATEGY_ROLES_PATHS: list[str] = [
    NOXYS_CLAUDE_MD,
    NOXYS_AGENTS_MD,
    NOXYS_AGENT_GOVERNANCE,
    NOXYS_CURSORRULES,
    NOXYS_WINDSURFRULES,
    NOXYS_GEMINI_MD,
]

_CODING_ROLES_PATHS: list[str] = [
    NOXYS_CLAUDE_MD,
    NOXYS_AGENTS_MD,
    NOXYS_AGENT_GOVERNANCE,
    NOXYS_CURSORRULES,
    NOXYS_WINDSURFRULES,
    NOXYS_GEMINI_MD,
    VAULT_GIT_BRANCH_RULES,
]

_AUTOMATION_ROLES_PATHS: list[str] = [
    NOXYS_CLAUDE_MD,
    NOXYS_AGENTS_MD,
    NOXYS_AGENT_GOVERNANCE,
    NOXYS_CURSORRULES,
    NOXYS_WINDSURFRULES,
    NOXYS_GEMINI_MD,
]

ROLE_RULES: dict[str, list[str]] = {
    # --- strategy tier (opus) -----------------------------------------------
    "ceo": [
        *_STRATEGY_ROLES_PATHS,
        *CROSS_CUTTING_RULES,
    ],
    "cto": [
        *_STRATEGY_ROLES_PATHS,
        VAULT_GIT_BRANCH_RULES,
        *CROSS_CUTTING_RULES,
    ],
    "ciso": [
        *_STRATEGY_ROLES_PATHS,
        VAULT_DETECTION_FABRIC,
        VAULT_GIT_BRANCH_RULES,
        *CROSS_CUTTING_RULES,
    ],
    # --- coding tier (sonnet) -----------------------------------------------
    "developer": [
        *_CODING_ROLES_PATHS,
        VAULT_DETECTION_FABRIC,
        *CROSS_CUTTING_RULES,
    ],
    "reviewer": [
        *_CODING_ROLES_PATHS,
        VAULT_DETECTION_FABRIC,
        *CROSS_CUTTING_RULES,
    ],
    "qa": [
        *_CODING_ROLES_PATHS,
        VAULT_DETECTION_FABRIC,
        *CROSS_CUTTING_RULES,
    ],
    # --- automation tier (haiku) --------------------------------------------
    "chief_of_staff": [
        *_AUTOMATION_ROLES_PATHS,
        *CROSS_CUTTING_RULES,
    ],
    "documentation": [
        *_AUTOMATION_ROLES_PATHS,
        VAULT_DETECTION_FABRIC,
        *CROSS_CUTTING_RULES,
    ],
}


def get_rules_for_role(role_name: str) -> list[str]:
    """Return the ordered rule source paths/statements for *role_name*.

    Raises:
        KeyError: if *role_name* is not a registered role (mirrors roles.get_role).
    """
    return ROLE_RULES[role_name]

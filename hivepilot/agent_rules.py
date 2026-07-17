"""
Agent rules registry for HivePilot V4.

Each role is mapped to the canonical rule source files it MUST read before acting.
Rule files are referenced BY PATH only — content is never copied here.
This prevents drift: the canonical source is always the authoritative version.

Design:
- Unknown role → CROSS_CUTTING_RULES floor, never an empty list (fail-safe;
  Sprint 2 of the roles-model-effort-config-owned PRD made this lookup safe
  for a role that isn't loaded, e.g. a business role absent under the
  reduced generic-only defaults, without dropping the enforced policy
  minimum every known role already inherits).
- CROSS_CUTTING_RULES: enforced statements that every role inherits.
  These are short natural-language policy statements, NOT file paths.
- ROLE_RULES: role-name → ordered list of absolute file paths to read.
  Per-repo CLAUDE.md (e.g. your-service-a, your-service-b, your-service-c) is loaded
  on demand at runtime when an agent works in that repo; it is NOT baked in here.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings

# ---------------------------------------------------------------------------
# Config-derived source roots (never hardcoded)
# ---------------------------------------------------------------------------
# _GOVERNANCE_ROOT is kept as a module-level variable for backward-compat: runners
# import it directly as a string.  Derive from settings so any deployment can
# override via HIVEPILOT_GOVERNANCE_REPO without touching this file.
# ---------------------------------------------------------------------------

_GOVERNANCE_ROOT: str = settings.governance_repo or ""
_VAULT_SECURITY: str = (
    str(Path(str(settings.obsidian_vault)) / "08 - Security")
    if settings.obsidian_vault and Path(str(settings.obsidian_vault)).is_absolute()
    else ""
)

# ---------------------------------------------------------------------------
# Config-derived helper functions
# ---------------------------------------------------------------------------


def governance_file_paths() -> list[str]:
    """Return absolute paths to governance files, derived from settings.

    Returns empty list if settings.governance_repo is None/empty.
    """
    if not settings.governance_repo:
        return []
    return [str(Path(settings.governance_repo) / fname) for fname in settings.governance_files]


def vault_security_path() -> str | None:
    """Return the security vault directory path from settings.obsidian_vault.

    Returns None if obsidian_vault is not configured to an absolute path.
    """
    vault = settings.obsidian_vault
    if vault and Path(str(vault)).is_absolute():
        return str(Path(str(vault)) / "08 - Security")
    return None


# ---------------------------------------------------------------------------
# Governance rule file paths (derived from _GOVERNANCE_ROOT at import time)
# ---------------------------------------------------------------------------

GOVERNANCE_CLAUDE_MD = f"{_GOVERNANCE_ROOT}/CLAUDE.md" if _GOVERNANCE_ROOT else ""
GOVERNANCE_AGENTS_MD = f"{_GOVERNANCE_ROOT}/AGENTS.md" if _GOVERNANCE_ROOT else ""
GOVERNANCE_CURSORRULES = f"{_GOVERNANCE_ROOT}/.cursorrules" if _GOVERNANCE_ROOT else ""
GOVERNANCE_WINDSURFRULES = f"{_GOVERNANCE_ROOT}/.windsurfrules" if _GOVERNANCE_ROOT else ""
GOVERNANCE_GEMINI_MD = f"{_GOVERNANCE_ROOT}/GEMINI.md" if _GOVERNANCE_ROOT else ""
GOVERNANCE_AGENT_GOVERNANCE = f"{_GOVERNANCE_ROOT}/AGENT-GOVERNANCE.md" if _GOVERNANCE_ROOT else ""

# ---------------------------------------------------------------------------
# Vault canonical security / git rules
# ---------------------------------------------------------------------------

VAULT_DETECTION_FABRIC = f"{_VAULT_SECURITY}/AGENT-DETECTION-FABRIC.md" if _VAULT_SECURITY else ""
VAULT_GIT_BRANCH_RULES = f"{_VAULT_SECURITY}/AGENT-GIT-BRANCH-RULES.md" if _VAULT_SECURITY else ""

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
# Empty strings are filtered out at role-rules build time to avoid injecting blank paths.
# ---------------------------------------------------------------------------

_STRATEGY_ROLES_PATHS: list[str] = [
    p
    for p in [
        GOVERNANCE_CLAUDE_MD,
        GOVERNANCE_AGENTS_MD,
        GOVERNANCE_AGENT_GOVERNANCE,
        GOVERNANCE_CURSORRULES,
        GOVERNANCE_WINDSURFRULES,
        GOVERNANCE_GEMINI_MD,
    ]
    if p
]

_CODING_ROLES_PATHS: list[str] = [
    p
    for p in [
        GOVERNANCE_CLAUDE_MD,
        GOVERNANCE_AGENTS_MD,
        GOVERNANCE_AGENT_GOVERNANCE,
        GOVERNANCE_CURSORRULES,
        GOVERNANCE_WINDSURFRULES,
        GOVERNANCE_GEMINI_MD,
        VAULT_GIT_BRANCH_RULES,
    ]
    if p
]

_AUTOMATION_ROLES_PATHS: list[str] = [
    p
    for p in [
        GOVERNANCE_CLAUDE_MD,
        GOVERNANCE_AGENTS_MD,
        GOVERNANCE_AGENT_GOVERNANCE,
        GOVERNANCE_CURSORRULES,
        GOVERNANCE_WINDSURFRULES,
        GOVERNANCE_GEMINI_MD,
    ]
    if p
]

ROLE_RULES: dict[str, list[str]] = {
    # --- strategy tier (opus) -----------------------------------------------
    "ceo": [
        *_STRATEGY_ROLES_PATHS,
        *CROSS_CUTTING_RULES,
    ],
    "cto": [
        *_STRATEGY_ROLES_PATHS,
        *([VAULT_GIT_BRANCH_RULES] if VAULT_GIT_BRANCH_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    "ciso": [
        *_STRATEGY_ROLES_PATHS,
        *([VAULT_DETECTION_FABRIC] if VAULT_DETECTION_FABRIC else []),
        *([VAULT_GIT_BRANCH_RULES] if VAULT_GIT_BRANCH_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    # --- coding tier (sonnet) -----------------------------------------------
    "developer": [
        *_CODING_ROLES_PATHS,
        *([VAULT_DETECTION_FABRIC] if VAULT_DETECTION_FABRIC else []),
        *CROSS_CUTTING_RULES,
    ],
    "reviewer": [
        *_CODING_ROLES_PATHS,
        *([VAULT_DETECTION_FABRIC] if VAULT_DETECTION_FABRIC else []),
        *CROSS_CUTTING_RULES,
    ],
    "qa": [
        *_CODING_ROLES_PATHS,
        *([VAULT_DETECTION_FABRIC] if VAULT_DETECTION_FABRIC else []),
        *CROSS_CUTTING_RULES,
    ],
    # --- automation tier (haiku) --------------------------------------------
    "chief_of_staff": [
        *_AUTOMATION_ROLES_PATHS,
        *CROSS_CUTTING_RULES,
    ],
    "documentation": [
        *_AUTOMATION_ROLES_PATHS,
        *([VAULT_DETECTION_FABRIC] if VAULT_DETECTION_FABRIC else []),
        *CROSS_CUTTING_RULES,
    ],
}


def get_rules_for_role(role_name: str) -> list[str]:
    """Return the ordered rule source paths/statements for *role_name*.

    Fail-safe lookup (roles-model-effort-config-owned PRD, Sprint 2): a role
    absent from ``ROLE_RULES`` (e.g. a business role like "ceo" that isn't
    loaded in a deployment relying on the reduced generic-only defaults)
    returns the ``CROSS_CUTTING_RULES`` floor instead of raising
    ``KeyError``. This is fail-safe, not fail-open: every known role already
    inherits this enforced policy minimum (English-only artifacts,
    code-review-graph-before-Grep, detection-fabric, EU-sovereign-first,
    privacy-by-design/no-raw-prompt-logging), so an unknown role must
    inherit it too rather than fall through with zero policy coverage. A
    fresh ``list(...)`` copy is returned so callers cannot
    mutate the module-level constant. Callers that want to assert a role is
    genuinely known should check ``hivepilot.roles.ROLES`` directly; this
    function's job is only to hand back a rule manifest for a role, never to
    crash the caller and never to return an empty, policy-free list.
    """
    return ROLE_RULES.get(role_name, list(CROSS_CUTTING_RULES))

"""
Agent rules registry for HivePilot V4.

Each role is mapped to the canonical rule source files it MUST read before acting.
Rule files are referenced BY PATH only — content is never copied here.
This prevents drift: the canonical source is always the authoritative version.

Design:
- Unknown role → CROSS_CUTTING_RULES floor (fail-safe; Sprint 2 of the
  roles-model-effort-config-owned PRD made this lookup safe for a role that
  isn't loaded, e.g. a business role absent under the reduced generic-only
  defaults, without dropping the enforced policy minimum every known role
  already inherits). The floor is whatever the DEPLOYMENT resolved — see
  ``load_cross_cutting_rules`` for the absent-vs-explicitly-empty semantics.
- CROSS_CUTTING_RULES: enforced statements that every role inherits.
  These are short natural-language policy statements, NOT file paths.
  CONFIG-OWNED (``cross_cutting_rules:`` in roles.yaml), exactly like the
  source roots below — HivePilot is a generic orchestrator, so the engine
  default carries no organisation-, jurisdiction-, language- or
  tooling-specific statement.
- ROLE_RULES: role-name → ordered list of absolute file paths to read.
  Per-repo CLAUDE.md (e.g. your-service-a, your-service-b, your-service-c) is loaded
  on demand at runtime when an agent works in that repo; it is NOT baked in here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from hivepilot.config import settings

log = logging.getLogger(__name__)

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
# that callers can scan for specific markers.
#
# CONFIG-OWNED (this module's whole point: "config-derived, never hardcoded").
# The paths above were already derived from settings; the rule STATEMENTS were
# not. Five statements used to be baked in here, four of them one specific
# customer's policy. HivePilot is a generic orchestrator for ANY project and
# ANY organisation, so a deployment in another jurisdiction, working in
# another language, running a different toolchain, was still getting all four
# injected into every agent prompt with no way to decline. They now live where
# every other piece of role policy already lives — `roles.yaml`.
#
# The removed statements are quoted in full in the PR that made this change,
# not reproduced here: this is a PUBLIC repository and those strings are the
# customer's, which is the entire point of moving them out.
# ---------------------------------------------------------------------------

#: Top-level key read from ``roles.yaml`` (see ``load_cross_cutting_rules``).
CROSS_CUTTING_RULES_KEY = "cross_cutting_rules"

#: What the engine ships when a deployment configures nothing.
#:
#: Deliberately ONE statement. The five originals were judged individually,
#: not as a block — four dropped, one kept:
#:
#: - A mandate that all artifacts be written in one specific human language.
#:   DROPPED — a language mandate is an organisation's choice, not an
#:   orchestrator's; a team working in another language must not have to opt
#:   OUT of it.
#: - An instruction to prefer a named third-party MCP code-navigation server
#:   over the agent's own file-search tools. DROPPED — HivePilot has no
#:   mechanism to provision, configure or even detect that server (the rule
#:   was the sole occurrence of "MCP" anywhere in this package). Telling every
#:   agent to use a tool that is not there is an instruction the agent cannot
#:   follow: wasted prompt tokens at best, a hallucinated tool call at worst.
#:   A deployment that genuinely runs such a server can declare the rule; the
#:   engine must not assume it.
#: - A mandate to run one customer's internal pre-write security control.
#:   DROPPED — names that organisation's internal system and the private
#:   document describing it.
#: - A regional-sovereignty preference for infrastructure and data. DROPPED —
#:   a jurisdiction stance: correct for the organisation that wrote it,
#:   meaningless-to-wrong for a deployment governed elsewhere.
#: - "Privacy-by-design: never log or surface raw prompt content." KEPT — the
#:   only one of the five about the ENGINE's own output hygiene rather than an
#:   organisation's policy. It names no jurisdiction, language, product or
#:   vendor tool, and it asks the agent to behave the way HivePilot already
#:   behaves in code (prompt/secret masking at the DB and notification sinks),
#:   so it is an instruction the agent CAN follow and one that keeps agent
#:   output consistent with engine-enforced behaviour. Still overridable: an
#:   organisation whose compliance regime demands full prompt retention
#:   declares its own list and this statement is gone.
ENGINE_DEFAULT_CROSS_CUTTING_RULES: tuple[str, ...] = (
    "Privacy-by-design: never log or surface raw prompt content.",
)


def load_cross_cutting_rules() -> list[str]:
    """Resolve the cross-cutting rule statements from configuration.

    Read from the ``cross_cutting_rules:`` top-level key of ``roles.yaml``,
    resolved through the standard config chain
    (``settings.resolve_config_path``: ``$XDG_CONFIG_HOME/hivepilot/`` →
    ``config_repo/`` → ``base_dir/``). ``roles.yaml`` is the right owner
    because it is already where role policy lives (the
    roles-model-effort-config-owned PRD moved runner/model/effort there), and
    these statements are precisely "the policy every role inherits" — keeping
    them beside the roles they apply to means a config repo cannot ship a
    roster and its inherited rules out of sync in two separate files.
    ``hivepilot.roles`` only ever reads ``raw["roles"]``, so this extra
    top-level key is invisible to it (and to ``validate_config``).

    ABSENT vs EXPLICITLY EMPTY — deliberately NOT the same thing:

    - **Absent** (no ``roles.yaml``, no ``cross_cutting_rules`` key, or the
      key present with a bare/``null`` value) → ``ENGINE_DEFAULT_CROSS_CUTTING_RULES``.
      Absence means "this deployment has not expressed an opinion", and the
      one failure this repository keeps re-shipping is a gate reading an
      absent value as "no constraint" and failing OPEN. A missing file must
      never silently delete a policy floor an operator believed was there.
      A bare ``cross_cutting_rules:`` with nothing under it parses as ``None``
      and is far more likely a half-finished edit than a considered decision,
      so it is treated as absent too — "no rules at all" has an unambiguous
      spelling, and it is the next bullet.
    - **Explicitly empty** (``cross_cutting_rules: []``) → ``[]``, genuinely no
      rules. This is NOT the empty-value fail-open pattern: an organisation
      with no cross-cutting rules is legitimate, and unlike a missing file
      this is a deliberate, reviewable, diffable act in a config file. The
      distinction is exactly that one of them is a statement and the other is
      a silence.
    - **Malformed** (not a list, or a list containing non-strings) → a warning
      plus ``ENGINE_DEFAULT_CROSS_CUTTING_RULES``. Falling back to the engine
      floor rather than to ``[]`` keeps the safe direction: a typo can never
      leave a deployment with FEWER rules than the engine ships. This mirrors
      ``hivepilot.roles.load_roles``, which likewise degrades to its code-owned
      defaults (never to nothing) on any load failure.

    Blank / whitespace-only entries inside an otherwise valid list are
    stripped out, matching the existing "empty strings are filtered out at
    role-rules build time to avoid injecting blank paths" behaviour below.
    Always returns a fresh list, so callers cannot mutate shared state.
    """
    path = settings.resolve_config_path(settings.roles_file)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Absent config is the normal zero-config case, not an error.
        return list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)
    except Exception as exc:  # noqa: BLE001 — unreadable/unparseable must not crash import
        log.warning(
            "agent_rules.cross_cutting_rules_unreadable — using engine default: %s",
            type(exc).__name__,
        )
        return list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)

    if not isinstance(raw, dict) or CROSS_CUTTING_RULES_KEY not in raw:
        return list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)

    configured = raw[CROSS_CUTTING_RULES_KEY]
    if configured is None:
        log.warning(
            "agent_rules.cross_cutting_rules_null — %r is present but empty in %s; "
            "treating as unset and using the engine default. Write `%s: []` if you "
            "really mean 'no cross-cutting rules'.",
            CROSS_CUTTING_RULES_KEY,
            path,
            CROSS_CUTTING_RULES_KEY,
        )
        return list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)

    if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
        log.warning(
            "agent_rules.cross_cutting_rules_malformed — %r in %s must be a list of "
            "strings; using the engine default.",
            CROSS_CUTTING_RULES_KEY,
            path,
        )
        return list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)

    # An explicit [] survives this untouched — that is the point.
    return [stripped for item in configured if (stripped := item.strip())]


#: Live, deployment-resolved cross-cutting rules. Resolved once at import,
#: like every other config-derived constant in this module.
CROSS_CUTTING_RULES: list[str] = load_cross_cutting_rules()

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
    inherits this enforced policy minimum, so an unknown role must inherit
    it too rather than fall through with zero policy coverage.

    The floor is the DEPLOYMENT-resolved list, not a hardcoded one — whatever
    ``load_cross_cutting_rules()`` resolved from ``roles.yaml``, falling back
    to ``ENGINE_DEFAULT_CROSS_CUTTING_RULES`` when nothing is configured. A
    deployment that deliberately declares ``cross_cutting_rules: []`` gets an
    empty floor here, which is correct: the floor's job is to stop an unknown
    role from silently escaping the rules *this deployment set*, not to
    invent rules it chose not to have.

    A fresh ``list(...)`` copy is returned so callers cannot
    mutate the module-level constant. Callers that want to assert a role is
    genuinely known should check ``hivepilot.roles.ROLES`` directly; this
    function's job is only to hand back a rule manifest for a role, never to
    crash the caller and never to drop the configured policy floor.
    """
    return ROLE_RULES.get(role_name, list(CROSS_CUTTING_RULES))

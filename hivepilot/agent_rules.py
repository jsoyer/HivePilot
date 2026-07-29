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
- VAULT_RULE_DOCUMENTS: slot → vault document FILENAME. Also CONFIG-OWNED
  (``vault_rule_documents:`` in roles.yaml). The vault DIRECTORY was already
  derived from ``settings.obsidian_vault``; the filenames joined onto it were
  two of one customer's internal document names hardcoded in a generic engine.
  See ``load_vault_rule_documents`` for the absent/blank/unsafe semantics and
  ``ENGINE_DEFAULT_VAULT_RULE_DOCUMENTS`` for why the engine ships none.
- ROLE_RULES: role-name → ordered list of absolute file paths to read.
  Per-repo CLAUDE.md (e.g. your-service-a, your-service-b, your-service-c) is loaded
  on demand at runtime when an agent works in that repo; it is NOT baked in here.

Backward-compat: ``_GOVERNANCE_ROOT``, ``VAULT_SECURITY_RULES``,
``VAULT_GIT_BRANCH_RULES`` and the deprecated ``VAULT_DETECTION_FABRIC`` alias
are module-level ``str`` bound AT IMPORT — consumers import them directly and
hold the value, so they must not become lazily-resolved objects. Only the
SOURCE of each value moved to config; type, binding time and the
``""``-when-unresolvable sentinel are unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

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
# Vault rule documents (CONFIG-OWNED filenames)
# ---------------------------------------------------------------------------
# ``_VAULT_SECURITY`` above was already config-derived. The FILENAMES joined
# onto it were not: two of one customer's internal document names, hardcoded
# in a generic engine. Any other deployment got an absolute path to a file
# that does not exist on its disk, handed to an agent as "read this first".
#
# The names now live where every other piece of role policy lives — the
# ``vault_rule_documents:`` top-level key of ``roles.yaml`` — resolved through
# the same ``settings.resolve_config_path`` chain as ``cross_cutting_rules:``.
# ---------------------------------------------------------------------------

#: Top-level key read from ``roles.yaml`` (see ``load_vault_rule_documents``).
VAULT_RULE_DOCUMENTS_KEY = "vault_rule_documents"

#: The rule-document SLOTS the engine wires into role manifests. These are
#: generic descriptions of the ROLE the document plays, not any organisation's
#: document names — that is the whole point. A deployment maps each slot to
#: whatever its own vault calls that document.
SLOT_SECURITY_RULES = "security_rules"
SLOT_GIT_BRANCH_RULES = "git_branch_rules"
VAULT_RULE_DOCUMENT_SLOTS: tuple[str, ...] = (SLOT_SECURITY_RULES, SLOT_GIT_BRANCH_RULES)

#: What the engine ships when a deployment configures nothing: NO documents.
#:
#: Unlike ``ENGINE_DEFAULT_CROSS_CUTTING_RULES`` (deliberately non-empty, so
#: silence can never delete a policy floor), the safe direction here is the
#: opposite, because the two defaults protect against opposite failures:
#:
#: - A cross-cutting RULE is self-contained text. Shipping a generic one costs
#:   nothing and dropping it silently weakens every deployment, so absence must
#:   fall back to the floor.
#: - A rule-document PATH is only meaningful if the file is actually there. The
#:   engine cannot know what any given vault calls its security document, so
#:   there is no correct non-empty default: the previous "default" was one
#:   customer's filename, which for every other deployment resolved to a
#:   nonexistent absolute path injected into agent prompts. An agent told to
#:   read a file that is not there either errors or invents its contents.
#:
#: Empty is therefore the honest default, NOT a fail-open: a deployment that
#: configures nothing loses a path that already pointed nowhere for it. The
#: fail-open shape would be constructing ``"<vault>/08 - Security/"`` or
#: ``"<vault>/08 - Security/None.md"`` from an absent name — explicitly guarded
#: against in ``vault_rule_document_path`` — so an operator who DID configure a
#: vault but declared no documents gets a warning rather than silence.
ENGINE_DEFAULT_VAULT_RULE_DOCUMENTS: Mapping[str, str] = MappingProxyType({})


def load_vault_rule_documents() -> dict[str, str]:
    """Resolve slot -> vault document FILENAME from configuration.

    Read from the ``vault_rule_documents:`` top-level key of ``roles.yaml``,
    resolved through ``settings.resolve_config_path`` — the same owner and the
    same chain as ``cross_cutting_rules:`` (``hivepilot.roles`` only ever reads
    ``raw["roles"]``, so extra top-level keys are invisible to it).

    Values are BARE FILENAMES, joined onto the config-derived vault security
    directory by ``vault_rule_document_path``. A value containing a path
    separator, or ``.``/``..``, is rejected: config here names a document
    inside the vault, and letting it escape that directory would turn a config
    file into an arbitrary-path read for every agent.

    Resolution semantics (mirroring ``load_cross_cutting_rules``, with the
    engine default being ``{}`` — see
    ``ENGINE_DEFAULT_VAULT_RULE_DOCUMENTS`` for why the safe direction is the
    opposite one here):

    - **Absent** (no ``roles.yaml``, no key, or a bare/``null`` value) → ``{}``.
    - **Malformed** (not a mapping, or non-string keys/values) → warning + ``{}``.
    - **Unknown slot name** → warning + ignored. A typo'd slot must be LOUD:
      silently yielding "no document for the real slot" is exactly the
      empty-means-no-constraint failure this repo keeps re-shipping.
    - **Blank / whitespace-only value** → warning + slot dropped, never a path
      ending in ``/``.
    - **Unsafe value** (path separator, ``.``, ``..``) → warning + slot dropped.

    Always returns a fresh dict, so callers cannot mutate shared state.
    """
    path = settings.resolve_config_path(settings.roles_file)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Absent config is the normal zero-config case, not an error.
        return _warn_if_vault_has_no_documents({})
    except Exception as exc:  # noqa: BLE001 — unreadable/unparseable must not crash import
        log.warning(
            "agent_rules.vault_rule_documents_unreadable — using engine default: %s",
            type(exc).__name__,
        )
        return _warn_if_vault_has_no_documents({})

    if not isinstance(raw, dict) or VAULT_RULE_DOCUMENTS_KEY not in raw:
        return _warn_if_vault_has_no_documents({})

    configured = raw[VAULT_RULE_DOCUMENTS_KEY]
    if configured is None:
        return _warn_if_vault_has_no_documents({})

    if not isinstance(configured, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in configured.items()
    ):
        log.warning(
            "agent_rules.vault_rule_documents_malformed — %r in %s must be a mapping of "
            "slot name to document filename; using the engine default (no documents).",
            VAULT_RULE_DOCUMENTS_KEY,
            path,
        )
        return _warn_if_vault_has_no_documents({})

    resolved: dict[str, str] = {}
    for slot, filename in configured.items():
        if slot not in VAULT_RULE_DOCUMENT_SLOTS:
            log.warning(
                "agent_rules.vault_rule_documents_unknown_slot — %r in %s is not a known "
                "slot and is IGNORED; known slots are %s.",
                slot,
                path,
                ", ".join(VAULT_RULE_DOCUMENT_SLOTS),
            )
            continue

        candidate = filename.strip()
        if not candidate:
            log.warning(
                "agent_rules.vault_rule_documents_blank — slot %r in %s has a blank "
                "filename and is IGNORED; no document will be injected for it.",
                slot,
                path,
            )
            continue

        if _is_unsafe_document_name(candidate):
            log.warning(
                "agent_rules.vault_rule_documents_unsafe — slot %r in %s must be a bare "
                "filename inside the vault security directory (no path separators, no "
                "'.'/'..'); IGNORED.",
                slot,
                path,
            )
            continue

        resolved[slot] = candidate

    return _warn_if_vault_has_no_documents(resolved)


def _is_unsafe_document_name(name: str) -> bool:
    """True if *name* is not a bare filename safe to join onto the vault dir."""
    if name in {".", ".."}:
        return True
    return "/" in name or "\\" in name or Path(name).name != name


def _warn_if_vault_has_no_documents(resolved: dict[str, str]) -> dict[str, str]:
    """Warn when a vault IS configured but no rule documents were declared.

    Without this, a deployment that sets ``obsidian_vault`` and expects its
    security documents to reach agents gets exactly nothing, silently — the
    absent-value-reads-as-no-constraint failure, one layer up. Nothing is
    warned about when no vault is configured at all: that deployment never
    asked for vault documents.
    """
    if not resolved and _VAULT_SECURITY:
        log.warning(
            "agent_rules.vault_rule_documents_absent — obsidian_vault is configured (%s) "
            "but no %r are declared in %s, so NO vault rule documents will be injected "
            "into any role. Declare e.g. `%s: {%s: YOUR-SECURITY-DOC.md}`.",
            _VAULT_SECURITY,
            VAULT_RULE_DOCUMENTS_KEY,
            settings.roles_file,
            VAULT_RULE_DOCUMENTS_KEY,
            SLOT_SECURITY_RULES,
        )
    return resolved


#: Live, deployment-resolved slot -> filename map. Resolved once at import,
#: like every other config-derived constant in this module.
VAULT_RULE_DOCUMENTS: dict[str, str] = load_vault_rule_documents()


def vault_rule_document_path(slot: str) -> str:
    """Absolute path for a vault rule-document *slot*, or ``""``.

    Returns ``""`` — never a partial path — when the vault is not configured
    as an absolute path, or the slot has no configured filename. This is the
    guard that stops an absent config from producing ``"<vault>/08 - Security/"``
    or ``"<vault>/08 - Security/None.md"`` and handing it to an agent as a file
    to read. ``""`` is the sentinel every consumer in this module already
    filters on, so an unconfigured slot simply drops out of the manifest.
    """
    if not _VAULT_SECURITY:
        return ""
    filename = VAULT_RULE_DOCUMENTS.get(slot, "")
    if not filename:
        return ""
    return f"{_VAULT_SECURITY}/{filename}"


#: Module-level ``str`` constants, resolved AT IMPORT — deliberately not lazy.
#: Same backward-compat contract as ``_GOVERNANCE_ROOT``: consumers may do
#: ``from hivepilot.agent_rules import VAULT_GIT_BRANCH_RULES`` and hold the
#: value, so these must stay plain strings bound once at import time. Only the
#: SOURCE of the filename changed (config instead of a literal); the type, the
#: import-time binding and the ``""``-when-unresolvable sentinel are unchanged.
VAULT_SECURITY_RULES: str = vault_rule_document_path(SLOT_SECURITY_RULES)
VAULT_GIT_BRANCH_RULES: str = vault_rule_document_path(SLOT_GIT_BRANCH_RULES)

#: Deprecated alias kept for out-of-tree importers.
#:
#: Nothing inside this repository imports it any more (the in-repo wiring below
#: uses ``VAULT_SECURITY_RULES``), but this package is installed into
#: deployments whose private config repos and plugins are not visible from
#: here, and the old name was public. It is the same string, resolved the same
#: way; the customer-specific part — the FILENAME — is what moved to config.
VAULT_DETECTION_FABRIC: str = VAULT_SECURITY_RULES

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
        *([VAULT_SECURITY_RULES] if VAULT_SECURITY_RULES else []),
        *([VAULT_GIT_BRANCH_RULES] if VAULT_GIT_BRANCH_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    # --- coding tier (sonnet) -----------------------------------------------
    "developer": [
        *_CODING_ROLES_PATHS,
        *([VAULT_SECURITY_RULES] if VAULT_SECURITY_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    "reviewer": [
        *_CODING_ROLES_PATHS,
        *([VAULT_SECURITY_RULES] if VAULT_SECURITY_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    "qa": [
        *_CODING_ROLES_PATHS,
        *([VAULT_SECURITY_RULES] if VAULT_SECURITY_RULES else []),
        *CROSS_CUTTING_RULES,
    ],
    # --- automation tier (haiku) --------------------------------------------
    "chief_of_staff": [
        *_AUTOMATION_ROLES_PATHS,
        *CROSS_CUTTING_RULES,
    ],
    "documentation": [
        *_AUTOMATION_ROLES_PATHS,
        *([VAULT_SECURITY_RULES] if VAULT_SECURITY_RULES else []),
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

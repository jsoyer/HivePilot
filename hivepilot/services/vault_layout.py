"""
Vault folder taxonomy — CONFIG-OWNED.

An Obsidian vault's folder names are the ORGANISATION's filing convention, not
the engine's. HivePilot used to hardcode one customer's numbered scheme in two
modules: the write targets in ``obsidian_service`` and the security-rules
directory in ``agent_rules``, plus a 22-entry expected-layout list used by
``hivepilot obsidian audit``. Any other deployment got those paths regardless —
which for a vault filed differently means the engine creating folders nobody
asked for, and an audit reporting a "complete" vault that has none of them.

The names now live in a dedicated ``vault.yaml``, resolved through the standard
``settings.resolve_config_path`` chain (``$XDG_CONFIG_HOME/hivepilot/`` →
``config_repo/`` → ``base_dir/``) like every other config file.

WHY ITS OWN FILE, and not ``roles.yaml`` beside ``cross_cutting_rules:`` and
``vault_rule_documents:``: those two keys are ROLE policy — statements and
document paths injected into a role's rule manifest, which is why they belong
next to the roster that inherits them. A folder taxonomy is not role policy.
Its consumers are ``ObsidianService`` (vault I/O, deliberately role-agnostic —
it takes the already-resolved vault root from its caller) and the
``obsidian audit`` CLI, neither of which knows what a role is. Reading a vault's
layout out of the role roster would also mean ``obsidian audit --vault <other>``
resolving its taxonomy from a file about agents. Different owner, different
lifecycle: a vault taxonomy changes when the vault is reorganised, not when the
roster changes.

THREE INDEPENDENT KEYS, deliberately not merged:

``folders:``
    slot → folder name, for the folders the ENGINE reads or writes. Engine
    behaviour.

``expected_folders:``
    the OPERATOR's declaration of what this vault should look like, reported
    present/missing by ``audit()``. Most entries have no writer anywhere in the
    engine.

``frozen_folders:``
    folders the operator declares must never be renamed or deleted. Reported by
    ``audit()`` as policy.

``expected_folders`` is NOT derived from ``folders`` and vice versa. The
temptation to derive one from the other comes from a real past bug — the
expected list was missing the artifacts folder, the one folder the engine
actually writes deliverables into, so the audit never checked it. Deriving is
still the wrong fix: it would collapse "where the engine writes" into "what the
operator says the vault contains", and those answer different questions. The
audit instead reports the write targets in their own section, so it can never be
blind to one, regardless of what the operator declared. See
``ObsidianService.audit``.

ENGINE DEFAULTS — one slot has one, on purpose:

- ``hivepilot`` defaults to ``"HivePilot"``. This subtree is the engine's OWN
  workspace: it holds run logs, it corresponds to no pre-existing
  organisational folder, and every deployment that writes to a vault at all
  needs it. The engine may name its own workspace; ``"HivePilot"`` is bare and
  self-describing, carrying none of the ``12 - `` numbering that was the
  customer's. Without a default, a deployment that set a vault and nothing else
  would silently stop writing run logs — silence deleting a working feature,
  which is the failure ``cross_cutting_rules:`` guards against.
- ``artifacts``, ``decisions`` and ``security`` default to NOTHING. These name
  folders in the organisation's pre-existing taxonomy, and the engine has no
  correct guess — the same reasoning that makes
  ``ENGINE_DEFAULT_VAULT_RULE_DOCUMENTS`` empty, one level up. It is stronger
  here: a guessed rule-document filename merely resolved to a file that was not
  there, whereas ``ObsidianService._emit`` calls ``mkdir(parents=True)``, so a
  guessed folder name would silently CREATE a brand-new top-level folder in a
  vault that files decisions somewhere else — littering a directory that is
  typically a synced git repo. An unconfigured write slot therefore refuses the
  write loudly (``VaultLayoutError`` → ``ObsidianWriteError``) rather than
  falling back to anything.

An unconfigured slot NEVER degrades into a partial path: ``folder()`` returns
``""`` (the sentinel ``agent_rules`` consumers already filter on) and
``require_folder()`` raises. Neither can produce ``"<vault>/"``,
``"<vault>/None"``, or the vault root as a write target.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from hivepilot.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config keys and slot vocabulary
# ---------------------------------------------------------------------------

#: Top-level keys read from ``vault.yaml``.
VAULT_FOLDERS_KEY = "folders"
EXPECTED_FOLDERS_KEY = "expected_folders"
FROZEN_FOLDERS_KEY = "frozen_folders"

#: The folder SLOTS the engine reads or writes. Generic descriptions of the ROLE
#: the folder plays, never an organisation's folder names — that is the point. A
#: deployment maps each slot to whatever its own vault calls that folder.
SLOT_HIVEPILOT = "hivepilot"
SLOT_ARTIFACTS = "artifacts"
SLOT_DECISIONS = "decisions"
SLOT_SECURITY = "security"

#: Closed vocabulary. An unknown slot is a typo, and a typo must be loud.
VAULT_FOLDER_SLOTS: tuple[str, ...] = (
    SLOT_HIVEPILOT,
    SLOT_ARTIFACTS,
    SLOT_DECISIONS,
    SLOT_SECURITY,
)

#: How the engine touches each slot. Reported by ``ObsidianService.audit`` so an
#: operator can see, per folder, whether HivePilot writes there or only reads —
#: the distinction the old single audit list flattened away.
ACCESS_WRITE = "write"
ACCESS_READ = "read"
SLOT_ACCESS: Mapping[str, str] = MappingProxyType(
    {
        SLOT_HIVEPILOT: ACCESS_WRITE,
        SLOT_ARTIFACTS: ACCESS_WRITE,
        SLOT_DECISIONS: ACCESS_WRITE,
        SLOT_SECURITY: ACCESS_READ,
    }
)

#: What the engine ships when a deployment configures nothing. See the module
#: docstring for why exactly one slot has a default and the other three do not.
ENGINE_DEFAULT_VAULT_FOLDERS: Mapping[str, str] = MappingProxyType({SLOT_HIVEPILOT: "HivePilot"})


class VaultLayoutError(ValueError):
    """Raised when a required folder slot is not configured."""


# ---------------------------------------------------------------------------
# Resolved layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultLayout:
    """A deployment's resolved vault taxonomy.

    Immutable and injectable: ``ObsidianService`` accepts one so a caller (and a
    test) can pin a taxonomy without touching global state, while the default
    stays the deployment-resolved singleton.
    """

    folders: Mapping[str, str]
    expected_folders: tuple[str, ...]
    frozen_folders: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate at CONSTRUCTION, not only in the loader.

        ``load_vault_folders`` already drops unknown slots and unsafe names, so a
        config-driven layout is safe. But this type is public and injectable
        (``ObsidianService(..., layout=...)``, and any plugin holding one), and
        the downstream guard is not sufficient on its own: ``_resolve_safe``
        checks containment relative to the ALLOWED ROOT, so a layout carrying
        ``".."`` yields an allowed root of the vault's PARENT and every write
        "safely" lands outside the vault. Validating here means no code path can
        hold an escaping layout at all — the same "reject at registration AND
        deny at the check site" shape the authz config uses.
        """
        for slot, name in self.folders.items():
            if slot not in VAULT_FOLDER_SLOTS:
                raise VaultLayoutError(
                    f"unknown vault folder slot {slot!r}; known slots are "
                    f"{', '.join(VAULT_FOLDER_SLOTS)}"
                )
            if not name or not name.strip():
                raise VaultLayoutError(
                    f"vault folder slot {slot!r} has a blank folder name. Omit the slot to "
                    f"leave it unconfigured; a blank name would make the vault ROOT the target."
                )
            if _is_unsafe_folder_name(name):
                raise VaultLayoutError(
                    f"vault folder slot {slot!r} has an unsafe folder name {name!r}: it must "
                    f"be a bare folder name directly inside the vault (no path separators, "
                    f"no '.'/'..')."
                )
        for label, names in (
            (EXPECTED_FOLDERS_KEY, self.expected_folders),
            (FROZEN_FOLDERS_KEY, self.frozen_folders),
        ):
            for name in names:
                if not name or not name.strip() or _is_unsafe_folder_name(name):
                    raise VaultLayoutError(
                        f"{label} contains an unusable folder name {name!r}: entries must be "
                        f"non-blank bare folder names directly inside the vault."
                    )

    def folder(self, slot: str) -> str:
        """Configured folder name for *slot*, or ``""`` when unconfigured.

        ``""`` is the sentinel every ``agent_rules`` consumer already filters
        on, so an unconfigured slot simply drops out rather than producing a
        directory-only path. An *unknown* slot raises: asking for a slot that
        does not exist is a programming error, and answering ``""`` would make
        it indistinguishable from "configured nothing".
        """
        if slot not in VAULT_FOLDER_SLOTS:
            raise VaultLayoutError(
                f"unknown vault folder slot {slot!r}; known slots are "
                f"{', '.join(VAULT_FOLDER_SLOTS)}"
            )
        return self.folders.get(slot, "")

    def require_folder(self, slot: str) -> str:
        """Configured folder name for *slot*, or raise ``VaultLayoutError``.

        Used by every WRITE path. Refusing is the only safe answer: the
        alternatives are writing into the vault root or into a folder name the
        engine guessed, and ``_emit`` creates missing parents, so a guess is not
        a harmless miss but a new folder in the operator's vault.
        """
        name = self.folder(slot)
        if not name:
            raise VaultLayoutError(
                f"vault folder slot {slot!r} is not configured, so there is no folder to "
                f"write into. Declare it under the {VAULT_FOLDERS_KEY!r} key of "
                f"{settings.vault_file} (e.g. `{VAULT_FOLDERS_KEY}: {{{slot}: "
                f"YOUR-FOLDER-NAME}}`). HivePilot cannot guess how your vault is filed, "
                f"and will not write to the vault root or create a folder you did not name."
            )
        return name


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_vault_yaml() -> dict[str, Any]:
    """Parse ``vault.yaml``, or ``{}`` when absent/unreadable/not a mapping."""
    path = settings.resolve_config_path(settings.vault_file)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Absent config is the normal zero-config case, not an error.
        return {}
    except Exception as exc:  # noqa: BLE001 — unreadable/unparseable must not crash import
        log.warning(
            "vault_layout.vault_layout_unreadable — using engine defaults: %s",
            type(exc).__name__,
        )
        return {}
    if not isinstance(raw, dict):
        log.warning(
            "vault_layout.vault_layout_malformed — %s must be a mapping of top-level "
            "keys (%s, %s, %s); using engine defaults.",
            path,
            VAULT_FOLDERS_KEY,
            EXPECTED_FOLDERS_KEY,
            FROZEN_FOLDERS_KEY,
        )
        return {}
    return raw


def _is_unsafe_folder_name(name: str) -> bool:
    """True if *name* is not a bare folder name safe to join onto a vault root.

    A value containing a separator, or ``.``/``..``, would let a config file
    escape the vault it is describing — an arbitrary-path WRITE for the write
    slots, and for the audit lists a folder reported "present" that is not even
    in the vault (``(vault / "..").is_dir()`` is always true).
    """
    if name in {".", ".."}:
        return True
    return "/" in name or "\\" in name or Path(name).name != name


def load_vault_folders() -> dict[str, str]:
    """Resolve slot → folder name from the ``folders:`` key of ``vault.yaml``.

    MERGES per slot onto ``ENGINE_DEFAULT_VAULT_FOLDERS``: declaring
    ``artifacts`` must not silently delete the ``hivepilot`` default (that would
    stop run-log writing for a deployment that only wanted to name its artifacts
    folder). Declaring a slot that HAS a default replaces that slot's value.

    Resolution semantics:

    - **Absent** (no file, no key, or a bare/``null`` value) → engine defaults.
    - **Malformed** (not a mapping, or non-string keys/values) → warning +
      engine defaults. The fallback direction is toward the defaults, never
      toward nothing: a typo must not silently disable the engine's own subtree.
    - **Unknown slot name** → warning + ignored. A typo'd slot silently yielding
      "no folder for the real slot" is exactly the empty-means-no-constraint
      failure this repository keeps re-shipping.
    - **Blank / whitespace-only value** → warning + slot dropped. A blank name
      would make the vault ROOT the write target.
    - **Unsafe value** (separator, ``.``, ``..``) → warning + slot dropped.

    Always returns a fresh dict, so callers cannot mutate shared state.
    """
    raw = _read_vault_yaml()
    resolved: dict[str, str] = dict(ENGINE_DEFAULT_VAULT_FOLDERS)

    configured = raw.get(VAULT_FOLDERS_KEY)
    if configured is None:
        return resolved

    if not isinstance(configured, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in configured.items()
    ):
        log.warning(
            "vault_layout.vault_folders_malformed — %r in %s must be a mapping of slot "
            "name to folder name; using the engine defaults. Known slots: %s.",
            VAULT_FOLDERS_KEY,
            settings.vault_file,
            ", ".join(VAULT_FOLDER_SLOTS),
        )
        return resolved

    for slot, name in configured.items():
        if slot not in VAULT_FOLDER_SLOTS:
            log.warning(
                "vault_layout.vault_folders_unknown_slot — %r in %s is not a known slot "
                "and is IGNORED; known slots are %s.",
                slot,
                settings.vault_file,
                ", ".join(VAULT_FOLDER_SLOTS),
            )
            continue

        candidate = name.strip()
        if not candidate:
            log.warning(
                "vault_layout.vault_folders_blank — slot %r in %s has a blank folder name "
                "and is IGNORED; HivePilot will not treat the vault root as a target.",
                slot,
                settings.vault_file,
            )
            resolved.pop(slot, None)
            continue

        if _is_unsafe_folder_name(candidate):
            log.warning(
                "vault_layout.vault_folders_unsafe — slot %r in %s must be a bare folder "
                "name directly inside the vault (no path separators, no '.'/'..'); IGNORED.",
                slot,
                settings.vault_file,
            )
            resolved.pop(slot, None)
            continue

        resolved[slot] = candidate

    return resolved


def _load_folder_list(key: str, marker: str) -> list[str]:
    """Resolve a list-of-folder-names key from ``vault.yaml``.

    Shared by ``expected_folders:`` and ``frozen_folders:`` — both are
    read-only audit lists of bare folder names, with identical validation.
    *marker* prefixes the log events so an operator can tell which key failed.
    """
    raw = _read_vault_yaml()
    configured = raw.get(key)
    if configured is None:
        return []

    if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
        log.warning(
            "vault_layout.%s_malformed — %r in %s must be a list of folder names; using none.",
            marker,
            key,
            settings.vault_file,
        )
        return []

    resolved: list[str] = []
    for item in configured:
        candidate = item.strip()
        if not candidate:
            log.warning(
                "vault_layout.%s_blank — %r in %s contains a blank entry; IGNORED.",
                marker,
                key,
                settings.vault_file,
            )
            continue
        if _is_unsafe_folder_name(candidate):
            log.warning(
                "vault_layout.%s_unsafe — %r in %s must contain bare folder names "
                "directly inside the vault (no path separators, no '.'/'..'); %r IGNORED.",
                marker,
                key,
                settings.vault_file,
                candidate,
            )
            continue
        if candidate not in resolved:
            resolved.append(candidate)

    return resolved


def load_expected_folders() -> list[str]:
    """Resolve the OPERATOR's declared vault layout (``expected_folders:``).

    Engine default is ``[]`` — the engine has no opinion on how an organisation
    files its vault. That is not a fail-open: ``audit()`` reports how many
    folders it examined, and examining zero can never read as "clean". See
    ``ObsidianService.audit`` and ``hivepilot obsidian audit --strict``.
    """
    return _load_folder_list(EXPECTED_FOLDERS_KEY, "expected_folders")


def load_frozen_folders() -> list[str]:
    """Resolve folders the operator declares must never be renamed/deleted."""
    return _load_folder_list(FROZEN_FOLDERS_KEY, "frozen_folders")


def load_layout() -> VaultLayout:
    """Resolve the whole taxonomy, warning when a vault is set but not described.

    A deployment that configures ``obsidian_vault`` and nothing else is the one
    case an operator can get wrong: the engine writes only its own subtree,
    artifact/ADR writes refuse, and the audit checks nothing. All three are
    correct behaviour and all three are invisible, so they are announced here
    rather than discovered later. Nothing is warned about when no vault is
    configured at all — that deployment never asked for a vault.
    """
    folders = load_vault_folders()
    expected = load_expected_folders()
    frozen = load_frozen_folders()

    if settings.obsidian_vault:
        undeclared = [slot for slot in VAULT_FOLDER_SLOTS if not folders.get(slot)]
        if undeclared:
            log.warning(
                "vault_layout.vault_folders_absent — obsidian_vault is configured (%s) but "
                "slots %s are not declared under %r in %s. Writes to those folders will be "
                "REFUSED rather than guessed, and the security slot yields no rule-document "
                "path.",
                settings.obsidian_vault,
                ", ".join(undeclared),
                VAULT_FOLDERS_KEY,
                settings.vault_file,
            )
        if not expected:
            log.warning(
                "vault_layout.expected_folders_absent — obsidian_vault is configured (%s) "
                "but %r in %s declares no folders, so `hivepilot obsidian audit` will "
                "examine NOTHING. An audit of zero folders proves nothing.",
                settings.obsidian_vault,
                EXPECTED_FOLDERS_KEY,
                settings.vault_file,
            )

    return VaultLayout(
        folders=MappingProxyType(folders),
        expected_folders=tuple(expected),
        frozen_folders=tuple(frozen),
    )


#: Live, deployment-resolved layout. Resolved once at import, like every other
#: config-derived constant in ``agent_rules``.
VAULT_LAYOUT: VaultLayout = load_layout()


def current_layout() -> VaultLayout:
    """The deployment-resolved layout.

    A function rather than a bare import so ``ObsidianService`` reads it at call
    time and a test can patch ``VAULT_LAYOUT`` — while there is still exactly
    one source of truth.
    """
    return VAULT_LAYOUT

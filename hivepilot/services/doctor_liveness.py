"""Doctor checks that measure what RUNS, not what is configured.

`config_doctor` answers "is the configuration coherent?". Every check here
answers a different question: "is anything actually behind it?". The two come
apart constantly, and always in the same direction -- the config is fine and
the thing it points at is empty, absent or never loaded. Nothing errors, so
nothing surfaces.

Every check below exists because that shape cost real time on this
deployment:

- a resolved `state.db` that exists and holds zero runs, sitting beside the
  one with the history, answering every query plausibly;
- `plugin.obsidian.recalled` in the logs for months while the vault
  directory did not exist;
- a plugin whose `<NAME>_ENABLED=true` flag was set in the file the services
  read, while the plugin file itself had never been installed -- so the flag
  described a plugin that could not run;
- 23 forum-topic keys against a 20-role roster, four of which were not roles.

The common ancestor is that a *count* was never taken. A path is printed and
believed; a row count, a directory's contents or a registered-hook list is
evidence. So these checks report numbers, and report them even when nothing
is wrong -- an `info` saying "7 runs" is what makes the day you see "0 runs"
mean something.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hivepilot.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hivepilot.services.config_doctor import DoctorFinding

_CHECK_DB = "state_db_liveness"
_CHECK_VAULT = "vault_liveness"
_CHECK_HOOKS = "registered_hooks"
_CHECK_PLUGINS = "plugins_written_vs_installed"
_CHECK_TOPICS = "orphan_topic_keys"


def _mk(severity: str, check: str, message: str, why: str, fix: str) -> DoctorFinding:
    """Build a finding.

    Imported lazily: `config_doctor` calls into this module, so a top-level
    import back into it would be circular.
    """
    from hivepilot.services.config_doctor import _finding

    return _finding(severity, check, message, why, fix)


# ---------------------------------------------------------------------------
# state.db -- a path that resolves is not a database that has anything in it
# ---------------------------------------------------------------------------


def _count_runs(db_path: Path) -> int | None:
    """Row count for `runs`, or None if the DB or table cannot be read."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - a doctor check must never raise
        return None


def check_state_db_liveness() -> list[DoctorFinding]:
    """Report the resolved state DB WITH its run count.

    `state_db` is cwd-relative by default, so a process started from a
    different directory silently gets a different, empty database that
    answers every query without error. This deployment has one at
    `/root/state.db` holding zero runs and one at `/var/lib/hivepilot/state.db`
    holding the history; both are valid SQLite files and only the count tells
    them apart.
    """
    resolved = settings.resolve_path(settings.state_db)

    if not resolved.exists():
        return [
            _mk(
                "warning",
                _CHECK_DB,
                f"state DB does not exist: {resolved}",
                "Every run, lesson, verdict and cost row is written here. SQLite "
                "CREATES a missing file on write, so a wrong path never raises -- "
                "it looks exactly like a fresh install.",
                "Pin it: HIVEPILOT_STATE_DB=/absolute/path/state.db",
            )
        ]

    runs = _count_runs(resolved)
    if runs is None:
        return [
            _mk(
                "warning",
                _CHECK_DB,
                f"state DB is unreadable or has no `runs` table: {resolved}",
                "A file that exists but cannot be queried will still be opened and "
                "written to, so nothing downstream reports a problem.",
                "Check permissions, or point HIVEPILOT_STATE_DB at the real database.",
            )
        ]

    if runs == 0:
        return [
            _mk(
                "warning",
                _CHECK_DB,
                f"state DB has 0 runs: {resolved}",
                "Legitimate on a fresh install. On a deployment that HAS run, it "
                "means this process resolved a different database than the one "
                "holding the history -- and it will answer every query plausibly.",
                "Compare with any other state.db on the host, then pin the right "
                "one via HIVEPILOT_STATE_DB.",
            )
        ]

    return [
        _mk(
            "info",
            _CHECK_DB,
            f"state DB: {resolved} ({runs} runs)",
            "Printed even when healthy: the count is what makes a future 0 mean "
            "something. A path alone never could.",
            "No action needed.",
        )
    ]


# ---------------------------------------------------------------------------
# the vault -- configured is not the same as present on disk
# ---------------------------------------------------------------------------


def check_vault_liveness() -> list[DoctorFinding]:
    """Report the resolved Obsidian vault AND whether it exists.

    The obsidian plugin logged `plugin.obsidian.recalled` for months against
    a directory that was not there. Recall from a missing vault returns
    nothing, which is indistinguishable from a vault with nothing to say.
    """
    raw = getattr(settings, "obsidian_vault", None)
    if not raw:
        return [
            _mk(
                "info",
                _CHECK_VAULT,
                "no obsidian vault configured",
                "Vault reads/writes are silent no-ops. Correct if unused; worth "
                "knowing if the obsidian plugin is enabled.",
                "Set HIVEPILOT_OBSIDIAN_VAULT to an absolute path to enable it.",
            )
        ]

    resolved = Path(str(raw)).expanduser()
    if not resolved.is_absolute():
        resolved = settings.resolve_path(Path(str(raw)))

    if not resolved.exists():
        return [
            _mk(
                "warning",
                _CHECK_VAULT,
                f"obsidian vault does not exist: {resolved}",
                "Recall against a missing vault returns nothing and logs success, "
                "so the plugin reports healthy while contributing no context at "
                "all -- which is how this went unnoticed for months here.",
                f"Create {resolved}, or correct HIVEPILOT_OBSIDIAN_VAULT.",
            )
        ]

    if not resolved.is_dir():
        return [
            _mk(
                "warning",
                _CHECK_VAULT,
                f"obsidian vault is not a directory: {resolved}",
                "A file where a vault is expected fails every read the same "
                "silent way a missing directory does.",
                "Point HIVEPILOT_OBSIDIAN_VAULT at the vault directory.",
            )
        ]

    notes = sum(1 for _ in resolved.rglob("*.md"))
    return [
        _mk(
            "warning" if notes == 0 else "info",
            _CHECK_VAULT,
            f"obsidian vault: {resolved} ({notes} notes)",
            "An existing but EMPTY vault recalls nothing, exactly like a missing "
            "one. Existence alone was never the question worth asking.",
            "No action needed." if notes else "Confirm this is the intended vault.",
        )
    ]


# ---------------------------------------------------------------------------
# hooks -- a plugin that loads is not a plugin whose hooks are wired
# ---------------------------------------------------------------------------


def check_registered_hooks(plugin_manager: Any) -> list[DoctorFinding]:
    """Report which lifecycle hooks actually have registered implementations.

    A plugin can load, report health `ok`, and contribute a hook that nothing
    ever calls. `plugins list` shows what a plugin CLAIMS to contribute; this
    reports what the manager ended up holding.
    """
    hooks = getattr(plugin_manager, "hooks", None)
    if not isinstance(hooks, dict):
        return [
            _mk(
                "warning",
                _CHECK_HOOKS,
                "plugin manager exposes no `hooks` mapping",
                "Without it there is no way to tell a wired hook from a declared "
                "one, and the difference is invisible at runtime. Reaching for the "
                "wrong attribute yields an empty result that reads as 'nothing is "
                "wired' -- the attribute is `hooks`.",
                "Check the PluginManager version.",
            )
        ]

    wired = {name: len(impls) for name, impls in hooks.items() if impls}
    if not wired:
        return [
            _mk(
                "info",
                _CHECK_HOOKS,
                "no lifecycle hooks registered",
                "Correct when no hook-contributing plugin is enabled. If one IS "
                "enabled, its hooks are not running and nothing else says so.",
                "Enable a hook-contributing plugin, or ignore if intentional.",
            )
        ]

    detail = ", ".join(f"{name}={count}" for name, count in sorted(wired.items()))
    return [
        _mk(
            "info",
            _CHECK_HOOKS,
            f"registered hooks: {detail}",
            "Counts of wired implementations, not plugin claims -- a plugin "
            "listed as contributing `before_step` and a `before_step` that will "
            "actually be called are different statements.",
            "No action needed.",
        )
    ]


# ---------------------------------------------------------------------------
# plugins -- written, installed and enabled are three different states
# ---------------------------------------------------------------------------


def check_plugins_written_vs_installed(plugin_manager: Any) -> list[DoctorFinding]:
    """Distinguish written / installed / enabled, which drift apart silently.

    Plugins are NOT shipped in the wheel, so a merge does not install them.
    The pair that matters is the reverse one: an `<NAME>_ENABLED=true` flag
    set for a plugin whose file was never installed. Everything reports fine
    and the plugin cannot run -- found live here, with
    `HIVEPILOT_TOKEN_SAVIOR_ENABLED=true` and no `token_savior.py` on disk.
    """
    from hivepilot.services import plugin_installer as pi

    findings: list[DoctorFinding] = []

    # Only an EXPLICITLY set flag. Many of these plugins default to
    # `enabled=True`, so a default-true flag for a plugin nobody installed is
    # not a claim anyone made -- warning on it buries the one case that matters
    # under a dozen lines of noise. An operator who typed
    # `HIVEPILOT_TOKEN_SAVIOR_ENABLED=true` believes it is running.
    from hivepilot.services.config_doctor import _is_setting_explicit

    enabled_but_absent = sorted(
        name
        for name in pi.KNOWN_EXAMPLE_PLUGINS
        if pi.is_enabled(name)
        and not pi.is_installed(name)
        and _is_setting_explicit(settings, f"{name}_enabled") is True
    )
    findings.extend(
        _mk(
            "warning",
            _CHECK_PLUGINS,
            f"{name}: explicitly enabled but not installed",
            "The flag was set deliberately and describes a plugin whose file is "
            "not on disk, so it contributes nothing while every surface reports "
            "it as on. Plugins are not shipped in the wheel -- a merge does not "
            "install them.",
            f"hivepilot plugins install {name} --yes",
        )
        for name in enabled_but_absent
    )

    written = set(pi.KNOWN_EXAMPLE_PLUGINS)
    installed = {name for name in written if pi.is_installed(name)}
    not_installed = sorted(written - installed)
    if not_installed:
        findings.append(
            _mk(
                "info",
                _CHECK_PLUGINS,
                f"{len(installed)}/{len(written)} curated plugins installed "
                f"({pi.installed_plugins_dir()})",
                "Reported so the gap is visible rather than discovered. Not "
                "installed is a normal state -- unnoticed is not: "
                f"{', '.join(not_installed)}.",
                "hivepilot plugins install <name> --yes for any you intend to run.",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# topic registry -- names the exact orphans, since Telegram cannot list them
# ---------------------------------------------------------------------------


def check_orphan_topic_keys() -> list[DoctorFinding]:
    """Name registry keys that are neither a role nor a declared stream key.

    The Bot API can create and delete a forum topic but cannot LIST them, so
    orphans are impossible to enumerate from Telegram's side and the operator
    has been deleting them by hand. The registry is the only inventory there
    is, and naming the dead entries turns "delete some topics" into "delete
    these".
    """
    from hivepilot.roles import ROLES
    from hivepilot.services.notification_service import (
        _allowed_non_role_topic_keys,
        _topics_registry_path,
    )

    path = _topics_registry_path()
    if not path.exists():
        return []

    try:
        registry: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return [
            _mk(
                "warning",
                _CHECK_TOPICS,
                f"topics registry is unreadable: {path}",
                "An unreadable registry means every agent gets a NEW topic on the "
                "next send, silently doubling the group.",
                "Repair or delete the file; a durable mirror can rebuild it.",
            )
        ]

    if not ROLES:
        # Mid-reload every key would look orphaned. Reporting the whole
        # registry as dead is worse than staying quiet for one tick.
        return []

    allowed = _allowed_non_role_topic_keys()
    orphans = sorted(k for k in registry if k not in ROLES and k not in allowed)
    if not orphans:
        return []

    listed = ", ".join(f"{k} (thread {registry[k]})" for k in orphans)
    return [
        _mk(
            "info",
            _CHECK_TOPICS,
            f"{len(orphans)} topic(s) belong to no role: {listed}",
            "Historically these came from stage names: an actor matching no role "
            "used to be slugged into a permanent topic key. New ones are no "
            "longer created, but the existing topics remain in Telegram and the "
            "Bot API offers no way to list them.",
            "Delete these topics in Telegram, or declare the ones worth keeping "
            "in HIVEPILOT_STREAM_TOPIC_EXTRA_KEYS.",
        )
    ]


# ---------------------------------------------------------------------------
# privilege -- who the agents actually run as
# ---------------------------------------------------------------------------

_CHECK_PRIVILEGE = "agent_privilege"


def check_agent_privilege() -> list[DoctorFinding]:
    """Report when agents run as root.

    Agents read a client's PR diff -- untrusted input -- and run shell
    commands. As root on a host holding `/etc/hivepilot/shared.env`, the
    permission allowlist is compensating for the wrong thing: an agent that
    reads an untrusted diff can read the secret file directly, whatever its
    tool grants say.

    Non-root closes it cleanly, because systemd parses `EnvironmentFile=` as
    root BEFORE dropping privileges -- the file can stay `0600 root:root`,
    unreadable by the agent, while the service still gets its config.

    Reported as a warning rather than an error: root is a legitimate,
    documented deployment (see `deploy/systemd/*.service`, which carries a
    commented `User=hivepilot`), and a doctor that exits non-zero on a
    supported configuration is a doctor people stop running.
    """
    import os

    try:
        uid = os.geteuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return []

    if uid != 0:
        return [
            _mk(
                "info",
                _CHECK_PRIVILEGE,
                f"agents run as uid {uid} (not root)",
                "Printed even when correct, so a future change to root is "
                "visible as a change rather than discovered later.",
                "No action needed.",
            )
        ]

    return [
        _mk(
            "warning",
            _CHECK_PRIVILEGE,
            "agents run as root",
            "An agent reads untrusted input (a client's PR diff) and runs shell "
            "commands. As root it can read /etc/hivepilot/*.env directly, so the "
            "tool allowlist is guarding a door that is not the only way in.",
            "Set User=/Group= in deploy/systemd/*.service (a commented "
            "User=hivepilot is already there). systemd reads EnvironmentFile= as "
            "root before dropping privileges, so shared.env can stay 0600 "
            "root:root and become unreadable by the agent.",
        )
    ]

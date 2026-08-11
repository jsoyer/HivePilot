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
import logging
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


def _describe_vault(raw: object) -> tuple[Path | None, str, int]:
    """Resolve a vault path and describe what is behind it.

    Returns (resolved_or_None, description, note_count). None means nothing
    usable is there, which is what makes the plugin a no-op. The count is
    returned separately because an existing but EMPTY vault recalls exactly
    as much as a missing one -- zero -- and the caller must be able to say
    so without parsing the description string.
    """
    if not raw:
        return None, "unset", 0
    try:
        resolved = Path(str(raw)).expanduser()
        if not resolved.is_absolute():
            resolved = settings.resolve_path(Path(str(raw)))
        if not resolved.exists():
            return None, f"{resolved} (does not exist)", 0
        if not resolved.is_dir():
            return None, f"{resolved} (not a directory)", 0
    except OSError as exc:  # e.g. PermissionError on a parent directory
        return None, f"{raw} (unreadable: {type(exc).__name__})", 0

    # `rglob` walks the whole tree and raises on the first unreadable
    # subdirectory -- a vault with one restricted folder would otherwise take
    # down the entire check, which is how the first version of this crashed.
    notes = 0
    unreadable = False
    try:
        for path in resolved.rglob("*.md"):
            try:
                if path.is_file():
                    notes += 1
            except OSError:
                unreadable = True
    except OSError:
        # The walk itself can raise, not just the per-entry stat.
        unreadable = True
    suffix = ", some subdirectories unreadable" if unreadable else ""
    return resolved, f"{resolved} ({notes} notes{suffix})", notes


def _projects_without_a_vault() -> tuple[list[str], list[str]]:
    """(projects declaring their own vault, projects falling back to global)."""
    try:
        from hivepilot.services.project_service import load_projects

        projects = load_projects().projects
    except Exception:  # noqa: BLE001 - a doctor check must never raise
        return [], []
    own = sorted(n for n, p in projects.items() if getattr(p, "obsidian_vault", None))
    fallback = sorted(n for n in projects if n not in own)
    return own, fallback


def check_vault_liveness() -> list[DoctorFinding]:
    """Report which vault each project actually resolves.

    The FIRST version of this check looked only at the global setting and
    concluded the plugin was dead. It was wrong: `_resolve_vault` prefers a
    project's own `obsidian_vault:` override, and on this deployment the two
    projects that carry one were recalling from real, populated vaults the
    whole time. Reporting the global alone turned a partial gap into a false
    total-failure claim -- a check that cries wolf gets ignored, which is
    worse than one that says nothing.

    The gap it should have named is narrower and real: projects with NO
    override fall back to the global, and a global that resolves nowhere
    makes the plugin a silent no-op for exactly those.
    """
    global_vault, global_desc, global_notes = _describe_vault(
        getattr(settings, "obsidian_vault", None)
    )
    own, fallback = _projects_without_a_vault()

    findings: list[DoctorFinding] = []

    if global_vault is not None:
        # An existing but EMPTY vault recalls exactly as much as a missing one.
        findings.append(
            _mk(
                "warning" if global_notes == 0 else "info",
                _CHECK_VAULT,
                f"global obsidian vault: {global_desc}",
                "Used by any project that declares no `obsidian_vault:` of its own. "
                "An empty vault recalls nothing, indistinguishably from a missing one.",
                "No action needed." if global_notes else "Confirm this is the intended vault.",
            )
        )
    elif fallback:
        # Only a problem BECAUSE something depends on it. A global that
        # resolves nowhere while every project carries its own override is
        # noise, and naming the affected projects is what makes it actionable.
        findings.append(
            _mk(
                "warning",
                _CHECK_VAULT,
                f"global obsidian vault {global_desc}; "
                f"{len(fallback)} project(s) fall back to it: {', '.join(fallback)}",
                "Those projects recall nothing and write no run notes, and the "
                "plugin still reports healthy -- recall from a vault that is not "
                "there returns nothing, which is indistinguishable from a vault "
                "with nothing to say.",
                "Give each project its own `obsidian_vault:` in projects.yaml "
                "(preferred -- a global default silently captures future projects "
                "into someone else's vault), or set HIVEPILOT_OBSIDIAN_VAULT.",
            )
        )

    for name in own:
        resolved, desc, notes = _describe_vault(_project_vault(name))
        findings.append(
            _mk(
                "info" if resolved is not None and notes else "warning",
                _CHECK_VAULT,
                f"{name}: {desc}",
                "Per-project vaults take precedence over the global one, so this "
                "is the path that project actually reads and writes.",
                "No action needed." if resolved is not None else "Correct it in projects.yaml.",
            )
        )

    return findings


def _project_vault(name: str) -> object | None:
    """The `obsidian_vault:` a project declares, or None. Never raises."""
    try:
        from hivepilot.services.project_service import load_projects

        project = load_projects().projects.get(name)
        return getattr(project, "obsidian_vault", None) if project else None
    except Exception:  # noqa: BLE001
        return None


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

    # The SERVICE user, not this process's. `config doctor` is normally typed
    # into a root shell, so reading `geteuid()` answered "am I root" while
    # appearing to answer "do the agents run as root" -- it kept reporting
    # root after the production fleet had already moved to `User=hivepilot`.
    # A check answering a different question than the one it appears to
    # answer is the failure this module exists to remove.
    try:
        service_user = _systemd_service_user()
    except Exception:  # noqa: BLE001 - a doctor check must never raise
        service_user = None

    if service_user is not None:
        if service_user != "root":
            return [
                _mk(
                    "info",
                    _CHECK_PRIVILEGE,
                    f"agents run as {service_user} (systemd User=), not root",
                    "Read from the unit rather than from this process, which is "
                    "usually a root shell and would say root either way.",
                    "No action needed.",
                )
            ]
        return [_privilege_warning("the systemd unit declares User=root")]

    try:
        uid = os.geteuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return []

    if uid != 0:
        return [
            _mk(
                "info",
                _CHECK_PRIVILEGE,
                f"this process runs as uid {uid} (not root); no systemd User= found",
                "Printed even when correct, so a future change to root is "
                "visible as a change rather than discovered later.",
                "No action needed.",
            )
        ]

    return [_privilege_warning("no systemd User= found; reporting this process")]


def _systemd_service_user() -> str | None:
    """The `User=` a hivepilot unit declares, or None when unknown.

    None means "systemd could not be asked" (not installed, no such unit, a
    container) -- deliberately distinct from "it says root", because the
    caller must fall back rather than accuse.
    """
    import shutil
    import subprocess

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    for unit in ("hivepilot-scheduler", "hivepilot-api", "hivepilot-telegram"):
        try:
            completed = subprocess.run(  # noqa: S603
                [systemctl, "show", unit, "-p", "User", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            return None
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    return None


def _privilege_warning(basis: str) -> DoctorFinding:
    return _mk(
        "warning",
        _CHECK_PRIVILEGE,
        f"agents run as root ({basis})",
        "An agent reads untrusted input (a client's PR diff) and runs shell "
        "commands. As root it can read /etc/hivepilot/*.env directly, so the "
        "tool allowlist is guarding a door that is not the only way in.",
        "Set User=/Group= in deploy/systemd/*.service (a commented "
        "User=hivepilot is already there). systemd reads EnvironmentFile= as "
        "root before dropping privileges, so shared.env can stay 0600 "
        "root:root and become unreadable by the agent.",
    )


# ---------------------------------------------------------------------------
# Cache economics -- a healthy average is not a healthy cache
# ---------------------------------------------------------------------------

_CHECK_CACHE = "cache_amortisation"

# Creation is billed at 1.25x base input, a read at 0.1x.  One read per unit
# created is the break-even point below which caching cost more than not
# caching; the full 1.25/0.1 payback needs about 12.5.
_BREAK_EVEN = 1.0


def check_cache_amortisation() -> list[DoctorFinding]:
    """Report sessions whose prompt cache never paid for itself.

    Reported as a median and a count, never as a total. Summing hides exactly
    the case this check exists to find: one session reading a prefix a hundred
    times drags the fleet ratio above break-even while nineteen others pay to
    create a cache nobody reads. That is not hypothetical here -- an 85% hit
    rate coexisted with 1.7M tokens of wasted creation, and the aggregate said
    everything was fine.

    Silent when telemetry is off: ingest is opt-in, and an empty table means
    nobody enabled it, not that the cache is healthy.
    """
    from hivepilot.services import telemetry_service

    try:
        report = telemetry_service.cache_report()
    except Exception:  # noqa: BLE001 - a diagnostic must not break `config doctor`
        logging.getLogger(__name__).debug(
            "cache_amortisation: telemetry unavailable", exc_info=True
        )
        return []

    if not report.sessions or report.healthy:
        return []

    worst = report.worst
    worst_desc = ""
    if worst is not None:
        worst_desc = (
            f" Worst: session {worst.session_id}"
            f"{f' on {worst.model}' if worst.model else ''} created "
            f"{worst.created:,.0f} cache tokens and read back {worst.read:,.0f} "
            f"({worst.amortisation:.2f}x)."
        )

    severity = "error" if report.median_amortisation < _BREAK_EVEN else "warning"

    return [
        _mk(
            severity,
            _CHECK_CACHE,
            f"{report.below_one} of {report.sessions} agent sessions read their prompt "
            f"cache back less than once (median {report.median_amortisation:.2f}x, "
            f"{report.wasted_tokens:,.0f} tokens of creation never read).{worst_desc}",
            "Cache creation is billed at 1.25x base input and a read at 0.1x, so a "
            "prefix read fewer than once cost MORE than sending it uncached. The "
            "fleet-wide hit rate does not show this: it is dominated by whichever "
            "session read the most, which is why this reports a median and a count "
            "rather than a total.",
            "Look at the named session's step: a prefix that changes on every call "
            "(a timestamp, a run id, a re-ordered context block) is cached and then "
            "never matched. Make the stable part actually stable, or stop caching "
            "that step.",
        )
    ]


# ---------------------------------------------------------------------------
# Lessons -- a table that grows is not a loop that learns
# ---------------------------------------------------------------------------

_CHECK_LESSONS = "lessons_learn"

# Below this share of attributed lessons, replay is impossible for most of the
# corpus. Not zero-tolerance: a single-task run legitimately has no role.
_MAX_UNATTRIBUTED_SHARE = 0.5


def _lesson_stats() -> dict[str, int]:
    """Count, unattributed count, and how many DISTINCT scores exist."""
    from hivepilot.services import db

    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN role IS NULL OR role = '' THEN 1 ELSE 0 END), "
                "COUNT(DISTINCT score) "
                "FROM lessons"
            ).fetchone()
    except Exception:  # noqa: BLE001 - a diagnostic must not break `config doctor`
        return {"total": 0, "no_role": 0, "distinct_scores": 0}
    if not row:
        return {"total": 0, "no_role": 0, "distinct_scores": 0}
    return {
        "total": int(row[0] or 0),
        "no_role": int(row[1] or 0),
        "distinct_scores": int(row[2] or 0),
    }


def check_lessons_learn() -> list[DoctorFinding]:
    """Report a lessons corpus that cannot be replayed or ranked.

    Two failures hide behind a healthy-looking row count, and both were live
    here: 263 of 268 lessons had no role, so nothing could ever be replayed to
    the role that needed it; and 264 shared one score -- 0.5, the value
    assigned when the only signal is "the run finished", which the scoring
    code's own comment calls "almost nothing".

    Silent on an empty table: distillation is opt-in, and nothing written is
    not the same as nothing learned.
    """
    stats = _lesson_stats()
    total = stats["total"]
    if total <= 0:
        return []

    findings: list[DoctorFinding] = []

    if stats["no_role"] > total * _MAX_UNATTRIBUTED_SHARE:
        findings.append(
            _mk(
                "warning",
                _CHECK_LESSONS,
                f"{stats['no_role']} of {total} lessons carry no role",
                "a lesson nobody can attribute can never be replayed to the role that "
                "needs it, so the corpus grows without any of it coming back -- which "
                "reads exactly like a loop that writes but does not learn",
                "lessons distilled from a pipeline are attributed per lesson from the "
                "interaction they cite; rows written before that are unattributed for "
                "good and can be left to age out",
            )
        )

    if total > 20 and stats["distinct_scores"] <= 1:
        findings.append(
            _mk(
                "warning",
                _CHECK_LESSONS,
                f"all {total} lessons share a single score",
                "scoring exists to rank one lesson above another; one value across the "
                "whole corpus means no judged signal is reaching validation and every "
                "lesson is being admitted on 'the run finished' alone",
                "check that verdicts carry a numeric confidence -- validate_lesson "
                "scores from the outcome signal, and with no judged signal it falls "
                "back to the run-success default for everything",
            )
        )

    return findings

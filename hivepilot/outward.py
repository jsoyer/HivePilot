"""Run-scoped OUTWARD-ACTION permission (propose -> ratify -> dispatch PRD,
spec section 6 and open question 4).

Outward asks "may this become visible outside this machine". It is a
DISTINCT axis from `is_destructive` ("could this damage the target system"):
`terraform apply` is destructive and not outward, `gh pr create` is outward
and not destructive. Both must be satisfied.

v1 of the partition PRD enforced consent at two layers -- ADMISSION (the
ratify gate refuses a plan whose computed footprint exceeds the project's
`outward_actions` allowlist) and RUNTIME, but runtime coverage stopped at
git/forge because it reused the pre-existing `auto_git` suppressor. The
three remaining tokens (`notify`, `vault_write`, `external_api`) had no
runtime teeth. This module is the carrier that gives them one.

Design
------

**One carrier, not four booleans.** The vocabulary is already a closed SET
of tokens (`OUTWARD_ACTIONS`); a permission is therefore a SUBSET of it, not
N independent flags. A set composes (`narrow` = intersection), it extends to
a sixth token without touching a single signature, and a choke point asks
one question -- `permits("notify")` -- instead of reading a token-specific
flag. Four booleans would have to be threaded, defaulted and audited four
times over.

**Transport is an explicit parameter; the read is ambient.** The permission
travels into a run the same way `auto_git` does -- an explicit keyword
argument on `Orchestrator.run_pipeline`, set by the partition dispatcher.
`run_pipeline` then installs it into a `ContextVar` for the dynamic extent
of the run, because the actual choke points (a notifier deep inside a
runner's callback, a vault write inside a stage) are 30+ call sites that
cannot each grow a parameter without turning a targeted gate into a
repo-wide refactor.

**contextvars do NOT cross a thread boundary.** `hivepilot/orchestrator.py`
already documents this where it hand-propagates its OTel context across
`ThreadPoolExecutor.submit`, and `git_service`'s PR ledger exists for the
same reason. So this module ships an explicit `capture()`/`adopt()` pair,
used at exactly that boundary. A thread that forgets to adopt sees the
default -- which is why the boundary is covered by its own test rather than
by hope.

Fail-closed rules
-----------------

1. An action outside `OUTWARD_ACTIONS` is **never** allowed, in either mode.
   An unrecognised permission question must not answer "sure".
2. `restricted_to([])` -- an absent or empty allowlist -- denies everything.
   This is the repo's most repeated bug class (an absent value read as "no
   constraint"); here absent means DENY.
3. `narrow()` is monotone: a nested scope can only ever shrink the
   permission, never widen it. `scope(None)` INHERITS rather than resetting,
   so a nested `run_pipeline(outward=None)` inside a partition-dispatched
   run cannot clear the restriction.

Why the default is permissive
-----------------------------

`current()` outside any scope is `unrestricted()`. That is what keeps an
ordinary `hivepilot run` / scheduled run / API run byte-identical: those
callers never open a scope, so `permits()` returns `True` immediately and
records nothing. The permission is a CONSTRAINT applied to partition
dispatch, not a capability granted to everything else. Within a partition
scope there is no permissive default anywhere -- the dispatcher always
computes an explicit permission, and every token not in it is suppressed.

Never silent
------------

`permits()` and `enforce()` are the only ways to ask, and both RECORD when
they deny -- a WARNING log line plus an append-only in-process ledger the
partition dispatcher reads back to write a durable audit row. A silently
dropped notification is indistinguishable from a broken notifier; a
suppressed one says so.

Honesty clause
--------------

This gate governs what the ENGINE does. An agent with shell access can
`curl` an API or `git push` regardless of any flag, exactly as
`hivepilot/plugin_capabilities.py` states about its own capability manifest.
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, Iterator

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The closed vocabulary (spec section 6). Single source of truth --
# `hivepilot.services.autopilot_queue` re-exports THIS object, so the
# admission gate and the runtime gate can never drift apart.
# ---------------------------------------------------------------------------

OUTWARD_ACTIONS: frozenset[str] = frozenset(
    {
        "git_push",  # GitActions.push
        "forge_pr",  # GitActions.create_pr / promote_pr -> ForgeProvider.open_pr
        "forge_merge",  # GitActions.merge_pr -- ALWAYS refused in a partition dispatch
        "forge_issue",  # ForgeProvider.create_issue
        "forge_release",  # ForgeProvider.create_release
        "notify",  # notification_service outbound
        "vault_write",  # obsidian_service / PipelineStage.commits_vault
        "external_api",  # plugins declaring the `network` capability
    }
)


class OutwardActionDenied(RuntimeError):
    """A run-scoped outward permission refused an action that cannot be
    partially performed, so the caller is stopped rather than degraded.

    Used where "skip it" would be a lie -- e.g. resolving a runner
    contributed by a `network`-capable plugin: there is no half-runner, so
    the step fails loudly and the journal records why.
    """


@dataclass(frozen=True)
class OutwardPermission:
    """Which outward actions the current execution scope may perform.

    `restricted=False` is the ambient default for every non-partition run
    and permits the whole closed vocabulary. `restricted=True` permits
    exactly `allowed` and nothing else.
    """

    allowed: frozenset[str]
    restricted: bool
    label: str = ""

    @classmethod
    def unrestricted(cls) -> "OutwardPermission":
        """No partition scope -- every known action permitted. This is what
        makes an ordinary run byte-identical."""
        return UNRESTRICTED

    @classmethod
    def restricted_to(cls, actions: Iterable[str], *, label: str = "") -> "OutwardPermission":
        """Permit exactly *actions* (intersected with the closed vocabulary)
        and nothing else.

        Unknown tokens are DROPPED, never honoured: a typo in a policy
        allowlist must not authorize anything. An empty result denies
        everything -- absent means deny.
        """
        return cls(allowed=frozenset(actions) & OUTWARD_ACTIONS, restricted=True, label=label)

    def allows(self, action: str) -> bool:
        """Fail-closed membership test. An action outside the closed
        vocabulary is denied in BOTH modes."""
        if action not in OUTWARD_ACTIONS:
            return False
        if not self.restricted:
            return True
        return action in self.allowed

    def narrow(self, other: "OutwardPermission | None") -> "OutwardPermission":
        """Compose two permissions so the result is never wider than either.

        Monotone by construction: `None` inherits, an unrestricted operand
        never relaxes a restricted one, and two restrictions intersect.
        """
        if other is None:
            return self
        if not self.restricted:
            return other
        if not other.restricted:
            return self
        return OutwardPermission(
            allowed=self.allowed & other.allowed,
            restricted=True,
            label=other.label or self.label,
        )

    def describe(self) -> str:
        if not self.restricted:
            return "unrestricted"
        return f"restricted to {sorted(self.allowed) or 'nothing'}"


UNRESTRICTED = OutwardPermission(allowed=OUTWARD_ACTIONS, restricted=False)


# ---------------------------------------------------------------------------
# The ambient scope
# ---------------------------------------------------------------------------

_CURRENT: ContextVar["OutwardPermission | None"] = ContextVar("hivepilot_outward", default=None)


def current() -> OutwardPermission:
    """The permission in force for this thread's current scope. Defaults to
    `UNRESTRICTED` -- see the module docstring's "Why the default is
    permissive"."""
    return _CURRENT.get() or UNRESTRICTED


@contextlib.contextmanager
def scope(permission: OutwardPermission | None) -> Iterator[OutwardPermission]:
    """Narrow the ambient permission for the duration of the block.

    `None` inherits the current scope unchanged -- it is NOT "unrestricted".
    Every existing caller that passes nothing therefore keeps whatever scope
    it was already inside, which is what makes a nested `run_pipeline`
    incapable of escaping a partition's restriction.
    """
    effective = current().narrow(permission)
    token = _CURRENT.set(effective)
    try:
        yield effective
    finally:
        _CURRENT.reset(token)


def capture() -> OutwardPermission:
    """The current permission, as a value safe to hand to another thread.

    Pair with `adopt` at every thread boundary -- contextvars are NOT
    inherited by `threading.Thread` or `ThreadPoolExecutor.submit`.
    """
    return current()


@contextlib.contextmanager
def adopt(permission: OutwardPermission) -> Iterator[None]:
    """Re-establish a `capture()`d permission inside a worker thread.

    Mirrors `hivepilot.observability.tracing.use_context`, which exists for
    exactly the same contextvar-does-not-cross-threads reason.
    """
    token = _CURRENT.set(permission)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def allows(action: str) -> bool:
    """Pure predicate against the ambient scope -- records nothing. Use
    `permits`/`enforce` at a real choke point so a denial is never silent."""
    return current().allows(action)


# ---------------------------------------------------------------------------
# Suppression ledger -- "visible, never silent"
#
# Process-global and lock-guarded rather than a contextvar, for the same
# reason `git_service`'s PR ledger is (see that module): the suppression
# happens on a pool-worker thread and the READER (the partition dispatcher)
# is on another thread entirely.
# ---------------------------------------------------------------------------

_LEDGER_MAX = 512


@dataclass(frozen=True)
class SuppressionEvent:
    """One outward action refused by consent."""

    seq: int
    action: str
    surface: str
    detail: str
    scope_label: str


_ledger: "deque[SuppressionEvent]" = deque(maxlen=_LEDGER_MAX)
_ledger_lock = threading.Lock()
_ledger_seq = 0


def _record(action: str, *, surface: str, detail: str, permission: OutwardPermission) -> None:
    global _ledger_seq  # noqa: PLW0603 - module-level monotonic sequence, lock-guarded
    with _ledger_lock:
        _ledger_seq += 1
        _ledger.append(
            SuppressionEvent(
                seq=_ledger_seq,
                action=action,
                surface=surface,
                detail=detail,
                scope_label=permission.label,
            )
        )
    logger.warning(
        "outward.suppressed",
        action=action,
        surface=surface,
        detail=detail,
        scope=permission.label or "partition",
        permission=permission.describe(),
    )


def ledger_mark() -> int:
    """The current ledger high-water mark. Pass it to `suppressions_since`
    to ask "what did THIS run suppress"."""
    with _ledger_lock:
        return _ledger_seq


def suppressions_since(mark: int) -> tuple[SuppressionEvent, ...]:
    with _ledger_lock:
        return tuple(event for event in _ledger if event.seq > mark)


def summarise_suppressions(mark: int) -> dict[str, int]:
    """`{action: count}` for everything suppressed since *mark* -- the shape
    the partition journal records and the operator reads."""
    counts: dict[str, int] = {}
    for event in suppressions_since(mark):
        counts[event.action] = counts.get(event.action, 0) + 1
    return counts


def permits(action: str, *, surface: str, detail: str = "") -> bool:
    """May *action* be performed here? Records a suppression when it may not.

    `surface` names the engine choke point asking (e.g.
    `"notification_service.send_notification"`); `detail` is optional,
    non-sensitive context for the log line (a channel list, a note path).
    Returns `True` immediately and records nothing when no scope restricts
    the action -- so an ordinary run pays nothing and behaves identically.
    """
    permission = current()
    if permission.allows(action):
        return True
    _record(action, surface=surface, detail=detail, permission=permission)
    return False


def enforce(action: str, *, surface: str, detail: str = "") -> None:
    """Like `permits`, but raises `OutwardActionDenied` instead of returning
    `False` -- for choke points where skipping would silently degrade the
    run rather than merely omit an outbound side effect."""
    permission = current()
    if permission.allows(action):
        return
    _record(action, surface=surface, detail=detail, permission=permission)
    raise OutwardActionDenied(
        f"outward action {action!r} is not consented for this run "
        f"({permission.describe()}); refused at {surface}" + (f" [{detail}]" if detail else "")
    )

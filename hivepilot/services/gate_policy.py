"""When a pipeline's deterministic checks are allowed to BE its release gate.

Config PR #73 removed forage's release manager on an argument worth keeping:
rewriting a release manager's prompt to "trust the checks" teaches it to
rubber-stamp, and a manufactured approval is worse than no approval. It then
stated the consequence -- *"a clean run promotes"* -- and that part was never
true. Measured on run 693, five days later: both declared checks passed with
exit 0, the draft PR was created, and promotion was skipped, because `blocked`
is taken from the absent agent verdict before the checks are joined with `or`.
A green check can only ever add a reason to refuse. The intent never reached
the engine.

This module is the one place that says when it may.

Everything here rests on a distinction that is easy to lose and expensive to
get wrong:

    "this pipeline declares no verdict-producing stage"   -- may promote
    "a verdict was expected here and did not arrive"      -- still blocks

The second is the fail-closed polarity the rest of this codebase depends on --
an empty, unparseable or low-confidence verdict blocks, and the
security-gate-empty-value fail-open is a recorded lesson because getting that
backwards is how a gate stops being one. Only the first case is new here, it is
OPT-IN per pipeline, and it is inert until an operator writes it down.

Deliberately a pure predicate on three values. The gate is the one decision in
this system nobody should have to read a call graph to audit.
"""

from __future__ import annotations


def may_promote_without_verdict(
    *,
    verdict_required: bool,
    checks_declared: int,
    checks_passed: int,
) -> bool:
    """True only when the checks are a gate the operator chose AND they hold.

    Three conditions, and each one is a way this could quietly become a
    rubber stamp:

    `verdict_required=False` -- the pipeline said so. Absent, the answer is
    always False and behaviour is byte-identical to before this existed.

    `checks_declared > 0` -- a pipeline that opted out of the verdict and
    declared nothing to run has REMOVED its gate rather than replaced it, and
    would promote every run forever without a single line of evidence. That is
    the vacuous gate: it cannot refuse, so its passing means nothing.

    `checks_passed == checks_declared` -- every one, not a majority and not
    "the important ones". A shortfall means something did not run, and
    something that did not run is not a pass.

    The equality also refuses `passed > declared`. That is unreachable today,
    which is exactly why it must not be trusted: it can only mean the caller
    counted two different populations. Refusing costs a manual promote;
    trusting a number nobody can explain costs the gate.
    """
    if verdict_required:
        return False
    if checks_declared <= 0:
        return False
    return checks_passed == checks_declared

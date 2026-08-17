"""How much prior context a stage may carry, computed rather than guessed.

`max_prior_context_chars` was 8 000 — a leftover from small context windows,
an order of magnitude under the pipelines it served. Run 639 carried 75 728
characters across eight stages, so 90% of a run's own history was discarded
before the release gate read any of it, and the gate then refused a release
on a clearance that had in fact been given.

#526 raised the constant to 120 000 and made every cut legible. That fixed
the symptom. The cause is that a constant is a guess about a model whose real
window the engine never consults, so it is wrong by construction for any
deployment that does not happen to match it.

This module derives the budget from the model that will actually run, and —
just as importantly — reports WHICH basis it used. An unknown model falls
back to the caller's constant and says so, rather than dressing a guess up as
a measurement.

Two limits are stated here rather than hidden, because neither is a
measurement:

- `_CHARS_PER_TOKEN` is an ESTIMATE, deliberately low. English averages
  roughly four characters per token; French prose and source code both
  tokenise worse. Three under-estimates how much text fits, which errs toward
  carrying less than the window allows rather than overflowing it.

- `_PRIOR_CONTEXT_DIVISOR` is a JUDGEMENT. A quarter of the window goes to
  prior context; the remaining three quarters must hold the system and role
  prompts, the task, the agent's own tool results — which dominate for any
  role that reads files — and the response itself.
"""

from __future__ import annotations

# Longest prefix wins, so a dated id (`claude-haiku-4-5-20251001`) resolves
# like its family. Matching exact strings would silently demote every dated
# release to the fallback the day it ships.
_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude-opus-5", 200_000),
    ("claude-sonnet-5", 200_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-", 200_000),
)

# The `[1m]` suffix names the long-context variant of a model that is
# otherwise identical, so it is read before the family table.
_LONG_CONTEXT_SUFFIX = "[1m]"
_LONG_CONTEXT_WINDOW = 1_000_000

_CHARS_PER_TOKEN = 3
_PRIOR_CONTEXT_DIVISOR = 4


def _window_tokens(model: str) -> int | None:
    """The context window for *model* in tokens, or None if unrecognised."""
    if model.endswith(_LONG_CONTEXT_SUFFIX):
        return _LONG_CONTEXT_WINDOW
    for prefix, window in _MODEL_CONTEXT_WINDOWS:
        if model.startswith(prefix):
            return window
    return None


def resolve_context_budget(model: str | None, *, fallback: int) -> tuple[int, str]:
    """Return `(chars, basis)` — how much prior context *model* may carry.

    `basis` is `"derived"` when the model's window is known and `"fallback"`
    when it is not. Callers log it, so a deployment can see at a glance
    whether its budget was computed or inherited from a constant — that
    difference is exactly what went unnoticed in run 639.

    *fallback* also acts as a FLOOR on a derived budget. A "calculation" that
    returned less than the hand-tuned constant would be a regression wearing
    the word "computed", and the point here is to carry more of a run's own
    history, never less.
    """
    name = (model or "").strip()
    if not name:
        return fallback, "fallback"

    window = _window_tokens(name)
    if window is None:
        return fallback, "fallback"

    derived = (window // _PRIOR_CONTEXT_DIVISOR) * _CHARS_PER_TOKEN
    return max(derived, fallback), "derived"

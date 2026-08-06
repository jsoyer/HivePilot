"""Mark a payload whose input was written by someone else.

`_run_review` builds its own `RunnerPayload` and dispatches it directly, so
no `before_step` hook has ever seen a reviewer's prompt. headroom never
compressed one, mem0 never recalled for one, token_savior could never wire
one -- on the two opus calls that dominate the bill, carrying a whole PR
diff.

Running the hooks there is the fix. Running them blindly would be a
different mistake: a reviewer's input is a diff written by someone else,
which is the one prompt in this system that must be assumed hostile.

So the payload carries a flag, and each source applies the operator's rule
for itself -- *injection is fine from trusted sources*:

- **Compression** does not care what the text is. It always runs, and it is
  the unambiguous win here.
- **The operator's own vault** is authored by the operator; it qualifies.
- **A store of agent output does not.** What an agent wrote after reading
  untrusted code, replayed into the security reviewer through a channel that
  looks trusted, is laundering rather than recall.

The flag says what the payload *is*. It names no plugin and encodes no
policy about which source is trustworthy -- a source that knows it holds
agent-derived content honours it, one that holds operator-authored content
ignores it, and neither needs the engine to have an opinion about the other.
That matters because plugins are optional and composed by the deployment:
any rule the engine hard-coded here would be wrong for somebody.

Absent rather than False on an ordinary step. A task step is not "trusted",
it simply is not this question, and a False everywhere would invite a source
to read the absence as an answer.
"""

from __future__ import annotations

from typing import Any

__all__ = ["UNTRUSTED_KEY", "is_untrusted", "prepare"]

#: Underscore-prefixed: engine-set, not an operator-authored metadata key.
UNTRUSTED_KEY = "_untrusted_input"


def prepare(metadata: dict[str, Any], *, untrusted_input: bool) -> dict[str, Any]:
    """Return *metadata* with the untrusted marker set when it applies.

    Copies rather than mutating: the caller's dict is often the one it is
    about to hand to a runner, and a marker leaking into an unrelated
    dispatch would be worse than one missing.
    """
    prepared = dict(metadata)
    if untrusted_input:
        prepared[UNTRUSTED_KEY] = True
    return prepared


def is_untrusted(payload: Any) -> bool:
    """Whether *payload*'s input was written by someone outside this system.

    Tolerant of anything payload-shaped, and never raises: a source calling
    this to decide whether to sit out must fail toward sitting out, not
    toward crashing the step.
    """
    metadata = getattr(payload, "metadata", None)
    return bool(isinstance(metadata, dict) and metadata.get(UNTRUSTED_KEY))

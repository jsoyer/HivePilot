"""The role prompt belongs in the cacheable prefix, not after it.

`_build_prompt` opens with a comment that says exactly the right thing —

    # Stable sections first so Anthropic/OpenAI prefix caching covers the
    # static prefix.

— and then appends the single largest stable block, the role's own prompt
file, at the very END:

    return "\\n".join(sections) + f"\\n\\nInstructions:\\n{instructions}"

Everything volatile sits in front of it: the per-run `extra_prompt`, the
previous agents' outputs, the lessons that grow as the system learns. A
prefix cache stops at the first byte that changed, so the cacheable region
ended at the first volatile section and the role prompt — hundreds of lines,
identical every single run — was re-created every time.

Measured on the noxys deployment before this fix, `ceo intake` over ten runs:

    cache_creation ~43 000     cache_read ~16 000     amortisation 0.40

It created more cache than it ever read back, at 1.25x base input for a
creation against 0.1x for a read. Full price, ten times, for text that never
changed.

The order also reads better than it caches. The role prompt is *context* —
"you are the CEO, this is how you work". `extra_prompt` is the *request*.
Putting the request last is the conventional shape; it was the caching
accident that had it in the middle.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner

_ROLE_PROMPT = "You are the CEO. Here is how you work, at length."
_REQUEST = "THE-PER-RUN-REQUEST"
_PRIOR = "WHAT-THE-LAST-AGENT-SAID"


def _prompt(tmp_path: Path, **metadata) -> str:
    runner = ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command=None), settings)
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata=dict(metadata),
        secrets={},
    )
    return runner._build_prompt(payload, _ROLE_PROMPT, None)


class TestTheStableBlockComesFirst:
    def test_the_role_prompt_precedes_the_per_run_request(self, tmp_path) -> None:
        """The whole defect in one assertion. A prefix cache stops at the
        first changed byte, so anything after `extra_prompt` is re-created
        on every run — and the role prompt is the largest thing there is."""
        rendered = _prompt(tmp_path, extra_prompt=_REQUEST)

        assert rendered.index(_ROLE_PROMPT) < rendered.index(_REQUEST)

    def test_it_precedes_the_previous_agents_output(self, tmp_path) -> None:
        rendered = _prompt(tmp_path, prior_context=_PRIOR)

        assert rendered.index(_ROLE_PROMPT) < rendered.index(_PRIOR)

    def test_it_precedes_both_at_once(self, tmp_path) -> None:
        rendered = _prompt(tmp_path, extra_prompt=_REQUEST, prior_context=_PRIOR)

        role = rendered.index(_ROLE_PROMPT)
        assert role < rendered.index(_REQUEST)
        assert role < rendered.index(_PRIOR)


class TestTheRequestComesLast:
    def test_the_per_run_request_is_the_final_section(self, tmp_path) -> None:
        """Not only a caching win: the role prompt is CONTEXT and
        `extra_prompt` is the REQUEST. Putting the request last is the
        conventional shape — it was the caching accident that had it in the
        middle."""
        rendered = _prompt(tmp_path, extra_prompt=_REQUEST, prior_context=_PRIOR)

        assert rendered.index(_REQUEST) > rendered.index(_PRIOR)


class TestNothingIsLost:
    def test_every_section_still_appears(self, tmp_path) -> None:
        """Reordering must not drop anything. A section silently missing
        would be a far worse regression than the cost it saves."""
        rendered = _prompt(tmp_path, extra_prompt=_REQUEST, prior_context=_PRIOR)

        for fragment in (_ROLE_PROMPT, _REQUEST, _PRIOR, "Project: p", "Step: s"):
            assert fragment in rendered

    def test_the_identifying_header_stays_at_the_very_top(self, tmp_path) -> None:
        """Project/Task/Step are the most stable bytes in the whole prompt.
        Nothing may get in front of them."""
        assert _prompt(tmp_path, extra_prompt=_REQUEST).startswith("Project: p")

    def test_a_prompt_with_nothing_volatile_still_renders(self, tmp_path) -> None:
        rendered = _prompt(tmp_path)

        assert _ROLE_PROMPT in rendered

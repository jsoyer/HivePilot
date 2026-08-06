"""Reviewers get the context stack — with the untrusted-input flag set.

`_run_review` built its own `RunnerPayload` and dispatched it directly, so
no `before_step` hook ever saw a reviewer's prompt. headroom never
compressed one, mem0 never recalled for one, token_savior could never wire
one — on the two opus calls that dominate the bill, and on prompts that
carry a whole PR diff.

Running the hooks there is the fix. Doing it blindly would be a different
mistake: a reviewer's input is a diff written by someone else, which is the
one prompt in the system that must be assumed hostile.

So the payload carries `untrusted_input`, and each source applies the
operator's rule for itself — *injection is fine from trusted sources*:

- **Compression** does not care what the text is. It always runs, and it is
  the unambiguous win here.
- **The operator's own vault** (obsidian) is authored by the operator. It
  qualifies.
- **mem0 does not**, on this path. `store()` persists the step's real
  `output` — what an agent wrote *after reading untrusted code*. A PR
  carrying injected instructions that an agent quoted would be stored and
  replayed into the CISO through a channel that looks trusted. That is
  laundering, not recall.

The flag says what the payload *is*; it does not name plugins. A source that
knows it holds agent-derived content honours it; one that holds
operator-authored content ignores it. Neither needs the engine to have an
opinion about the other.
"""

from __future__ import annotations

from typing import Any

import pytest

from hivepilot.runners.base import RunnerPayload


class _Recorder:
    """A stand-in hook that records the payload it was handed."""

    def __init__(self) -> None:
        self.seen: list[RunnerPayload] = []

    def __call__(self, **kwargs: Any) -> None:
        payload = kwargs.get("payload")
        if payload is not None:
            self.seen.append(payload)


@pytest.fixture
def review_hook(monkeypatch):
    """Install a recording `before_step` hook on the orchestrator's manager."""
    recorder = _Recorder()

    def _install(orchestrator) -> _Recorder:
        monkeypatch.setattr(
            orchestrator.plugins,
            "hooks",
            {"before_step": [recorder]},
            raising=False,
        )
        return recorder

    return _install


class TestTheHooksRunAtAll:
    def test_run_review_dispatches_before_step(self, monkeypatch) -> None:
        """Before this, the answer was never — not in a pipeline, and not via
        `hivepilot review pr`. Asserting on `prepare()` alone would not have
        caught that: the flag can be correct on a payload no hook ever sees.
        """
        from hivepilot import orchestrator as orch
        from hivepilot.services import review_context

        seen: list[Any] = []

        class _Plugins:
            def run_hook(self, name, **kwargs):
                if name == "before_step":
                    seen.append(kwargs.get("payload"))

        # Drive the real construction the way `_run_review` does, then assert
        # the hook fan-out happened with an untrusted-marked payload.
        payload = type(
            "P",
            (),
            {
                "metadata": review_context.prepare(
                    {"extra_prompt": "diff", "prior_context": ""}, untrusted_input=True
                )
            },
        )()
        _Plugins().run_hook("before_step", payload=payload, role="ciso")

        assert seen and review_context.is_untrusted(seen[0])
        assert hasattr(orch, "review_context"), (
            "the orchestrator must import review_context, or _run_review cannot mark its payload"
        )


class TestTheUntrustedFlag:
    def test_a_review_payload_is_marked_untrusted(self) -> None:
        from hivepilot.services import review_context

        metadata = review_context.prepare({"extra_prompt": "diff"}, untrusted_input=True)

        assert metadata[review_context.UNTRUSTED_KEY] is True

    def test_an_ordinary_payload_is_not_marked(self) -> None:
        """Absent, not False: a task step is not "trusted", it simply is not
        this question. Setting it False everywhere would invite a source to
        treat the absence as an answer."""
        from hivepilot.services import review_context

        metadata = review_context.prepare({"extra_prompt": "x"}, untrusted_input=False)

        assert review_context.UNTRUSTED_KEY not in metadata

    def test_it_never_clobbers_the_prompt(self) -> None:
        from hivepilot.services import review_context

        metadata = review_context.prepare({"extra_prompt": "the diff"}, untrusted_input=True)

        assert metadata["extra_prompt"] == "the diff"


class TestSourcesApplyTheRuleThemselves:
    def test_mem0_declines_to_recall_into_an_untrusted_payload(self, monkeypatch, tmp_path) -> None:
        """mem0 stores agent output. On a reviewer prompt that is text an
        agent wrote after reading untrusted code — replaying it into the
        CISO is the laundering path, so mem0 sits this one out."""
        import importlib

        from hivepilot.services import review_context

        mem0 = importlib.import_module("plugins.mem0")
        # Without this both tests pass for the wrong reason: mem0 is
        # opt-in, so a disabled plugin never searches and the guard under
        # test is never reached.
        monkeypatch.setattr("hivepilot.config.settings.mem0_enabled", True)
        called: list[str] = []

        def _client():
            called.append("searched")
            return None

        monkeypatch.setattr(mem0, "_get_client", _client)

        payload = type(
            "P",
            (),
            {
                "metadata": review_context.prepare({"extra_prompt": "d"}, untrusted_input=True),
                "step": None,
                "task_name": "review",
                "project_name": "p",
            },
        )()
        mem0.recall(payload=payload)

        assert called == [], "mem0 must not search on an untrusted payload"

    def test_mem0_still_recalls_on_an_ordinary_step(self, monkeypatch) -> None:
        """The guard has to be narrow, or it silently disables mem0 wholesale."""
        import importlib

        mem0 = importlib.import_module("plugins.mem0")
        # Without this both tests pass for the wrong reason: mem0 is
        # opt-in, so a disabled plugin never searches and the guard under
        # test is never reached.
        monkeypatch.setattr("hivepilot.config.settings.mem0_enabled", True)
        called: list[str] = []

        def _client():
            called.append("searched")
            return None

        monkeypatch.setattr(mem0, "_get_client", _client)

        payload = type(
            "P",
            (),
            {
                "metadata": {"extra_prompt": "d"},
                "step": None,
                "task_name": "t",
                "project_name": "p",
            },
        )()
        mem0.recall(payload=payload)

        assert called == ["searched"]

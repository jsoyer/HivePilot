"""The engine takes the snapshot, so recall order stops mattering.

`plugins/mem0.py` snapshots `extra_prompt` at the top of its OWN recall
(line 491) so its `store` can persist the task's real ask instead of
re-persisting mem0's own recalled block -- a feedback loop it was right to
close. But the snapshot is taken when MEM0 runs, not before the recall phase,
so whatever another backend appended first is inside it. mem0 then writes
`snapshot + its own block` and obsidian's recall is gone.

That is what forced the `SNAPSHOT` semantics (#533), which refuses mem0
alongside any other recaller -- and therefore refuses mem0 + obsidian, the
combination this deployment actually runs.

The fix is to move the snapshot up: the engine captures the pre-recall value
ONCE per task, before any `before_step` hook fires, and every store reads that
instead of capturing its own. Ordering stops being load-bearing, so a backend
that was only dangerous because of ordering becomes plainly additive again.

Idempotent per metadata dict, because `Orchestrator._execute_task` builds ONE
dict per task and reuses it for every step -- re-capturing on step two would
snapshot step one's recalled memories, which is the very loop being closed.
"""

from __future__ import annotations

from hivepilot.services.memory_kind import (
    INPUT_SNAPSHOT_KEY,
    capture_input_snapshot,
    input_snapshot,
)


class TestTheEngineCapturesOnce:
    def test_it_records_the_pre_recall_value(self):
        metadata = {"extra_prompt": "the operator's actual ask"}

        capture_input_snapshot(metadata)

        assert input_snapshot(metadata) == "the operator's actual ask"

    def test_a_second_capture_does_not_overwrite_the_first(self):
        """The step-two trap: recapturing would snapshot step one's recalled
        memories, which is exactly the feedback loop being closed."""
        metadata = {"extra_prompt": "the ask"}
        capture_input_snapshot(metadata)

        metadata["extra_prompt"] = "the ask\n\nRelevant memories:\n- recalled"
        capture_input_snapshot(metadata)

        assert input_snapshot(metadata) == "the ask"

    def test_an_absent_extra_prompt_snapshots_nothing_not_a_blank(self):
        """None and "" mean different things to a store: 'there was no ask'
        versus 'the ask was empty'. Collapsing them would persist a blank."""
        metadata: dict = {}

        capture_input_snapshot(metadata)

        assert input_snapshot(metadata) is None
        assert INPUT_SNAPSHOT_KEY in metadata

    def test_the_key_is_private_so_no_prompt_ever_renders_it(self):
        """`ClaudeRunner._build_prompt` reads only `extra_prompt` and
        `prior_context`, but a `_`-prefixed key also reads as private to any
        future code that iterates the dict."""
        assert INPUT_SNAPSHOT_KEY.startswith("_")


class TestReadingItBack:
    def test_an_uncaptured_dict_reads_as_none(self):
        assert input_snapshot({}) is None

    def test_it_survives_a_later_recall_appending(self):
        """The whole point: obsidian appends after the capture, and the
        snapshot still holds the original ask."""
        metadata = {"extra_prompt": "the ask"}
        capture_input_snapshot(metadata)

        metadata["extra_prompt"] = "the ask\n\nRelevant vault notes:\n- a note"

        assert input_snapshot(metadata) == "the ask"

    def test_capture_is_safe_on_a_dict_that_already_has_the_key(self):
        metadata = {"extra_prompt": "second", INPUT_SNAPSHOT_KEY: "first"}

        capture_input_snapshot(metadata)

        assert input_snapshot(metadata) == "first"


class TestMem0NoLongerNeedsToBeASnapshotBackend:
    def test_mem0_declares_additive_now_that_the_engine_snapshots(self):
        """With the capture lifted, mem0's append is against a value nobody
        else can have overwritten in the meantime -- so it composes."""
        import importlib.util
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "mem0.py"
        spec = importlib.util.spec_from_file_location("hivepilot_test_mem0_sem", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        from hivepilot.services.memory_kind import RecallSemantics

        assert module.RECALL_SEMANTICS is RecallSemantics.ADDITIVE

    def test_mem0_and_obsidian_compose_again(self):
        from hivepilot.services.memory_kind import (
            MemoryBackend,
            RecallSemantics,
            resolve_composition,
        )

        decision = resolve_composition(
            [
                MemoryBackend(name="mem0", semantics=RecallSemantics.ADDITIVE),
                MemoryBackend(name="obsidian", semantics=RecallSemantics.ADDITIVE),
            ]
        )

        assert decision.allowed is True

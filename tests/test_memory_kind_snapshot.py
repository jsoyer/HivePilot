"""mem0 is neither additive nor exclusive, and reading its code says otherwise.

The composition check shipped with two states. Reality has three, and the
third is the only one that actually bites.

`plugins/mem0.py` recall, line 546:

    metadata[_RECALL_FIELD] = f"{original_extra_prompt}\\n\\n{block}"

That reads as an append, and a maintainer declaring mem0's semantics by
reading it would write ADDITIVE. They would be wrong in the one direction that
matters: `original_extra_prompt` is a SNAPSHOT taken at line 491, not the
current value. If obsidian appended in between, mem0 writes
`snapshot + its own block` and **obsidian's recall is gone**.

So mem0 is additive in form and overwriting in effect, against anything that
wrote after its snapshot. The dangerous part is not the assignment; it is the
interaction between the snapshot and the ordering, which no reading of a
single line reveals.

That is why guessing was rejected -- and it is also why two states were not
enough. Declared ADDITIVE, mem0 + obsidian would be permitted by the check,
and obsidian's block would vanish silently: the check would miss the only real
case it was built for.

SNAPSHOT is therefore refused alongside ANY other recalling backend, until the
snapshot moves into the engine (taken once, before the recall phase, offered
to every store). After that, ordering stops mattering and a snapshot backend
becomes plainly additive -- so this state is a statement about the engine's
current wiring, not about mem0 forever.
"""

from __future__ import annotations

import pytest

from hivepilot.services.memory_kind import (
    MemoryBackend,
    RecallSemantics,
    describe_conflict,
    resolve_composition,
)

ADDITIVE = MemoryBackend(name="obsidian", semantics=RecallSemantics.ADDITIVE)
SNAPSHOT = MemoryBackend(name="mem0", semantics=RecallSemantics.SNAPSHOT)
EXCLUSIVE = MemoryBackend(name="honcho-x", semantics=RecallSemantics.EXCLUSIVE)


class TestTheThirdStateExists:
    def test_snapshot_is_a_declarable_semantics(self):
        assert RecallSemantics("snapshot") is RecallSemantics.SNAPSHOT

    def test_a_snapshot_backend_alone_is_fine(self):
        """mem0 on its own has nothing to clobber."""
        assert resolve_composition([SNAPSHOT]).allowed is True


class TestASnapshotBackendCannotShareTheField:
    def test_snapshot_plus_additive_is_refused(self):
        """The case the two-state check would have WRONGLY allowed: mem0 with
        obsidian silently drops obsidian's block."""
        decision = resolve_composition([SNAPSHOT, ADDITIVE])

        assert decision.allowed is False

    def test_the_refusal_explains_the_snapshot_not_just_the_names(self):
        decision = resolve_composition([SNAPSHOT, ADDITIVE])

        assert "mem0" in decision.reason and "obsidian" in decision.reason
        assert "snapshot" in decision.reason.lower()

    def test_snapshot_plus_exclusive_is_refused(self):
        assert resolve_composition([SNAPSHOT, EXCLUSIVE]).allowed is False

    def test_two_snapshot_backends_are_refused(self):
        other = MemoryBackend(name="other", semantics=RecallSemantics.SNAPSHOT)

        assert resolve_composition([SNAPSHOT, other]).allowed is False

    @pytest.mark.parametrize(
        "backends",
        [[SNAPSHOT, ADDITIVE], [ADDITIVE, SNAPSHOT]],
    )
    def test_the_refusal_does_not_depend_on_declaration_order(self, backends):
        assert resolve_composition(backends).allowed is False


class TestTheOtherRulesAreUnchanged:
    def test_additive_backends_still_compose(self):
        other = MemoryBackend(name="notes", semantics=RecallSemantics.ADDITIVE)

        assert resolve_composition([ADDITIVE, other]).allowed is True

    def test_one_exclusive_with_additives_still_composes(self):
        assert resolve_composition([EXCLUSIVE, ADDITIVE]).allowed is True

    def test_two_exclusives_are_still_refused(self):
        other = MemoryBackend(name="other-x", semantics=RecallSemantics.EXCLUSIVE)

        assert resolve_composition([EXCLUSIVE, other]).allowed is False

    def test_an_empty_set_is_still_allowed(self):
        assert resolve_composition([]).allowed is True


class TestThePairwiseExplanation:
    def test_a_snapshot_pair_is_explained(self):
        message = describe_conflict(SNAPSHOT, ADDITIVE)

        assert message is not None
        assert "mem0" in message

    def test_two_additives_still_explain_nothing(self):
        other = MemoryBackend(name="notes", semantics=RecallSemantics.ADDITIVE)

        assert describe_conflict(ADDITIVE, other) is None

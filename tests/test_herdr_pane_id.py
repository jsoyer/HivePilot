"""The pane id lives under `result`, and the top-level `id` is the RPC envelope.

Measured against herdr 0.7.5 on the box, 2026-08-18. A real `pane split`
answers::

    {"id": "cli:pane:split",
     "result": {"type": "pane_split",
                "pane": {"pane_id": "...", "cwd": "...", ...}}}

`_extract_pane_id` walks `_PANE_ID_KEYS = ("id", "pane_id", "paneId")` against
the TOP-LEVEL dict, so it returns `"cli:pane:split"` -- the JSON-RPC request
id, not a pane. The next call then fails with

    pane run failed on pane cli:pane:split: pane_not_found

which reads like herdr lost the pane it had just created.

This only shows up against a live server. Two runs were needed to see it: the
first never reached herdr at all (a config mistake of mine), and the second
split successfully and then addressed the envelope.

Note what is deliberately NOT changed: the top-level fallback stays, because a
future or older protocol may answer with a bare id, and `_extract_pane_id` is
also called on nested `pane` objects. `result` is simply searched FIRST.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _herdr():
    path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "herdr.py"
    spec = importlib.util.spec_from_file_location("hivepilot_test_herdr_pane", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


REAL_SPLIT = {
    "id": "cli:pane:split",
    "result": {
        "type": "pane_split",
        "pane": {
            "agent_status": "unknown",
            "cwd": "/var/lib/hivepilot/home",
            "focused": False,
            "pane_id": "pane-42",
            "revision": 7,
        },
    },
}


class TestTheEnvelopeIsNotAPaneId:
    def test_a_real_split_response_yields_the_pane_id(self):
        """The defect in one assertion: this returned 'cli:pane:split'."""
        assert _herdr().HerdrRunner._extract_pane_id(REAL_SPLIT) == "pane-42"

    def test_the_rpc_id_is_never_returned(self):
        assert _herdr().HerdrRunner._extract_pane_id(REAL_SPLIT) != "cli:pane:split"

    def test_a_result_without_a_pane_falls_back_to_the_top_level(self):
        """An older or future protocol answering with a bare id must keep
        working -- the fix is 'search result first', not 'ignore the rest'."""
        assert _herdr().HerdrRunner._extract_pane_id({"pane_id": "pane-9"}) == "pane-9"

    def test_a_bare_string_is_still_accepted(self):
        assert _herdr().HerdrRunner._extract_pane_id("pane-3") == "pane-3"

    def test_a_nested_pane_object_still_resolves(self):
        assert _herdr().HerdrRunner._extract_pane_id({"pane": {"pane_id": "p"}}) == "p"

    @pytest.mark.parametrize("payload", [{}, {"result": {}}, {"result": {"pane": {}}}, None, 7])
    def test_nothing_recognisable_returns_none(self, payload):
        """None, so the caller raises with the raw JSON. Constructing a pane id
        would turn a parse failure into a confident call against a pane that
        does not exist."""
        assert _herdr().HerdrRunner._extract_pane_id(payload) is None

    def test_the_envelope_id_is_not_used_even_when_result_is_empty(self):
        """The sharpest case: `result` present but useless. Falling back to the
        top-level `id` here is exactly how the envelope was mistaken for a
        pane."""
        assert _herdr().HerdrRunner._extract_pane_id({"id": "cli:pane:split", "result": {}}) is None

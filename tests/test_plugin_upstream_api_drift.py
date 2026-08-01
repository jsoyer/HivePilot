"""Both vendored memory plugins called upstream APIs that had moved on.

Found by running a real pipeline against `headroom-ai 0.32.1` / `mem0ai 2.0.14`:
`headroom_compressions` had sat at 0 and `memory_events` at 1 for weeks, while
`hivepilot plugins health` reported both `ok`. Neither was starved of work —
both were failing on every single call, invisibly:

- `headroom.compress` takes chat MESSAGES (`list[dict]`). Passing a bare string
  makes it fail internally with ``'str' object has no attribute 'get'``, which
  it swallows by "returning the original messages" — so the caller sees a
  successful no-op.
- `mem0` 2.x rejects top-level entity scoping: *Top-level entity parameters
  frozenset({'user_id'}) are not supported in search(). Use
  filters={'user_id': '...'} instead*. Recall caught and logged that, so it
  never surfaced either.

Both fixes stay tolerant of the older signatures so a pinned deployment keeps
working.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def _load(name: str) -> ModuleType:
    """Load a plugin file by path — they are not importable as a package."""
    spec = importlib.util.spec_from_file_location(f"_drift_{name}", PLUGINS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# headroom — compress() takes messages, not a string
# ---------------------------------------------------------------------------


@pytest.fixture
def headroom_mod():
    return _load("headroom")


def test_text_is_wrapped_as_a_message_and_unwrapped(headroom_mod, monkeypatch):
    seen = {}

    def fake_compress(messages, model=None):
        seen["messages"] = messages
        return SimpleNamespace(messages=[{"role": "user", "content": "short"}])

    monkeypatch.setattr(headroom_mod, "compress", fake_compress)

    out = headroom_mod._compress_text("a very long blob", "opus")

    assert out == "short"
    assert isinstance(seen["messages"], list), "compress must receive messages, never a bare str"
    assert seen["messages"][0]["content"] == "a very long blob"


def test_a_string_only_headroom_still_works(headroom_mod, monkeypatch):
    """Older headroom: string in, string out. Must not regress."""

    def legacy_compress(arg, model=None):
        if not isinstance(arg, str):
            raise AttributeError("'list' object has no attribute 'split'")
        return "compressed"

    monkeypatch.setattr(headroom_mod, "compress", legacy_compress)
    assert headroom_mod._compress_text("long text", None) == "compressed"


def test_an_unusable_result_leaves_the_original_alone(headroom_mod, monkeypatch):
    monkeypatch.setattr(headroom_mod, "compress", lambda *a, **k: SimpleNamespace(messages=[]))
    assert headroom_mod._compress_text("long text", None) is None


def test_a_failing_compressor_never_raises(headroom_mod, monkeypatch):
    def boom(*a, **k):
        raise TypeError("signature changed again")

    monkeypatch.setattr(headroom_mod, "compress", boom)
    # First shape raises TypeError -> legacy retry also raises -> None, no crash.
    assert headroom_mod._compress_text("long text", None) is None


# ---------------------------------------------------------------------------
# mem0 — entity scoping moved into filters=
# ---------------------------------------------------------------------------


@pytest.fixture
def mem0_mod():
    return _load("mem0")


def test_scoping_uses_the_2x_filters_shape(mem0_mod):
    method = MagicMock(return_value={"results": []})

    mem0_mod._call_scoped(method, "proj:task:developer", "the query")

    assert method.call_args.kwargs["filters"] == {"user_id": "proj:task:developer"}
    assert "user_id" not in method.call_args.kwargs, "2.x rejects top-level user_id"


def test_it_falls_back_to_the_1x_shape(mem0_mod):
    """A deployment pinned to mem0 1.x must keep working."""
    calls = []

    def legacy(*args, **kwargs):
        calls.append(kwargs)
        if "filters" in kwargs:
            raise TypeError("unexpected keyword argument 'filters'")
        return {"results": []}

    mem0_mod._call_scoped(legacy, "proj:task", "q")

    assert calls[-1]["user_id"] == "proj:task"


def test_other_errors_are_not_swallowed_by_the_retry(mem0_mod):
    """Only the scoping shape is retried — a real failure must propagate."""

    def boom(*args, **kwargs):
        raise RuntimeError("mem0 is down")

    with pytest.raises(RuntimeError, match="mem0 is down"):
        mem0_mod._call_scoped(boom, "k", "q")


def test_extra_kwargs_survive_both_shapes(mem0_mod):
    method = MagicMock(return_value={})
    mem0_mod._call_scoped(method, "k", "content", metadata={"category": "decision"})
    assert method.call_args.kwargs["metadata"] == {"category": "decision"}

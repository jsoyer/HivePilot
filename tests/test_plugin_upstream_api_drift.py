"""Vendored plugins called upstream APIs that had moved on.

Found by running a real pipeline against `headroom-ai 0.32.1`:
`headroom_compressions` had sat at 0 for weeks, while `hivepilot plugins
health` reported `ok`. The plugin was not starved of work — it was failing
on every single call, invisibly:

- `headroom.compress` takes chat MESSAGES (`list[dict]`). Passing a bare string
  makes it fail internally with ``'str' object has no attribute 'get'``, which
  it swallows by "returning the original messages" — so the caller sees a
  successful no-op.

The fix stays tolerant of the older signature so a pinned deployment keeps
working.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType, SimpleNamespace

import pytest
from conftest import BUNDLED_PLUGINS

PLUGINS = BUNDLED_PLUGINS


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

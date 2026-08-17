"""honcho as a memory backend: a model of a ROLE over time, not a fact store.

Read from honcho's own docs (honcho.dev + plastic-labs/honcho README,
2026-08-17). It is not mem0 with a different name:

- mem0 extracts and returns FACTS;
- honcho ingests MESSAGES and returns **Representations** -- conclusions
  derived about a *Peer*, an entity that persists and changes over time.

The natural Peer here is a **role**. How Victor reviews, how Hugo audits, and
how that drifts across runs, is a different question from "what did we decide
about worktrees" -- which is why enabling both is legitimate rather than
redundant.

Two contract points this file pins:

**Semantics are ADDITIVE.** `recall` appends to `extra_prompt` and never
replaces it, so honcho composes with mem0 and obsidian instead of racing them.
`memory_kind.resolve_composition` rules on the declaration, so declaring it
wrongly would silently permit a combination that destroys another backend's
recall.

**Dormant unless configured.** Every hook is best-effort and must never break
a run: no package, no API key, or a raising client all degrade to doing
nothing. A memory backend that can fail a pipeline is worse than no memory.

⚠️ The package is `honcho-ai`. Plain `honcho` on PyPI is an unrelated Procfile
runner -- installing that and importing `honcho` would import something else
entirely, which is exactly how a plugin ends up silently inert.
"""

from __future__ import annotations

import sys
import types

import pytest

from hivepilot.services.memory_kind import RecallSemantics, resolve_composition


@pytest.fixture
def honcho_module(monkeypatch):
    """Import plugins/honcho.py with a fake `honcho` package installed."""
    calls: dict = {"messages": [], "chats": []}

    class _Peer:
        def __init__(self, pid):
            self.id = pid

        def message(self, text):
            return {"peer": self.id, "text": text}

        def chat(self, question):
            calls["chats"].append((self.id, question))
            return "Victor asks for changes when tests are missing."

    class _Session:
        def __init__(self, sid):
            self.id = sid

        def add_messages(self, messages):
            calls["messages"].extend(messages)

    class _Honcho:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def peer(self, pid):
            return _Peer(pid)

        def session(self, sid):
            return _Session(sid)

    fake = types.ModuleType("honcho")
    fake.Honcho = _Honcho  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "honcho", fake)
    # A CONFIGURED deployment: without a key or a self-hosted URL the plugin
    # correctly declines to build a client, which is its dormant-by-default
    # contract and not what these tests are about.
    monkeypatch.setenv("HONCHO_API_KEY", "test-key")
    monkeypatch.setenv("HIVEPILOT_HONCHO_WORKSPACE", "hivepilot-test")

    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "honcho.py"
    spec = importlib.util.spec_from_file_location("hivepilot_test_honcho", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._TEST_CALLS = calls  # type: ignore[attr-defined]
    return module


class TestItDeclaresItsSemantics:
    def test_it_is_additive(self, honcho_module):
        assert honcho_module.RECALL_SEMANTICS is RecallSemantics.ADDITIVE

    def test_it_therefore_composes_with_an_exclusive_backend(self, honcho_module):
        from hivepilot.services.memory_kind import MemoryBackend

        decision = resolve_composition(
            [
                MemoryBackend(name="honcho", semantics=honcho_module.RECALL_SEMANTICS),
                MemoryBackend(name="mem0", semantics=RecallSemantics.EXCLUSIVE),
            ]
        )

        assert decision.allowed is True

    def test_it_registers_the_memory_hooks(self, honcho_module, monkeypatch):
        """With the flag ON. Off, `register()` must return {} -- a disabled
        backend contributes nothing rather than sitting in the hook chain
        no-opping, which is what `test_gating_conformance` pins."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)

        registered = honcho_module.register()

        assert "before_step" in registered
        assert "after_step" in registered


class TestRecallAppendsAndNeverOverwrites:
    def test_it_appends_below_existing_context(self, honcho_module, monkeypatch):
        """The contract that makes it composable: another backend's block
        survives underneath ours."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)
        metadata = {"extra_prompt": "Relevant memories:\n- from mem0"}

        honcho_module.recall(payload=_payload(metadata), role="reviewer")

        assert metadata["extra_prompt"].startswith("Relevant memories:")
        assert "Victor asks for changes" in metadata["extra_prompt"]

    def test_it_writes_nothing_when_there_is_no_context_key(self, honcho_module, monkeypatch):
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)
        metadata: dict = {}

        honcho_module.recall(payload=_payload(metadata), role="reviewer")

        assert honcho_module._RECALL_FIELD in metadata

    def test_it_recalls_once_per_shared_metadata_dict(self, honcho_module, monkeypatch):
        """`Orchestrator._execute_task` reuses ONE metadata dict for every step
        of a task. Without a sentinel the same representation is appended on
        every step -- headroom's and mem0's exact problem."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)
        metadata: dict = {}
        payload = _payload(metadata)

        honcho_module.recall(payload=payload, role="reviewer")
        honcho_module.recall(payload=payload, role="reviewer")

        assert len(honcho_module._TEST_CALLS["chats"]) == 1

    def test_the_peer_is_the_role(self, honcho_module, monkeypatch):
        """The whole reason honcho is not redundant with mem0: it models the
        role, not the facts."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)

        honcho_module.recall(payload=_payload({}), role="ciso")

        assert honcho_module._TEST_CALLS["chats"][0][0] == "ciso"


class TestStoreFeedsTheSession:
    def test_the_step_output_becomes_a_message(self, honcho_module, monkeypatch):
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)

        honcho_module.store(payload=_payload({}), role="reviewer", output="I request changes.")

        assert any("I request changes." in m["text"] for m in honcho_module._TEST_CALLS["messages"])

    def test_an_empty_output_stores_nothing(self, honcho_module, monkeypatch):
        """A step that produced nothing teaches nothing, and an empty message
        would still cost a reasoning pass on honcho's side."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)

        honcho_module.store(payload=_payload({}), role="reviewer", output="   ")

        assert honcho_module._TEST_CALLS["messages"] == []


class TestItIsDormantAndNeverBreaksARun:
    def test_recall_does_nothing_when_disabled(self, honcho_module, monkeypatch):
        monkeypatch.setattr(honcho_module, "_enabled", lambda: False)
        metadata: dict = {}

        honcho_module.recall(payload=_payload(metadata), role="reviewer")

        assert metadata == {}

    def test_a_raising_client_never_propagates(self, honcho_module, monkeypatch):
        """Best-effort, like every other memory hook: a backend outage must
        not fail a pipeline that otherwise succeeded."""
        monkeypatch.setattr(honcho_module, "_enabled", lambda: True)
        monkeypatch.setattr(
            honcho_module,
            "_client",
            lambda: (_ for _ in ()).throw(RuntimeError("honcho unreachable")),
        )

        honcho_module.recall(payload=_payload({}), role="reviewer")
        honcho_module.store(payload=_payload({}), role="reviewer", output="x")

    def test_health_reports_without_raising(self, honcho_module):
        status = honcho_module.health()

        assert status is not None


def _payload(metadata: dict):
    class _P:
        project_name = "noxys"
        task_name = "noxys-reviewer"

        def __init__(self, md):
            self.metadata = md

    return _P(metadata)

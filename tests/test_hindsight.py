"""Hindsight as a memory backend: retain/recall over an HTTP client (HP-51).

Hindsight is a world-fact store the operator deploys (Postgres/pgvector +
LLM). HivePilot is the HTTP client. This file pins the same two contracts
as honcho:

**Semantics are ADDITIVE.** ``recall`` appends to ``extra_prompt``.
**Dormant unless configured.** Missing extra / disabled flag / raising
client never break a run.
"""

from __future__ import annotations

import sys
import types

import pytest
from conftest import BUNDLED_PLUGINS

from hivepilot.services.memory_kind import MemoryBackend, RecallSemantics, resolve_composition


@pytest.fixture
def hindsight_module(monkeypatch):
    calls: dict = {"recalls": [], "retains": [], "init": None}

    class _Hindsight:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def recall(self, *, bank_id, query):
            calls["recalls"].append((bank_id, query))
            return [{"text": "The API lives on loopback :8888"}]

        def retain(self, *, bank_id, content):
            calls["retains"].append((bank_id, content))

    fake = types.ModuleType("hindsight_client")
    fake.Hindsight = _Hindsight  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hindsight_client", fake)

    import importlib.util

    path = BUNDLED_PLUGINS / "hindsight.py"
    spec = importlib.util.spec_from_file_location("hivepilot_test_hindsight", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._TEST_CALLS = calls  # type: ignore[attr-defined]
    return module


def _payload(metadata: dict, *, task: str = "docs"):
    class _Step:
        name = "write"

    class _P:
        project_name = "example-api"
        task_name = task
        step = _Step()

        def __init__(self, md):
            self.metadata = md

    return _P(metadata)


class TestItDeclaresItsSemantics:
    def test_it_is_additive(self, hindsight_module):
        assert hindsight_module.RECALL_SEMANTICS is RecallSemantics.ADDITIVE

    def test_it_composes_with_honcho(self, hindsight_module):
        decision = resolve_composition(
            [
                MemoryBackend(name="hindsight", semantics=hindsight_module.RECALL_SEMANTICS),
                MemoryBackend(name="honcho", semantics=RecallSemantics.ADDITIVE),
            ]
        )
        assert decision.allowed is True

    def test_it_registers_hooks_when_enabled(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        registered = hindsight_module.register()
        assert "before_step" in registered
        assert "after_step" in registered

    def test_register_is_empty_when_disabled(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: False)
        assert hindsight_module.register() == {}


class TestRecallAppendsAndNeverOverwrites:
    def test_it_appends_below_existing_context(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        metadata = {"extra_prompt": "Relevant memories:\n- from mem0"}

        hindsight_module.recall(payload=_payload(metadata), role="docs")

        assert metadata["extra_prompt"].startswith("Relevant memories:")
        assert "loopback :8888" in metadata["extra_prompt"]
        assert "hindsight" in metadata["extra_prompt"]

    def test_it_recalls_once_per_shared_metadata_dict(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        metadata: dict = {}
        payload = _payload(metadata)
        hindsight_module.recall(payload=payload, role="docs")
        hindsight_module.recall(payload=payload, role="docs")
        assert len(hindsight_module._TEST_CALLS["recalls"]) == 1

    def test_bank_is_project_task_role(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        hindsight_module.recall(payload=_payload({}), role="reviewer")
        assert hindsight_module._TEST_CALLS["recalls"][0][0] == "example-api:docs:reviewer"


class TestStoreRetainsRedactedOutput:
    def test_the_step_output_is_retained(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        hindsight_module.store(payload=_payload({}), role="docs", output="Shipped the endpoint.")
        assert hindsight_module._TEST_CALLS["retains"]
        bank, content = hindsight_module._TEST_CALLS["retains"][0]
        assert bank == "example-api:docs:docs"
        assert "Shipped the endpoint." in content

    def test_empty_output_retains_nothing(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        hindsight_module.store(payload=_payload({}), role="docs", output="   ")
        assert hindsight_module._TEST_CALLS["retains"] == []


class TestItIsDormantAndNeverBreaksARun:
    def test_recall_does_nothing_when_disabled(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: False)
        metadata: dict = {}
        hindsight_module.recall(payload=_payload(metadata), role="docs")
        assert metadata == {}

    def test_a_raising_client_never_propagates(self, hindsight_module, monkeypatch):
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        monkeypatch.setattr(
            hindsight_module,
            "_client",
            lambda: (_ for _ in ()).throw(RuntimeError("hindsight down")),
        )
        hindsight_module.recall(payload=_payload({}), role="docs")
        hindsight_module.store(payload=_payload({}), role="docs", output="x")

    def test_health_reports_without_raising(self, hindsight_module):
        assert hindsight_module.health() is not None


class TestLeavesHost:
    def test_loopback_is_not_egress(self, hindsight_module):
        assert hindsight_module.leaves_host("http://127.0.0.1:8888") is False
        assert hindsight_module.leaves_host("http://localhost:8888") is False

    def test_cloud_is_egress(self, hindsight_module):
        assert hindsight_module.leaves_host("https://api.hindsight.vectorize.io") is True


class TestExtractTexts:
    def test_list_of_dicts(self, hindsight_module):
        assert hindsight_module._extract_texts([{"text": "a"}, {"content": "b"}]) == ["a", "b"]

    def test_bare_string(self, hindsight_module):
        assert hindsight_module._extract_texts("just a fact") == ["just a fact"]


class TestInstrumentation:
    def test_recall_records_search(self, hindsight_module, monkeypatch, tmp_path):
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        from hivepilot.services import memory_service

        hindsight_module.recall(payload=_payload({}), role="docs", run_id="run-1")
        stats = memory_service.backend_stats(days=30)
        assert stats["hindsight"]["searches"] == 1

    def test_store_records_write(self, hindsight_module, monkeypatch, tmp_path):
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        monkeypatch.setattr(hindsight_module, "_enabled", lambda: True)
        from hivepilot.services import memory_service

        hindsight_module.store(payload=_payload({}), role="docs", output="kept")
        stats = memory_service.backend_stats(days=30)
        assert stats["hindsight"]["stores"] == 1

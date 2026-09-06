"""HP-55: Hindsight role-bank panel (mental models + observations)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from hivepilot.services import hindsight_panel as panel
from hivepilot.services.hindsight_role_sync import role_bank_id


@dataclass
class _RecordingClient:
    calls: list = field(default_factory=list)
    models: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    fail_models: Exception | None = None
    fail_memories: Exception | None = None
    fail_mutate: Exception | None = None
    created: object | None = None
    updated: object | None = None

    def list_mental_models(self, bank_id: str, **kwargs):
        self.calls.append(("list_mental_models", {"bank_id": bank_id, **kwargs}))
        if self.fail_models:
            raise self.fail_models
        return {"items": self.models}

    def create_mental_model(self, bank_id: str, **kwargs):
        self.calls.append(("create_mental_model", {"bank_id": bank_id, **kwargs}))
        if self.fail_mutate:
            raise self.fail_mutate
        return self.created or {"id": "mm-1", "name": kwargs.get("name"), **kwargs}

    def update_mental_model(self, bank_id: str, mental_model_id: str, **kwargs):
        self.calls.append(
            ("update_mental_model", {"bank_id": bank_id, "id": mental_model_id, **kwargs})
        )
        if self.fail_mutate:
            raise self.fail_mutate
        return self.updated or {"id": mental_model_id, **kwargs}

    def refresh_mental_model(self, bank_id: str, mental_model_id: str):
        self.calls.append(("refresh_mental_model", {"bank_id": bank_id, "id": mental_model_id}))
        if self.fail_mutate:
            raise self.fail_mutate
        return {"operation_id": "op-9"}

    def list_memories(self, bank_id: str, **kwargs):
        self.calls.append(("list_memories", {"bank_id": bank_id, **kwargs}))
        if self.fail_memories:
            raise self.fail_memories
        return SimpleNamespace(items=self.memories)

    def update_memory(self, bank_id: str, memory_id: str, **kwargs):
        self.calls.append(("update_memory", {"bank_id": bank_id, "id": memory_id, **kwargs}))
        if self.fail_mutate:
            raise self.fail_mutate
        return {"id": memory_id, "text": kwargs.get("text"), "fact_type": "world"}


class TestParsers:
    def test_mental_model_from_object(self):
        raw = SimpleNamespace(
            id="prefs",
            name="User Preferences",
            source_query="What does the user prefer?",
            content="Prefers dark mode.",
            last_refreshed_at="2026-09-01T00:00:00Z",
            is_stale=True,
            tags=["ui"],
        )
        parsed = panel.parse_mental_model(raw)
        assert parsed["id"] == "prefs"
        assert parsed["name"] == "User Preferences"
        assert parsed["source_query"].startswith("What")
        assert parsed["content"] == "Prefers dark mode."
        assert parsed["is_stale"] is True
        assert parsed["tags"] == ["ui"]

    def test_observation_extracts_quotes_and_source_facts(self):
        raw = {
            "id": "obs-1",
            "text": "The user prefers dark mode.",
            "fact_type": "observation",
            "proof_count": 2,
            "confidence": 0.81,
            "proofs": [
                {"text": "use dark theme", "id": "w1"},
                {"text": "night palette please", "id": "e1"},
            ],
            "evidence": [
                {"id": "w1", "text": "use dark theme", "fact_type": "world", "state": "valid"},
                {"id": "obs-skip", "text": "derived", "fact_type": "observation"},
            ],
        }
        parsed = panel.parse_observation(raw)
        assert parsed["id"] == "obs-1"
        assert parsed["proof_count"] == 2
        assert parsed["confidence"] == pytest.approx(0.81)
        assert [q["text"] for q in parsed["quotes"]] == ["use dark theme", "night palette please"]
        assert [e["id"] for e in parsed["evidence"]] == ["w1", "e1"]

    def test_observation_proof_count_falls_back_to_quote_len(self):
        parsed = panel.parse_observation({"id": "o", "text": "x", "quotes": ["a", "b"]})
        assert parsed["proof_count"] == 2


class TestRoleResolution:
    def test_bank_id_stays_role_colon_name(self):
        assert role_bank_id("developer") == "role:developer"

    def test_known_role_resolves(self):
        assert panel.resolve_role("developer") == "developer"

    def test_unknown_role_raises(self):
        with pytest.raises(panel.UnknownRole):
            panel.resolve_role("not-a-real-role-xyz")

    def test_path_like_role_rejected(self):
        with pytest.raises(panel.UnknownRole):
            panel.resolve_role("../admin")


class TestStatusAndPanel:
    def test_status_disabled_still_lists_roles(self):
        data = panel.panel_status(enabled=False, client=None)
        assert data["configured"] is False
        assert data["detail"]
        names = {row["name"] for row in data["roles"]}
        assert "developer" in names
        assert all(row["bank_id"] == f"role:{row['name']}" for row in data["roles"])

    def test_role_panel_disabled_does_not_call_client(self):
        client = _RecordingClient()
        data = panel.role_panel("developer", client=client, enabled=False)
        assert data["configured"] is False
        assert data["bank_id"] == "role:developer"
        assert data["mental_models"] == []
        assert data["observations"] == []
        assert client.calls == []

    def test_role_panel_lists_models_and_observations(self):
        client = _RecordingClient(
            models=[{"id": "mm", "name": "Prefs", "source_query": "q", "content": "c"}],
            memories=[
                {"id": "o1", "text": "dark mode", "fact_type": "observation", "proofs": ["p"]}
            ],
        )
        data = panel.role_panel("developer", client=client, enabled=True)
        assert data["configured"] is True
        assert data["role"] == "developer"
        assert data["bank_id"] == "role:developer"
        assert data["mental_models"][0]["name"] == "Prefs"
        assert data["observations"][0]["text"] == "dark mode"
        assert ("list_mental_models", {"bank_id": "role:developer", "detail": "content"}) in [
            (name, payload) for name, payload in client.calls
        ]
        mem_calls = [p for name, p in client.calls if name == "list_memories"]
        assert mem_calls[0]["type"] == "observation"

    def test_role_panel_partial_failure_still_returns(self):
        client = _RecordingClient(
            fail_models=RuntimeError("down"),
            memories=[{"id": "o1", "text": "kept"}],
        )
        data = panel.role_panel("developer", client=client, enabled=True)
        assert data["configured"] is True
        assert data["mental_models"] == []
        assert data["observations"][0]["id"] == "o1"
        assert "mental_models" in data["detail"]


class TestMutations:
    def test_create_mental_model_forwards_bank(self):
        client = _RecordingClient()
        result = panel.create_mental_model(
            "developer",
            name="Prefs",
            source_query="What does the user prefer?",
            client=client,
            enabled=True,
        )
        assert result["ok"] is True
        assert result["bank_id"] == "role:developer"
        assert client.calls[0][0] == "create_mental_model"
        assert client.calls[0][1]["name"] == "Prefs"

    def test_update_mental_model_requires_a_field(self):
        client = _RecordingClient()
        with pytest.raises(panel.PanelError) as exc:
            panel.update_mental_model("developer", "mm", client=client, enabled=True)
        assert exc.value.status_code == 400

    def test_refresh_returns_operation_id(self):
        client = _RecordingClient()
        result = panel.refresh_mental_model("developer", "mm", client=client, enabled=True)
        assert result["operation_id"] == "op-9"

    def test_curate_memory_edits_source_fact(self):
        client = _RecordingClient()
        result = panel.curate_memory(
            "developer",
            "w1",
            text="Prefers dark mode.",
            reason="wrong subject",
            client=client,
            enabled=True,
        )
        assert result["ok"] is True
        assert client.calls[0][1]["text"] == "Prefers dark mode."
        assert client.calls[0][1]["reason"] == "wrong subject"

    def test_mutation_disabled_is_503(self):
        with pytest.raises(panel.PanelError) as exc:
            panel.create_mental_model(
                "developer", name="x", source_query="y", client=_RecordingClient(), enabled=False
            )
        assert exc.value.status_code == 503

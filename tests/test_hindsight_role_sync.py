"""HP-52: each role maps onto a Hindsight identity bank.

Mission = prompt text. Directives = get_rules_for_role() (paths rewritten,
prose kept). Disposition is not a Role field and must never be sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hivepilot.agent_rules import ENGINE_DEFAULT_CROSS_CUTTING_RULES
from hivepilot.services import hindsight_role_sync as sync


@dataclass
class _FakeRole:
    name: str
    prompt_file: Path


@dataclass
class _RecordingAdapter:
    calls: list = field(default_factory=list)
    existing: list = field(default_factory=list)

    def ensure_bank(self, bank_id: str, *, reflect_mission: str) -> None:
        self.calls.append(("ensure_bank", {"bank_id": bank_id, "reflect_mission": reflect_mission}))

    def upsert_config(self, bank_id: str, *, reflect_mission: str) -> None:
        self.calls.append(
            ("upsert_config", {"bank_id": bank_id, "reflect_mission": reflect_mission})
        )

    def list_directives(self, bank_id: str):
        self.calls.append(("list_directives", {"bank_id": bank_id}))
        return list(self.existing)

    def create_directive(self, bank_id: str, *, name: str, content: str) -> None:
        self.calls.append(
            ("create_directive", {"bank_id": bank_id, "name": name, "content": content})
        )

    def delete_directive(self, bank_id: str, directive_id: str) -> None:
        self.calls.append(("delete_directive", {"bank_id": bank_id, "directive_id": directive_id}))


@dataclass
class _RecordingSdk:
    """Looks like hindsight_client.Hindsight — used to prove kwargs."""

    calls: list = field(default_factory=list)

    def create_bank(self, **kwargs):
        self.calls.append(("create_bank", kwargs))

    def update_bank_config(self, bank_id, **kwargs):
        self.calls.append(("update_bank_config", {"bank_id": bank_id, **kwargs}))

    def list_directives(self, bank_id, **kwargs):
        self.calls.append(("list_directives", {"bank_id": bank_id, **kwargs}))
        return {"items": []}

    def create_directive(self, bank_id, **kwargs):
        self.calls.append(("create_directive", {"bank_id": bank_id, **kwargs}))

    def delete_directive(self, bank_id, directive_id):
        self.calls.append(("delete_directive", {"bank_id": bank_id, "directive_id": directive_id}))


def _developer_role() -> _FakeRole:
    from hivepilot.roles import _DEFAULT_ROLES

    live = _DEFAULT_ROLES["developer"]
    return _FakeRole(name=live.name, prompt_file=live.prompt_file)


class TestPayloadMapping:
    def test_bank_id_is_role_colon_name(self):
        assert sync.role_bank_id("developer") == "role:developer"

    def test_developer_mission_is_the_prompt_file(self):
        role = _developer_role()
        payload = sync.build_role_payload(role, rules=[])
        assert payload.bank_id == "role:developer"
        assert "Implement features and fixes" in payload.reflect_mission
        assert payload.reflect_mission.startswith("# Developer")

    def test_path_rule_becomes_must_read(self):
        assert (
            sync.directive_from_rule("/tmp/CLAUDE.md") == "MUST read before acting: /tmp/CLAUDE.md"
        )
        assert sync.is_rule_path("/tmp/CLAUDE.md") is True

    def test_prose_rule_is_copied_as_is(self):
        prose = ENGINE_DEFAULT_CROSS_CUTTING_RULES[0]
        assert sync.directive_from_rule(prose) == prose
        assert sync.is_rule_path(prose) is False

    def test_unconfigured_includes_engine_default_cross_cutting(self, monkeypatch):
        monkeypatch.setattr(sync, "role_prompt_text", lambda role: "You are Gustave.")
        monkeypatch.setattr(
            "hivepilot.agent_rules.get_rules_for_role",
            lambda name: list(ENGINE_DEFAULT_CROSS_CUTTING_RULES),
        )
        payload = sync.build_role_payload(_developer_role())
        assert ENGINE_DEFAULT_CROSS_CUTTING_RULES[0] in payload.directives

    def test_mixed_rules_keep_order(self):
        rules = [
            "/vault/AGENTS.md",
            ENGINE_DEFAULT_CROSS_CUTTING_RULES[0],
            "  ",
            "./prompts/agents/developer.md",
        ]
        payload = sync.build_role_payload(_developer_role(), rules=rules)
        assert payload.directives == (
            "MUST read before acting: /vault/AGENTS.md",
            ENGINE_DEFAULT_CROSS_CUTTING_RULES[0],
            "MUST read before acting: ./prompts/agents/developer.md",
        )


class TestSyncCalls:
    def test_disabled_flag_makes_zero_calls(self):
        adapter = _RecordingAdapter()
        result = sync.sync_role_to_hindsight(_developer_role(), client=adapter, enabled=False)
        assert result is None
        assert adapter.calls == []
        assert (
            sync.sync_all_roles({"developer": _developer_role()}, client=adapter, enabled=False)
            == 0
        )
        assert adapter.calls == []

    def test_sync_upserts_mission_and_directives_never_disposition(self, monkeypatch):
        adapter = _RecordingAdapter()
        role = _developer_role()
        rules = ["/tmp/CLAUDE.md", ENGINE_DEFAULT_CROSS_CUTTING_RULES[0]]
        payload = sync.build_role_payload(role, rules=rules)

        monkeypatch.setattr(sync, "build_role_payload", lambda r, rules=None: payload)
        result = sync.sync_role_to_hindsight(role, client=adapter, enabled=True)

        assert result is payload
        kinds = [name for name, _ in adapter.calls]
        assert "ensure_bank" in kinds
        assert "upsert_config" in kinds
        assert "create_directive" in kinds
        for _name, kwargs in adapter.calls:
            assert "disposition" not in kwargs
            assert not any(key.startswith("disposition") for key in kwargs)

        missions = [
            kwargs["reflect_mission"]
            for name, kwargs in adapter.calls
            if "reflect_mission" in kwargs
        ]
        assert missions
        assert all(m == payload.reflect_mission for m in missions)

        created = [
            kwargs["content"] for name, kwargs in adapter.calls if name == "create_directive"
        ]
        assert "MUST read before acting: /tmp/CLAUDE.md" in created
        assert ENGINE_DEFAULT_CROSS_CUTTING_RULES[0] in created

    def test_sdk_adapter_never_forwards_disposition_kwargs(self):
        raw = _RecordingSdk()
        adapter = sync.SdkHindsightBankClient(raw)
        role = _developer_role()
        sync.sync_role_to_hindsight(role, client=adapter, enabled=True)
        assert raw.calls
        for _name, kwargs in raw.calls:
            assert "disposition" not in kwargs
            assert "disposition_skepticism" not in kwargs
            assert "disposition_literalism" not in kwargs
            assert "disposition_empathy" not in kwargs
            assert "mission" not in kwargs  # deprecated alias — we send reflect_mission

    def test_stale_managed_directive_is_deleted(self):
        stale = sync.ManagedDirective(
            directive_id="dir-old",
            name=sync.managed_directive_name("obsolete rule"),
            content="obsolete rule",
        )
        adapter = _RecordingAdapter(existing=[stale])
        payload = sync.RoleBankPayload(
            bank_id="role:developer",
            role_name="developer",
            reflect_mission="You are Gustave.",
            directives=("keep this",),
        )
        sync._reconcile_directives(adapter, payload)
        created = [kwargs for name, kwargs in adapter.calls if name == "create_directive"]
        deleted = [kwargs for name, kwargs in adapter.calls if name == "delete_directive"]
        assert created[0]["content"] == "keep this"
        assert deleted == [{"bank_id": "role:developer", "directive_id": "dir-old"}]

    def test_idempotent_refresh_does_not_recreate(self):
        content = "Privacy-by-design: never log or surface raw prompt content."
        existing = sync.ManagedDirective(
            directive_id="dir-1",
            name=sync.managed_directive_name(content),
            content=content,
        )
        adapter = _RecordingAdapter(existing=[existing])
        payload = sync.RoleBankPayload(
            bank_id="role:developer",
            role_name="developer",
            reflect_mission="You are Gustave.",
            directives=(content,),
        )
        sync._reconcile_directives(adapter, payload)
        kinds = [name for name, _ in adapter.calls]
        assert "create_directive" not in kinds
        assert "delete_directive" not in kinds


class TestRefreshRolesWiresSync:
    def test_successful_refresh_calls_sync(self, tmp_path, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module

        prompt_file = tmp_path / "tester.md"
        prompt_file.write_text("You are a tester.")
        roles_path = tmp_path / "roles.yaml"
        roles_path.write_text(
            f"""
roles:
  - name: tester
    title: Tester
    prompt_file: {prompt_file.name}
    model_profile: coding
    inputs: []
    outputs: []
    can_block: false
    order: 1
"""
        )
        seen: list[list[str]] = []

        def _capture(roles, **kwargs):  # noqa: ARG001
            seen.append(sorted(roles.keys()))
            return len(roles)

        monkeypatch.setattr(
            "hivepilot.services.hindsight_role_sync.sync_all_roles",
            _capture,
        )
        monkeypatch.setattr(roles_module, "_load_roles_live", roles_module._load_roles_strict)
        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            config_module.settings = type(
                "MockSettings",
                (),
                {
                    "roles_file": roles_path,
                    "resolve_config_path": lambda self, f: roles_path,
                },
            )()
            assert roles_module.refresh_roles() is True
            assert seen == [["tester"]]
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles

    def test_failed_refresh_does_not_sync(self, tmp_path, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module

        calls: list[int] = []
        monkeypatch.setattr(
            "hivepilot.services.hindsight_role_sync.sync_all_roles",
            lambda *a, **k: calls.append(1),
        )
        monkeypatch.setattr(
            roles_module,
            "_load_roles_live",
            lambda: (_ for _ in ()).throw(FileNotFoundError("missing")),
        )
        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            config_module.settings = type(
                "MockSettings",
                (),
                {
                    "roles_file": tmp_path / "missing.yaml",
                    "resolve_config_path": lambda self, f: tmp_path / "missing.yaml",
                },
            )()
            assert roles_module.refresh_roles() is False
            assert calls == []
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles

    def test_raising_sync_does_not_fail_refresh(self, tmp_path, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module

        prompt_file = tmp_path / "tester.md"
        prompt_file.write_text("You are a tester.")
        roles_path = tmp_path / "roles.yaml"
        roles_path.write_text(
            f"""
roles:
  - name: tester
    title: Tester
    prompt_file: {prompt_file.name}
    model_profile: coding
    inputs: []
    outputs: []
    can_block: false
    order: 1
"""
        )

        def _boom(*_a, **_k):
            raise RuntimeError("hindsight down")

        monkeypatch.setattr("hivepilot.services.hindsight_role_sync.sync_all_roles", _boom)
        monkeypatch.setattr(roles_module, "_load_roles_live", roles_module._load_roles_strict)
        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            config_module.settings = type(
                "MockSettings",
                (),
                {
                    "roles_file": roles_path,
                    "resolve_config_path": lambda self, f: roles_path,
                },
            )()
            assert roles_module.refresh_roles() is True
            assert "tester" in roles_module.ROLES
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles


class TestParseDirectives:
    def test_items_dict(self):
        items = sync._parse_directives(
            {
                "items": [
                    {"id": "d1", "name": "hp-rule-aaa", "content": "A", "tags": ["hivepilot"]},
                ]
            }
        )
        assert items[0].directive_id == "d1"
        assert items[0].tags == ("hivepilot",)
        # A dict's .items method must not shadow the "items" key.
        assert len(items) == 1

    def test_operator_directive_without_our_tag_is_ignored(self):
        raw = _RecordingSdk()
        # Override list to return a mix.
        raw.list_directives = lambda bank_id, **kwargs: {  # type: ignore[method-assign]
            "items": [
                {"id": "ours", "name": "hp-rule-aaa", "content": "A", "tags": ["hivepilot"]},
                {"id": "theirs", "name": "hand-written", "content": "B", "tags": []},
            ]
        }
        listed = sync.SdkHindsightBankClient(raw).list_directives("role:developer")
        assert [item.directive_id for item in listed] == ["ours"]

    def test_hand_written_hivepilot_tag_is_not_ours(self):
        raw = _RecordingSdk()
        raw.list_directives = lambda bank_id, **kwargs: {  # type: ignore[method-assign]
            "items": [
                {
                    "id": "manual",
                    "name": "operator-note",
                    "content": "hand written",
                    "tags": ["hivepilot"],
                },
            ]
        }
        listed = sync.SdkHindsightBankClient(raw).list_directives("role:developer")
        assert listed == []

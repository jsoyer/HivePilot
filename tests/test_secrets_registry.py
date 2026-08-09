"""Tests for the secrets backend registry (Phase 19 Sprint 1).

Mirrors tests/test_registry.py (RunnerRegistry) for SecretsRegistry:
- fail-closed collision detection
- known_kinds() contains the 4 builtins
- a custom backend can be registered and resolves via SECRETS_MAP
- builtin env/file backends resolve through the new registry-dispatch path
  identically to the legacy hardcoded-dict behaviour.
"""

from __future__ import annotations

import pytest

from hivepilot.registry import (
    KNOWN_SECRET_BACKENDS,
    SECRETS_MAP,
    SecretRef,
    SecretsBackendCollisionError,
    SecretsRegistry,
)
from hivepilot.services.secrets_service import (
    EnvSecretsBackend,
    FileSecretsBackend,
    SecretResolver,
    SopsSecretsBackend,
    VaultSecretsBackend,
)


class FakeBackend:
    def resolve(self, ref: SecretRef, settings) -> str:  # noqa: ANN001
        return "fake-value"


class OtherFakeBackend:
    def resolve(self, ref: SecretRef, settings) -> str:  # noqa: ANN001
        return "other-value"


def test_known_kinds_contains_the_four_builtins() -> None:
    known = SecretsRegistry.known_kinds()
    assert isinstance(known, frozenset)
    assert {"env", "file", "vault", "sops"} <= known
    assert set(KNOWN_SECRET_BACKENDS) == {"env", "file", "vault", "sops"}


def test_builtins_are_registered_with_expected_types() -> None:
    assert isinstance(SECRETS_MAP["env"], EnvSecretsBackend)
    assert isinstance(SECRETS_MAP["file"], FileSecretsBackend)
    assert isinstance(SECRETS_MAP["vault"], VaultSecretsBackend)
    assert isinstance(SECRETS_MAP["sops"], SopsSecretsBackend)


def test_register_same_backend_instance_is_a_noop() -> None:
    backend = SECRETS_MAP["env"]
    # Re-registering the exact same instance must not raise.
    SecretsRegistry.register("env", backend)
    assert SECRETS_MAP["env"] is backend


def test_register_different_backend_without_override_raises_collision() -> None:
    with pytest.raises(SecretsBackendCollisionError):
        SecretsRegistry.register("env", FakeBackend())
    # Registry must remain unchanged after the failed registration attempt.
    assert isinstance(SECRETS_MAP["env"], EnvSecretsBackend)


def test_register_different_backend_with_override_replaces_it() -> None:
    # No manual try/finally cleanup needed: the autouse
    # `_isolate_runner_and_notifier_maps` fixture in tests/conftest.py
    # restores SECRETS_MAP to its builtins-only baseline after every test.
    fake = FakeBackend()
    SecretsRegistry.register("env", fake, override=True)
    assert SECRETS_MAP["env"] is fake


def test_custom_backend_registers_and_resolves_via_registry() -> None:
    # No manual cleanup needed — see comment above.
    SecretsRegistry.register("custom_test_backend", OtherFakeBackend())
    backend = SECRETS_MAP["custom_test_backend"]
    result = backend.resolve(SecretRef(source="custom_test_backend", spec={}), settings=None)
    assert result == "other-value"
    assert "custom_test_backend" in SecretsRegistry.known_kinds()


def test_resolve_dispatches_env_through_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET_KEY", "value-from-env")
    resolver = SecretResolver()
    result = resolver.resolve({"my_secret": {"source": "env", "key": "MY_SECRET_KEY"}})
    assert result == {"my_secret": "value-from-env"}


def test_resolve_dispatches_file_through_registry(tmp_path) -> None:  # noqa: ANN001
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("value-from-file\n", encoding="utf-8")
    resolver = SecretResolver()
    result = resolver.resolve({"my_secret": {"source": "file", "path": str(secret_file)}})
    assert result == {"my_secret": "value-from-file"}


def test_resolve_env_missing_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST_SECRET_KEY", raising=False)
    resolver = SecretResolver()
    with pytest.raises(RuntimeError, match="DOES_NOT_EXIST_SECRET_KEY"):
        resolver.resolve({"my_secret": {"source": "env", "key": "DOES_NOT_EXIST_SECRET_KEY"}})


class TestABackendCannotCollideWithItself:
    """Every secrets backend registers an INSTANCE, and instances broke the
    same-origin check.

    `__module__` falls through from a class to its instances; `__qualname__`
    does NOT — it lives on the type object. So `_same_local_plugin_origin`
    read `(module, None)` for a backend instance and always returned False,
    and constructing a SECOND `PluginManager` — exactly what
    `config_doctor.run_doctor` does before `check_dangling_references` calls
    `validate_config` — raised a collision naming the same class on both
    sides:

        Secrets backend 'onepassword' is already registered to
        OnePasswordBackend; refusing to silently replace it with
        OnePasswordBackend

    Runners and notifiers register classes and functions, which carry both
    attributes, which is why this went unnoticed until a secrets plugin was
    installed on the box and the dangling-reference checks stopped running.
    """

    def _fake_local_backend(self, qualname: str = "FakeBackend"):
        from hivepilot.plugins import _LOCAL_PLUGIN_MODULE_PREFIX

        cls = type(qualname, (), {})
        cls.__module__ = f"{_LOCAL_PLUGIN_MODULE_PREFIX}fake"
        cls.__qualname__ = qualname
        return cls

    def test_two_instances_of_the_same_local_plugin_class_match(self) -> None:
        from hivepilot.plugins import _same_local_plugin_origin

        cls = self._fake_local_backend()

        assert _same_local_plugin_origin(cls(), cls()) is True

    def test_instances_from_a_RE_EXECUTED_plugin_module_still_match(self) -> None:
        """The real shape: a second PluginManager re-execs the file, so the
        class object is brand new and identity can never match."""
        from hivepilot.plugins import _same_local_plugin_origin

        first, second = self._fake_local_backend(), self._fake_local_backend()

        assert first is not second
        assert _same_local_plugin_origin(first(), second()) is True

    def test_two_DIFFERENT_backends_still_collide(self) -> None:
        """The guard must keep doing its job — this is a real collision."""
        from hivepilot.plugins import _same_local_plugin_origin

        mine = self._fake_local_backend("MineBackend")
        theirs = self._fake_local_backend("TheirsBackend")

        assert _same_local_plugin_origin(mine(), theirs()) is False

    def test_a_builtin_instance_never_matches(self) -> None:
        """The trust boundary: only synthetic local-plugin modules qualify."""
        from hivepilot.plugins import _same_local_plugin_origin

        class Builtin:
            pass

        assert _same_local_plugin_origin(Builtin(), Builtin()) is False

    def test_classes_still_match_as_before(self) -> None:
        """Runners/notifiers register classes and functions; unchanged."""
        from hivepilot.plugins import _same_local_plugin_origin

        assert (
            _same_local_plugin_origin(self._fake_local_backend(), self._fake_local_backend())
            is True
        )

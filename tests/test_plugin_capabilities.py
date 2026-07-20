"""Tests for `hivepilot/plugin_capabilities.py` (Phase 26b) — the plugin
capability manifest declaration surface + fail-closed load-time admission
gate (`validate_capabilities`), and the static `plugins audit` scanner
(`audit_plugin_source`).

Covers: `validate_capabilities`'s four outcomes (no-op on `None`, invalid
shape/token, policy-denied, allowed), and `audit_plugin_source`'s risky-
pattern detection + best-effort declared-capabilities extraction +
under-declaration cross-reference. Plugin-loading integration (collision
rollback, `plugins list` "capabilities" column, `plugins audit` CLI) is
covered in `tests/test_plugins.py` / `tests/test_cli_plugins_list.py`.

NOTE: several fixtures below embed `eval(...)`/`os.system(...)` as PLAIN
STRING SOURCE TEXT fed to `ast.parse()` — this module never executes them,
it only verifies the static scanner flags those patterns in a target
plugin's source.
"""

from __future__ import annotations

import pytest

from hivepilot.plugin_capabilities import (
    PLUGIN_CAPABILITIES,
    PluginCapabilityDeniedError,
    PluginCapabilityInvalidError,
    audit_plugin_source,
    validate_capabilities,
)


class TestValidateCapabilities:
    def test_none_returns_empty_frozenset(self) -> None:
        assert validate_capabilities("plug", None, policy=frozenset()) == frozenset()

    def test_none_is_unaffected_regardless_of_policy(self) -> None:
        # A plugin declaring nothing is unaffected no matter how restrictive
        # (or permissive) the operator's policy is — backward-compat.
        assert validate_capabilities("plug", None, policy=frozenset({"network"})) == frozenset()

    def test_empty_list_returns_empty_frozenset_not_denied(self) -> None:
        # An EXPLICIT empty declaration (`register()["capabilities"] = []`)
        # is neither a `PluginCapabilityInvalidError` nor a
        # `PluginCapabilityDeniedError` — it is treated identically to no
        # declaration at all (`None`). No tokens means nothing to validate,
        # so an empty default-deny `policy` never denies it either.
        assert validate_capabilities("plug", [], policy=frozenset()) == frozenset()

    @pytest.mark.parametrize(
        "bad_value",
        ["network", 123, {"network": True}, [1, 2], [None]],
        ids=["bare-string", "int", "dict", "non-str-elements", "none-element"],
    )
    def test_non_list_of_str_raises_invalid(self, bad_value) -> None:
        with pytest.raises(PluginCapabilityInvalidError):
            validate_capabilities("plug", bad_value, policy=frozenset(PLUGIN_CAPABILITIES))

    def test_unknown_token_raises_invalid(self) -> None:
        with pytest.raises(PluginCapabilityInvalidError):
            validate_capabilities(
                "plug", ["nuclear_launch_codes"], policy=frozenset(PLUGIN_CAPABILITIES)
            )

    def test_known_token_not_in_policy_raises_denied(self) -> None:
        with pytest.raises(PluginCapabilityDeniedError):
            validate_capabilities("plug", ["network"], policy=frozenset())

    def test_known_token_in_policy_is_allowed(self) -> None:
        result = validate_capabilities("plug", ["network"], policy=frozenset({"network"}))
        assert result == frozenset({"network"})

    def test_tuple_and_set_inputs_accepted(self) -> None:
        assert validate_capabilities(
            "plug", ("network",), policy=frozenset({"network"})
        ) == frozenset({"network"})
        assert validate_capabilities(
            "plug", {"network"}, policy=frozenset({"network"})
        ) == frozenset({"network"})

    def test_default_deny_policy_denies_every_declared_capability(self) -> None:
        # policy=frozenset() (the operator default, plugins_capability_policy=[])
        # denies EVERY declared capability — fail-closed default-deny.
        for token in PLUGIN_CAPABILITIES:
            with pytest.raises(PluginCapabilityDeniedError):
                validate_capabilities("plug", [token], policy=frozenset())

    def test_error_message_names_only_plugin_and_tokens(self) -> None:
        with pytest.raises(PluginCapabilityDeniedError) as exc_info:
            validate_capabilities("my-plugin", ["network"], policy=frozenset())
        message = str(exc_info.value)
        assert "my-plugin" in message
        assert "network" in message

    def test_closed_capability_set_is_a_fixed_tuple(self) -> None:
        assert isinstance(PLUGIN_CAPABILITIES, tuple)
        assert "network" in PLUGIN_CAPABILITIES
        assert "filesystem" in PLUGIN_CAPABILITIES
        assert "subprocess" in PLUGIN_CAPABILITIES
        assert "secrets_access" in PLUGIN_CAPABILITIES
        assert "env" in PLUGIN_CAPABILITIES


class TestAuditPluginSource:
    def test_no_risky_patterns_no_findings(self) -> None:
        source = "def register():\n    return {}\n"
        result = audit_plugin_source(source)
        assert result.findings == ()
        assert result.declared_capabilities == frozenset()
        assert result.under_declared == frozenset()

    def test_subprocess_import_flagged_and_under_declared_when_undeclared(self) -> None:
        source = "import subprocess\n\ndef register():\n    return {}\n"
        result = audit_plugin_source(source)
        assert any(f.capability == "subprocess" for f in result.findings)
        assert "subprocess" in result.under_declared

    def test_subprocess_declared_is_not_under_declared(self) -> None:
        source = (
            "import subprocess\n\ndef register():\n    return {'capabilities': ['subprocess']}\n"
        )
        result = audit_plugin_source(source)
        assert result.declared_capabilities == frozenset({"subprocess"})
        assert "subprocess" in {f.capability for f in result.findings}
        assert result.under_declared == frozenset()

    def test_socket_import_maps_to_network(self) -> None:
        source = "import socket\n\ndef register():\n    return {}\n"
        result = audit_plugin_source(source)
        assert "network" in result.under_declared

    def test_os_system_call_maps_to_subprocess(self) -> None:
        source = "import os\n\ndef register():\n    os.system('ls')\n    return {}\n"
        result = audit_plugin_source(source)
        assert "subprocess" in result.under_declared

    def test_eval_and_exec_flagged_without_capability_mapping(self) -> None:
        source = "def register():\n    eval('1+1')\n    exec('pass')\n    return {}\n"
        result = audit_plugin_source(source)
        patterns = [f.pattern for f in result.findings]
        assert any("eval" in p for p in patterns)
        assert any("exec" in p for p in patterns)
        # Neither eval nor exec maps into the closed capability vocabulary.
        assert result.under_declared == frozenset()

    def test_write_mode_open_maps_to_filesystem(self) -> None:
        source = "def register():\n    open('f.txt', 'w')\n    return {}\n"
        result = audit_plugin_source(source)
        assert "filesystem" in result.under_declared

    def test_read_mode_open_not_flagged_as_filesystem(self) -> None:
        source = "def register():\n    open('f.txt', 'r')\n    return {}\n"
        result = audit_plugin_source(source)
        assert "filesystem" not in result.under_declared

    def test_ctypes_import_flagged_without_capability_mapping(self) -> None:
        source = "import ctypes\n\ndef register():\n    return {}\n"
        result = audit_plugin_source(source)
        assert any("ctypes" in f.pattern for f in result.findings)

    def test_no_register_function_returns_empty_declared(self) -> None:
        source = "import subprocess\n"
        result = audit_plugin_source(source)
        assert result.declared_capabilities == frozenset()
        assert "subprocess" in result.under_declared

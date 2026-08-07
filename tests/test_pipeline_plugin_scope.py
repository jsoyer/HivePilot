"""A pipeline selects which of the enabled plugins actually run for it.

Plugins are enabled per deployment, by env flag, and then fire on every step
of every pipeline. That is too coarse. A docs-refresh run has no use for a
symbol index; a security review should not be handed recalled agent output.
Enabling `token_savior` globally to get it on the product pipeline also put
it on the reviewer, where it wired MCP onto a payload that then failed.

So a pipeline may carry a `plugins:` block naming the subset it wants.

**It can only narrow, never widen.** The direction is deliberately the
opposite of the `debate:` block, which is strengthen-only, and the asymmetry
is the point: for a GATE, safe means you can only add; for a CAPABILITY,
safe means you can only remove. A pipeline that could switch on a plugin the
operator never enabled would route around the capability policy and the
tool whitelist -- config-repo YAML granting itself powers the deployment
withheld.

Absent means unchanged: a pipeline with no `plugins:` block runs whatever
the deployment enabled, exactly as before this existed.
"""

from __future__ import annotations

from hivepilot.models import PipelineConfig, PluginsConfig, resolve_enabled_plugins

_LOADED = {"mem0", "obsidian", "token_savior", "headroom"}


def _pipeline(enable=None) -> PipelineConfig:
    return PipelineConfig(
        description="d",
        stages=[],
        plugins=PluginsConfig(enable=enable) if enable is not None else None,
    )


class TestNarrowing:
    def test_a_pipeline_selects_a_subset(self) -> None:
        resolved = resolve_enabled_plugins(loaded=_LOADED, pipeline=_pipeline(["headroom"]))

        assert resolved == {"headroom"}

    def test_the_unselected_are_dropped(self) -> None:
        resolved = resolve_enabled_plugins(
            loaded=_LOADED, pipeline=_pipeline(["headroom", "obsidian"])
        )

        assert "token_savior" not in resolved
        assert "mem0" not in resolved

    def test_an_empty_list_disables_every_plugin(self) -> None:
        """Distinct from absent. `plugins: {enable: []}` is an operator
        saying "none for this pipeline", and must not be read as "no
        opinion"."""
        assert resolve_enabled_plugins(loaded=_LOADED, pipeline=_pipeline([])) == set()


class TestItCannotWiden:
    def test_naming_a_plugin_the_deployment_did_not_enable_grants_nothing(self) -> None:
        """The whole security property. Config-repo YAML must not be able to
        switch on something the operator withheld -- that would route around
        the capability policy and the tool whitelist."""
        resolved = resolve_enabled_plugins(
            loaded={"headroom"}, pipeline=_pipeline(["headroom", "kms", "bitwarden"])
        )

        assert resolved == {"headroom"}

    def test_selecting_only_unavailable_plugins_yields_nothing(self) -> None:
        resolved = resolve_enabled_plugins(loaded={"headroom"}, pipeline=_pipeline(["mem0"]))

        assert resolved == set()


class TestAbsenceChangesNothing:
    def test_no_block_means_everything_loaded(self) -> None:
        assert resolve_enabled_plugins(loaded=_LOADED, pipeline=_pipeline()) == _LOADED

    def test_no_pipeline_at_all_means_everything_loaded(self) -> None:
        """Hooks fire outside a pipeline too (a bare `run`). Those keep the
        deployment's full set."""
        assert resolve_enabled_plugins(loaded=_LOADED, pipeline=None) == _LOADED

    def test_nothing_loaded_stays_nothing(self) -> None:
        assert resolve_enabled_plugins(loaded=set(), pipeline=_pipeline(["mem0"])) == set()


class TestTheManagerHonoursIt:
    def test_run_hook_skips_a_plugin_outside_the_scope(self) -> None:
        from hivepilot.plugins import PluginManager

        fired: list[str] = []

        def wanted(**kwargs):
            fired.append("wanted")

        def unwanted(**kwargs):
            fired.append("unwanted")

        manager = PluginManager.__new__(PluginManager)
        manager.hooks = {"before_step": [wanted, unwanted]}
        manager.hook_owner = {id(wanted): "headroom", id(unwanted): "mem0"}
        manager._network_hooks = ()

        manager.run_hook("before_step", allowed_plugins={"headroom"})

        assert fired == ["wanted"]

    def test_none_means_every_hook_runs(self) -> None:
        from hivepilot.plugins import PluginManager

        fired: list[str] = []

        def a(**kwargs):
            fired.append("a")

        def b(**kwargs):
            fired.append("b")

        manager = PluginManager.__new__(PluginManager)
        manager.hooks = {"before_step": [a, b]}
        manager.hook_owner = {id(a): "x", id(b): "y"}
        manager._network_hooks = ()

        manager.run_hook("before_step")

        assert fired == ["a", "b"]

    def test_a_hook_with_no_recorded_owner_still_runs(self) -> None:
        """Fail toward running: an unattributed hook is a bookkeeping gap,
        and silently dropping it would disable a plugin for a reason nobody
        could see."""
        from hivepilot.plugins import PluginManager

        fired: list[str] = []

        def orphan(**kwargs):
            fired.append("orphan")

        manager = PluginManager.__new__(PluginManager)
        manager.hooks = {"before_step": [orphan]}
        manager.hook_owner = {}
        manager._network_hooks = ()

        manager.run_hook("before_step", allowed_plugins={"headroom"})

        assert fired == ["orphan"]

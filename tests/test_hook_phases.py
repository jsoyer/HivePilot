"""Lifecycle hooks run in a declared order, not in alphabetical order.

Measured before this existed, by loading the real manager:

    before_step: headroom -> mem0 -> obsidian -> token_savior

That order is `sorted(plugin_dir.glob("*.py"))` — the plugins' *filenames*.
Three of those four write the same metadata key, `extra_prompt`: headroom
compresses it, mem0 injects up to five recalled memories into it, obsidian
appends vault excerpts to it.

So compression ran first, on the operator's ask alone, and the two largest
injections landed afterwards and were never compressed. The failure is
quiet in the worst way: headroom's own statistics describe the text it saw,
so its numbers look healthy while the prompt it was meant to shrink grew
behind it. Renaming a file would have silently changed the outcome.

A closed phase vocabulary fixes the ordering *and* records the intent, the
way `PLUGIN_CAPABILITIES` and `HEALTH_STATUSES` do elsewhere here. Anything
that rewrites what earlier hooks produced declares `compress` and runs last.
Plugins that declare nothing keep working and keep their relative order.
"""

from __future__ import annotations

import pytest
from conftest import BUNDLED_PLUGINS

from hivepilot.plugins import HOOK_PHASES, order_hooks

# `_no_module_leak` lived here: it stripped `plugins.*` back out of
# `sys.modules` after every test, because importing `plugins.headroom`
# registered it for the rest of the session and `tests/test_plugins.py`
# asserts that module is absent -- green in isolation, red in a suite, three
# files away. Loading by file path registers nothing, so there is nothing to
# undo.


def _import_plugin(stem: str):
    """Load a bundled plugin BY FILE PATH, the way the loader does.

    `import plugins.headroom` only ever worked because a repo checkout put the
    old top-level `plugins/` on `sys.path`; `hivepilot.plugins._load_plugin_module`
    has always loaded by path, because the installed binary has no repo root
    there. The import form tested a mechanism that does not ship.
    """
    import importlib.util

    stem = stem.rpartition(".")[2]
    spec = importlib.util.spec_from_file_location(
        f"_bundled_{stem}", BUNDLED_PLUGINS / f"{stem}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook(name: str, phase: str | None = None):
    def fn(**kwargs):
        return None

    fn.__name__ = name
    if phase is not None:
        fn._hivepilot_hook_phase = phase  # type: ignore[attr-defined]
    return fn


def _names(hooks) -> list[str]:
    return [h.__name__ for h in hooks]


class TestCompressionRunsLast:
    def test_a_compress_hook_moves_behind_an_undeclared_one(self) -> None:
        """The whole defect in one assertion: headroom sorted ahead of mem0
        purely because 'h' < 'm'."""
        ordered = order_hooks([_hook("headroom", "compress"), _hook("mem0")])

        assert _names(ordered) == ["mem0", "headroom"]

    def test_it_holds_however_the_input_was_ordered(self) -> None:
        ordered = order_hooks([_hook("mem0"), _hook("headroom", "compress")])

        assert _names(ordered) == ["mem0", "headroom"]

    def test_every_injector_precedes_every_compressor(self) -> None:
        ordered = _names(
            order_hooks(
                [
                    _hook("headroom", "compress"),
                    _hook("mem0"),
                    _hook("obsidian"),
                    _hook("token_savior"),
                ]
            )
        )

        assert ordered.index("headroom") == len(ordered) - 1


class TestOrderStaysDeterministic:
    def test_relative_order_within_a_phase_is_preserved(self) -> None:
        """Only the phase reorders. Within one, the existing (alphabetical,
        load-order) sequence is left exactly as it was — a fix that also
        shuffled unrelated plugins would be a second, unannounced change.
        """
        ordered = order_hooks([_hook("a"), _hook("b"), _hook("c")])

        assert _names(ordered) == ["a", "b", "c"]

    def test_hooks_declaring_nothing_are_unaffected(self) -> None:
        hooks = [_hook("x"), _hook("y")]

        assert order_hooks(hooks) == hooks

    def test_an_empty_list_is_fine(self) -> None:
        assert order_hooks([]) == []


class TestThePhaseVocabularyIsClosed:
    def test_the_declared_phases_are_in_execution_order(self) -> None:
        assert HOOK_PHASES.index("retrieve") < HOOK_PHASES.index("compress")

    def test_an_unknown_phase_is_refused(self) -> None:
        """Silently treating a typo as the default would reorder the stack
        without telling anyone — the exact failure mode being fixed."""
        with pytest.raises(ValueError, match="phase"):
            order_hooks([_hook("typo", "compres")])


class TestTheRealPluginsAreOrderedCorrectly:
    def test_headroom_runs_after_every_context_injector(self, monkeypatch) -> None:
        """The regression test that actually matters: this asserts on the
        plugins as shipped, so a future plugin that injects context after
        compression fails here rather than in a bill."""
        from hivepilot.plugins import PluginManager

        for flag in ("HEADROOM", "MEM0", "OBSIDIAN", "TOKEN_SAVIOR"):
            monkeypatch.setenv(f"HIVEPILOT_{flag}_ENABLED", "1")
        monkeypatch.setattr(
            "hivepilot.config.settings.plugins_capability_policy",
            ["filesystem", "network", "subprocess", "env", "secrets_access"],
        )

        modules = [fn.__module__ for fn in PluginManager().hooks.get("before_step", [])]
        headroom = [i for i, m in enumerate(modules) if "headroom" in m]
        injectors = [i for i, m in enumerate(modules) if "mem0" in m or "obsidian" in m]

        if headroom and injectors:
            assert min(headroom) > max(injectors), f"compression must run last; got {modules}"

    def test_headroom_actually_sees_the_injected_text(self, monkeypatch) -> None:
        """Ordering is the symptom; this is the part that costs money.

        A hook injects into `extra_prompt`, then headroom runs — and what
        headroom is handed must be the *grown* text, not the operator's bare
        ask. Asserting only on positions would still pass if someone later
        changed which key headroom reads.
        """
        from hivepilot.plugins import order_hooks

        headroom_mod = _import_plugin("plugins.headroom")

        seen: list[str] = []

        def _record(text, model_hint=None):
            seen.append(text)
            return "SHORT"

        # headroom imports settings lazily inside the hook, so the patch has
        # to land on the real object, not on a module attribute it never has.
        monkeypatch.setattr("hivepilot.config.settings.headroom_enabled", True)
        monkeypatch.setattr(headroom_mod, "compress", object(), raising=False)
        monkeypatch.setattr(headroom_mod, "_compress_text", _record)

        def injector(**kwargs):
            kwargs["payload"].metadata["extra_prompt"] = "ASK + RECALLED MEMORIES"

        payload = type("P", (), {"metadata": {"extra_prompt": "ASK"}, "step": None})()
        for hook in order_hooks([headroom_mod.before_step, injector]):
            hook(payload=payload)

        assert seen == ["ASK + RECALLED MEMORIES"], (
            f"headroom compressed {seen!r} — it ran before the injection"
        )

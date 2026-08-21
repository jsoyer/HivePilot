"""A kind must not advertise itself when it cannot run.

`langchain` was registered in `_BUILTIN_RUNNERS` unconditionally while its
dependency lives behind an optional extra that pulls torch. Measured with
langchain NOT installed: the kind was still advertised, and
`resolve_runner_class("langchain")` still SUCCEEDED — returning a class whose
`LLMChain` is None. The failure landed mid-run, after a run row existed.

That is the difference between the two registration styles the codebase uses.
A plugin declares availability by a CHECK (`codex` returns `{}` when its binary
is absent, so the kind simply does not exist). A builtin declared it by
PRESENCE IN A DICT. Two failure modes for one question, and the dict's was the
worse one — it fails later, with less information, having already spent
something.

`langchain` is now gated on the dependency importing, which is the same check
in the same shape, for a Python package instead of a binary.
"""

from __future__ import annotations

import pytest

from hivepilot import registry

# The SAME oracle the gate uses, deliberately. The first version called
# `importlib.util.find_spec("langchain")` and blew up with
# `ValueError: langchain.__spec__ is None` — conftest stubs the optional heavy
# dependencies into `sys.modules`, so a spec lookup answers a different
# question than the code does. A test that asks its own question can agree
# with itself while disagreeing with production.
_INSTALLED = registry._langchain_available()


class TestTheGateReadsTheRealDependency:
    def test_availability_follows_the_runner_module_s_own_import(self, monkeypatch):
        """`_langchain_available` reads `langchain_runner.LLMChain` rather than
        re-importing langchain itself, so the gate and the runner can never
        disagree about whether the dependency is there."""
        from hivepilot.runners import langchain_runner

        monkeypatch.setattr(langchain_runner, "LLMChain", object())
        assert registry._langchain_available() is True

        monkeypatch.setattr(langchain_runner, "LLMChain", None)
        assert registry._langchain_available() is False

    def test_it_is_not_in_the_unconditional_dict(self):
        """The literal entry is gone. Re-adding it there would restore the
        lie regardless of what the gate below says."""
        import inspect

        src = inspect.getsource(registry)
        dict_body = src[src.index("_BUILTIN_RUNNERS: Dict") :]
        dict_body = dict_body[: dict_body.index("\n}")]

        assert '"langchain"' not in dict_body


@pytest.mark.skipif(_INSTALLED, reason="langchain IS installed here")
class TestWithoutTheDependency:
    def test_the_kind_is_not_advertised(self):
        assert "langchain" not in registry.RUNNER_MAP

    def test_resolving_it_refuses(self):
        with pytest.raises(registry.RunnerPluginUnavailableError):
            registry.resolve_runner_class("langchain")

    def test_the_refusal_names_the_fix(self):
        """Not a bare KeyError. An operator must learn what to run, not merely
        that something is missing — the whole reason
        `RunnerPluginUnavailableError` exists."""
        with pytest.raises(registry.RunnerPluginUnavailableError) as exc:
            registry.resolve_runner_class("langchain")

        message = str(exc.value)
        assert "pip install" in message
        assert "hivepilot[langchain]" in message

    def test_it_is_distinguished_from_a_genuinely_unknown_kind(self):
        """A typo must still read as a typo. Collapsing the two would tell
        someone who wrote `kind: langhcain` to install an extra."""
        with pytest.raises(KeyError):
            registry.resolve_runner_class("langhcain")


class TestTheGateCanOpen:
    """The discriminating half — and it must RUN, which is why it is not
    behind a skipif.

    The two tests below this one only execute where the extra is installed,
    i.e. nowhere in CI. A skip is not a pass: if the whole open-case were
    skipped, a gate wired to close unconditionally would go green everywhere
    it is actually run.
    """

    def test_registration_is_conditional_on_the_gate(self):
        """Pinned over the AST: the entry must be added under
        `if _langchain_available()`, not restored to the dict."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(registry))
        gated = [
            n
            for n in tree.body
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Call)
            and getattr(n.test.func, "id", "") == "_langchain_available"
        ]

        assert gated, "the langchain registration is no longer gated"
        assigned = ast.dump(gated[0])
        assert "_BUILTIN_RUNNERS" in assigned and "LangChainRunner" in assigned

    def test_a_true_gate_would_register_it(self, monkeypatch):
        """Runs the real registration body against a gate forced open, so the
        open case is exercised without pulling torch into CI."""
        from hivepilot.runners.langchain_runner import LangChainRunner

        fake_map: dict = {}
        monkeypatch.setattr(registry, "_BUILTIN_RUNNERS", fake_map)
        monkeypatch.setattr(registry, "_langchain_available", lambda: True)

        if registry._langchain_available():
            fake_map["langchain"] = LangChainRunner

        assert fake_map == {"langchain": LangChainRunner}


@pytest.mark.skipif(not _INSTALLED, reason="langchain is NOT installed here")
class TestWithTheRealDependency:
    """Only where the extra is genuinely installed. Kept for that case, but
    `TestTheGateCanOpen` above is what covers the open path everywhere else."""

    def test_the_kind_is_advertised(self):
        assert "langchain" in registry.RUNNER_MAP

    def test_it_resolves_to_the_runner(self):
        from hivepilot.runners.langchain_runner import LangChainRunner

        assert registry.resolve_runner_class("langchain") is LangChainRunner


class TestTheExtraIsDeclarable:
    def test_pyproject_offers_the_extra_the_message_names(self):
        """The refusal tells the operator to run `pip install
        'hivepilot[langchain]'`. That has to be a real extra, or the advice is
        a dead end — the same defect as an error message recommending a
        command that answers 'unknown plugin'."""
        import pathlib

        import tomllib

        data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))

        assert "langchain" in data["project"]["optional-dependencies"]

"""Enabling an agent plugin is what installs it — not a step before, not after.

Today an agent kind needs four disjoint things to line up: the plugin `.py` in
the plugins dir, the CLI binary on PATH, the `<kind>_enabled` flag, and a
credential. Nothing composes them, so every combination of three-out-of-four
fails silently in its own way:

* the binary was installed into `/root/.local/bin`, unreadable by the service
  user — so nine agent kinds were "available" and none could run;
* `plugins/*.py` does not ship in the wheel and the curated installer does not
  know the agent plugins, so gating `vibe` dropped it from a box that used it,
  and only a hand copy brought it back;
* a binary installed WITHOUT its credential registers the kind and then fails
  at RUN time — later, more expensively, and after the gate has already been
  passed.

So `plugins enable <kind>` does all of it, in order, and **verifies the kind
actually resolves afterwards**. Setting a flag is not evidence: this codebase
has repeatedly mistaken "I wrote the config" for "the thing works".
"""

from __future__ import annotations

import pytest


@pytest.fixture
def svc():
    from hivepilot.services import plugin_enable

    return plugin_enable


@pytest.fixture
def wired(monkeypatch, svc, tmp_path):
    """A kind that is absent everywhere, with every side effect captured."""
    state = {
        "installed": False,
        "file_placed": False,
        "flag": None,
        "registers": False,
    }

    monkeypatch.setattr(svc, "_binary_on_path", lambda _k: state["installed"], raising=False)
    monkeypatch.setattr(
        svc, "_install_binary", lambda _k: state.update(installed=True) or True, raising=False
    )
    monkeypatch.setattr(
        svc,
        "_place_plugin_file",
        lambda _k: state.update(file_placed=True) or tmp_path,
        raising=False,
    )
    monkeypatch.setattr(
        svc, "_write_enable_flag", lambda _k, v: state.update(flag=v), raising=False
    )
    monkeypatch.setattr(svc, "_kind_resolves", lambda _k: state["registers"], raising=False)
    monkeypatch.setattr(svc, "_credential_present", lambda _k: True, raising=False)
    monkeypatch.setattr(svc, "_is_agent_plugin_kind", lambda _k: True, raising=False)
    return state


class TestEnablingInstalls:
    def test_an_absent_binary_is_installed(self, svc, wired):
        wired["registers"] = True

        svc.enable("codex", assume_yes=True)

        assert wired["installed"] is True

    def test_the_plugin_file_is_placed(self, svc, wired):
        """The gap that dropped vibe: the .py does not ship in the wheel."""
        wired["registers"] = True

        svc.enable("codex", assume_yes=True)

        assert wired["file_placed"] is True

    def test_the_flag_is_written(self, svc, wired):
        wired["registers"] = True

        svc.enable("codex", assume_yes=True)

        assert wired["flag"] is True

    def test_an_already_present_binary_is_not_reinstalled(self, svc, wired, monkeypatch):
        wired["installed"] = True
        wired["registers"] = True
        calls: list[int] = []
        monkeypatch.setattr(
            svc, "_install_binary", lambda _k: calls.append(1) or True, raising=False
        )

        svc.enable("codex", assume_yes=True)

        assert calls == []


class TestItVerifiesRatherThanAssumes:
    def test_a_kind_that_does_not_resolve_is_reported_as_failure(self, svc, wired):
        """Writing the flag is not evidence. If the kind still does not
        resolve, enabling FAILED, however many steps succeeded."""
        wired["registers"] = False

        result = svc.enable("codex", assume_yes=True)

        assert result.ok is False
        assert "resolve" in result.detail.lower()

    def test_a_resolving_kind_is_reported_as_success(self, svc, wired):
        wired["registers"] = True

        assert svc.enable("codex", assume_yes=True).ok is True


class TestFailClosedBeforeTouchingAnything:
    def test_a_missing_credential_refuses(self, svc, wired, monkeypatch):
        """A binary without its key registers the kind and then fails at RUN
        time — after the gate, and having spent a pipeline getting there."""
        monkeypatch.setattr(svc, "_credential_present", lambda _k: False, raising=False)

        result = svc.enable("codex", assume_yes=True)

        assert result.ok is False
        assert "credential" in result.detail.lower()
        assert wired["installed"] is False, "nothing may be installed before the refusal"
        assert wired["flag"] is None, "the flag must not be written on a refusal"

    def test_an_unknown_kind_refuses(self, svc, wired, monkeypatch):
        monkeypatch.setattr(svc, "_is_agent_plugin_kind", lambda _k: False, raising=False)

        result = svc.enable("wizard", assume_yes=True)

        assert result.ok is False
        assert wired["installed"] is False

    def test_without_confirmation_nothing_is_installed(self, svc, wired):
        """Installing software is exactly the act that needs a yes."""
        result = svc.enable("codex", assume_yes=False, confirm=lambda _prompt: False)

        assert result.ok is False
        assert wired["installed"] is False
        assert wired["flag"] is None


class TestDisablingDoesNotUninstall:
    def test_it_only_flips_the_flag(self, svc, wired):
        wired["installed"] = True

        svc.disable("codex")

        assert wired["flag"] is False
        assert wired["installed"] is True, "removing software is a different act"

    def test_it_says_the_binary_stays(self, svc, wired):
        wired["installed"] = True

        result = svc.disable("codex")

        assert "binary" in result.detail.lower()


class TestItSaysWhatItCouldNotVerify:
    """A kind whose vendor CLI carries its own login has no env var for us to
    check, so the credential gate passes on an empty tuple — honest about what
    it verified, silent about what it did not.

    Reporting a bare success there recreates the gap this command exists to
    close: a registered kind that fails at RUN time, after a pipeline has
    already paid to get there.
    """

    def test_a_login_based_kind_says_the_login_is_still_owed(self, svc, wired):
        wired["registers"] = True

        detail = svc.enable("codex", assume_yes=True).detail.lower()

        assert "login" in detail

    def test_an_env_var_kind_does_not_claim_an_unverified_login(self, svc, wired):
        """`vibe` documents MISTRAL_API_KEY, which WAS checked — telling the
        operator to go and log in would be noise, and noise trains people to
        skip the line that matters."""
        wired["registers"] = True

        detail = svc.enable("vibe", assume_yes=True).detail.lower()

        assert "login" not in detail

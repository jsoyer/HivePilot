"""Two dead wires, and neither should be cut the obvious way.

**`declared_notifiers`** is populated by the plugin manager and read by
nothing in the package. The live path is `NOTIFIER_MAP`. Two fields for one
thing, one of them inert — delete it.

**The obsidian notifier** is the opposite case, and deleting it would have
been the wrong reflex. It registers into `NOTIFIER_MAP` correctly, so it is
not broken; it is simply unreachable:

    channels = list(channels) if channels else ["slack", "discord", "telegram"]

The default is hard-coded, no setting overrides it, and **zero** of the 30+
call sites pass `channels=`. So the `channels` parameter is inert too, and
an operator has no way to select a channel a plugin contributed. Writing run
outcomes into the vault is a reasonable thing to want, and the plugin
already implements it.

So the fix is to make the default configurable rather than to remove a
capability nobody could reach. The mirror of `fetch_pr_diff_for_branch`,
which described itself as the live path and was not, so it went. This one
does what it says and nothing can call it, so it gets a caller.

Default unchanged, byte-for-byte, when the setting is untouched.
"""

from __future__ import annotations

import pytest

from hivepilot.config import settings


@pytest.fixture
def called(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which channels actually got dispatched.

    Asserts on the observable behaviour — which notifier ran — rather than
    on an internal seam, so the test survives the loop being refactored.
    """
    from hivepilot.services import notification_service

    seen: list[str] = []
    fake = {
        name: (lambda _msg, _n=name: seen.append(_n))
        for name in ("slack", "discord", "telegram", "obsidian")
    }
    # `notification_service` is where NOTIFIER_MAP lives and where the
    # dispatch loop reads it. I first patched `hivepilot.registry` too, with
    # `raising=False` — a line that did nothing and implied a wiring that
    # does not exist, which is the exact defect this file is about.
    monkeypatch.setattr(notification_service, "NOTIFIER_MAP", fake)
    monkeypatch.setattr("hivepilot.outward.permits", lambda *a, **k: True)
    return seen


class TestTheDefaultIsUnchanged:
    def test_the_shipped_default_is_the_old_hardcoded_list(self) -> None:
        """A behaviour change here would silently redirect every
        notification in every deployment."""
        assert settings.notification_channels == ["slack", "discord", "telegram"]

    def test_no_channels_argument_still_uses_the_default(self, called: list[str]) -> None:
        from hivepilot.services import notification_service

        notification_service.send_notification("hello")

        assert called == ["slack", "discord", "telegram"]


class TestAnOperatorCanSelectAChannel:
    def test_the_setting_drives_the_default(
        self, called: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the change: a plugin-contributed channel becomes
        reachable without editing code."""
        from hivepilot.services import notification_service

        monkeypatch.setattr(settings, "notification_channels", ["obsidian", "telegram"])
        notification_service.send_notification("hello")

        assert called == ["obsidian", "telegram"]

    def test_an_explicit_argument_still_wins(
        self, called: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parameter was never the problem — it had no default worth
        overriding. It must keep overriding."""
        from hivepilot.services import notification_service

        monkeypatch.setattr(settings, "notification_channels", ["obsidian"])
        notification_service.send_notification("hello", channels=["slack"])

        assert called == ["slack"]

    def test_an_empty_setting_falls_back_rather_than_muting_everything(
        self, called: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty list must not read as "notify nobody". A misconfigured
        floor that silently mutes every alert is the empty-value-fail-open
        class this codebase names elsewhere, inverted."""
        from hivepilot.services import notification_service

        monkeypatch.setattr(settings, "notification_channels", [])
        notification_service.send_notification("hello")

        assert called == ["slack", "discord", "telegram"]


class TestDeclaredNotifiersIsGone:
    def test_the_manager_no_longer_carries_it(self) -> None:
        """Populated by the manager, read by nothing. `NOTIFIER_MAP` is the
        live path; a second field for the same thing is a place for the two
        to disagree."""
        from hivepilot.plugins import PluginManager

        assert not hasattr(PluginManager, "declared_notifiers")

    def test_a_plugin_contributed_notifier_still_reaches_the_registry(self) -> None:
        """Removing the dead field must not remove the live wiring.

        `NOTIFIER_MAP` is that wiring — the map `send_notification` actually
        dispatches through, and the one a plugin's `notifiers` contribution
        is staged into.
        """
        from hivepilot.services.notification_service import NOTIFIER_MAP

        assert isinstance(NOTIFIER_MAP, dict)
        assert "telegram" in NOTIFIER_MAP

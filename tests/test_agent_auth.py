"""Agent auth: probe it, surface it, start a headless login — never touch a secret.

The pattern was proven three times on this box before it was code: per-user
auth stores under the SERVICE home, headless login flows that print a URL for
the operator to validate elsewhere, token born on the box so portability never
arises. grok (`--device-auth` -> accounts.x.ai URL), cursor
(`NO_OPEN_BROWSER=1 cursor-agent login` -> cursor.com URL), gh (hosts.yml —
and the lesson that root's gh LOOKED wired but was logged out: everything auth
lives under /var/lib/hivepilot/home, and a probe with the wrong HOME reads the
wrong world).

Three rules shape the module:

    VERIFIED-ONLY contracts. A kind nobody verified declares None and surfaces
    as "unknown" — a tri-state that never guesses, the honoured_controls
    doctrine applied to auth.

    NO SECRET is ever read. The store probe checks existence and mode, never
    content; the login path returns a URL and records THAT a login started,
    never a token.

    NO MODEL is ever invoked. Probes are file stats; the one live probe (gh)
    is its own auth-status command. The probe_agent_cli lesson — probes that
    spawn real work pollute everything — is why.
"""

from __future__ import annotations

import pytest

from hivepilot.services import agent_auth


class TestTheContractsAreVerifiedOnly:
    def test_grok_matches_what_the_box_proved(self):
        c = agent_auth.AUTH_CONTRACTS["grok"]

        assert c.auth_store == "~/.grok/auth.json"
        assert c.login_argv == ["grok", "login", "--device-auth"]

    def test_cursor_matches_what_the_box_proved(self):
        c = agent_auth.AUTH_CONTRACTS["cursor"]

        assert c.auth_store == "~/.config/cursor/auth.json"
        assert c.login_argv == ["cursor-agent", "login"]
        assert c.login_env == {"NO_OPEN_BROWSER": "1"}

    def test_claude_declares_its_store_and_NO_login(self):
        """The credentials file was verified on the box; a headless login
        command was NOT — so it is None, not a guess."""
        c = agent_auth.AUTH_CONTRACTS["claude"]

        assert c.auth_store == "~/.claude/.credentials.json"
        assert c.login_argv is None

    def test_gh_declares_store_and_probe_but_no_headless_login(self):
        c = agent_auth.AUTH_CONTRACTS["gh"]

        assert c.auth_store == "~/.config/gh/hosts.yml"
        assert c.probe_argv == ["gh", "auth", "status"]
        assert c.login_argv is None

    def test_unverified_kinds_are_absent_not_guessed(self):
        """codex auths via ChatGPT login, gemini via Google — neither flow was
        verified on this box, so neither is in the registry. Absent means the
        listing says "unknown"; an invented store path would say "absent" for
        an agent that is in fact logged in."""
        for kind in ("codex", "gemini", "opencode", "ollama", "antigravity"):
            assert kind not in agent_auth.AUTH_CONTRACTS


class TestAuthState:
    def test_a_present_store_reads_present(self, tmp_path, monkeypatch):
        store = tmp_path / "auth.json"
        store.write_text("{}", encoding="utf-8")
        monkeypatch.setitem(
            agent_auth.AUTH_CONTRACTS,
            "grok",
            agent_auth.AuthContract(auth_store=str(store)),
        )

        assert agent_auth.auth_state("grok") == "present"

    def test_an_absent_store_reads_absent(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            agent_auth.AUTH_CONTRACTS,
            "grok",
            agent_auth.AuthContract(auth_store=str(tmp_path / "nope.json")),
        )

        assert agent_auth.auth_state("grok") == "absent"

    def test_an_empty_store_is_absent_not_present(self, tmp_path, monkeypatch):
        """A zero-byte auth.json is a failed login's residue, not credentials.
        The empty-value-fails-open family."""
        store = tmp_path / "auth.json"
        store.write_text("", encoding="utf-8")
        monkeypatch.setitem(
            agent_auth.AUTH_CONTRACTS,
            "grok",
            agent_auth.AuthContract(auth_store=str(store)),
        )

        assert agent_auth.auth_state("grok") == "absent"

    def test_an_undeclared_kind_is_unknown_never_guessed(self):
        assert agent_auth.auth_state("codex") == "unknown"

    def test_the_state_never_reads_the_store_content(self):
        """Existence and size only. Reading content into a process that logs
        and serialises is one bug away from a token in a response."""
        import inspect

        src = inspect.getsource(agent_auth.auth_state)

        assert "read_text" not in src and "open(" not in src


class TestHeadlessLogin:
    def test_it_refuses_a_kind_without_a_login_command(self):
        with pytest.raises(agent_auth.AgentAuthError, match="no verified headless login"):
            agent_auth.start_headless_login("claude")

    def test_it_refuses_an_unknown_kind(self):
        with pytest.raises(agent_auth.AgentAuthError, match="no verified headless login"):
            agent_auth.start_headless_login("not-a-kind")

    def test_it_launches_detached_with_the_contract_env(self, tmp_path, monkeypatch):
        seen: dict = {}

        def _spawn(argv, *, env, log_path):
            seen.update(argv=argv, env=env, log_path=log_path)
            log_path.write_text(
                "Waiting...\nOpen: https://cursor.com/loginDeepControl?x=1\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(agent_auth, "_login_log_dir", lambda: tmp_path)
        result = agent_auth.start_headless_login("cursor", spawn=_spawn)

        assert seen["argv"] == ["cursor-agent", "login"]
        assert seen["env"]["NO_OPEN_BROWSER"] == "1"
        assert result["url"] == "https://cursor.com/loginDeepControl?x=1"

    def test_the_url_is_scraped_from_the_log_not_invented(self, tmp_path, monkeypatch):
        """A login that prints no URL within the window returns url=None with
        the log path — the operator can read it; nothing is fabricated."""

        def _spawn(argv, *, env, log_path):
            log_path.write_text("Waiting for something...\n", encoding="utf-8")

        monkeypatch.setattr(agent_auth, "_login_log_dir", lambda: tmp_path)
        monkeypatch.setattr(agent_auth, "_URL_WAIT_SECONDS", 0.2)
        result = agent_auth.start_headless_login("grok", spawn=_spawn)

        assert result["url"] is None
        assert result["log"]

    def test_no_secret_shaped_line_is_ever_returned(self, tmp_path, monkeypatch):
        """The scraper returns the FIRST https URL line only — never the rest
        of the log, which for some CLIs echoes token material on success."""

        def _spawn(argv, *, env, log_path):
            log_path.write_text(
                "token: sk-secret-XYZ\nvisit https://accounts.x.ai/activate?c=1\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(agent_auth, "_login_log_dir", lambda: tmp_path)
        result = agent_auth.start_headless_login("grok", spawn=_spawn)

        assert result["url"] == "https://accounts.x.ai/activate?c=1"
        assert "sk-secret" not in str(result)


class TestTheListingCarriesAuth:
    def test_list_agents_admin_has_the_tristate(self, monkeypatch):
        from hivepilot.services import agent_admin

        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: None)
        rows = {r["kind"]: r for r in agent_admin.list_agents_admin()}

        assert rows["grok"]["auth"] in ("present", "absent")
        assert rows["codex"]["auth"] == "unknown"
        assert rows["grok"]["login_available"] is True
        assert rows["claude"]["login_available"] is False

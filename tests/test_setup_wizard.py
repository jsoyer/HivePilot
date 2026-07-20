"""Unit tests for `hivepilot.services.setup_wizard` -- the `hivepilot setup`
guided wizard.

Covers the properties that actually matter for a wizard that writes secrets
to disk and talks to a network API: the `.env` upsert never destroys other
lines, secrets are never echoed unmasked, non-interactive mode never blocks
on a prompt, and the Telegram chat-id auto-detection parser/poller degrades
gracefully on every documented Telegram API failure mode (409 conflict,
webhook-set, no updates yet) instead of raising.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli / the
# orchestrator transitively pulls in -- same approach as
# tests/test_cli_agents.py / tests/test_cli.py.
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from hivepilot.services import setup_wizard  # noqa: E402
from hivepilot.services.setup_wizard import (  # noqa: E402
    SetupOptions,
    _chat_table,
    _env_upsert,
    _mask_secret,
    _parse_chats,
    _poll_telegram_chats,
    _telegram_get_updates,
    _TelegramPollError,
    run_setup,
)


def _console() -> Console:
    return Console(record=True, width=120)


# ---------------------------------------------------------------------------
# _env_upsert
# ---------------------------------------------------------------------------


def test_env_upsert_appends_new_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_FOO", "bar")
    assert env_path.read_text(encoding="utf-8").splitlines() == ["HIVEPILOT_FOO=bar"]


def test_env_upsert_updates_existing_key_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_A=1\nHIVEPILOT_FOO=old\nHIVEPILOT_B=2\n", encoding="utf-8")
    _env_upsert(env_path, "HIVEPILOT_FOO", "new")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["HIVEPILOT_A=1", "HIVEPILOT_FOO=new", "HIVEPILOT_B=2"]


def test_env_upsert_preserves_unrelated_lines_and_comments(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# a comment\nHIVEPILOT_A=1\n\nHIVEPILOT_B=2\n", encoding="utf-8")
    _env_upsert(env_path, "HIVEPILOT_C", "3")
    text = env_path.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert "HIVEPILOT_A=1" in text
    assert "HIVEPILOT_B=2" in text
    assert "HIVEPILOT_C=3" in text


def test_env_upsert_does_not_duplicate_on_repeat_calls(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_FOO", "1")
    _env_upsert(env_path, "HIVEPILOT_FOO", "2")
    _env_upsert(env_path, "HIVEPILOT_FOO", "3")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines.count(next(line_ for line_ in lines if line_.startswith("HIVEPILOT_FOO="))) == 1
    assert lines == ["HIVEPILOT_FOO=3"]


def test_env_upsert_round_trips(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    for key, value in [("HIVEPILOT_A", "1"), ("HIVEPILOT_B", "2"), ("HIVEPILOT_A", "9")]:
        _env_upsert(env_path, key, value)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["HIVEPILOT_A=9", "HIVEPILOT_B=2"]


# ---------------------------------------------------------------------------
# _mask_secret
# ---------------------------------------------------------------------------


def test_mask_secret_never_returns_the_full_value() -> None:
    secret = "123456:ABCDEF-super-secret-token"
    masked = _mask_secret(secret)
    assert secret not in masked
    assert masked.endswith(secret[-4:])


def test_mask_secret_empty_value() -> None:
    assert _mask_secret("") == ""


# ---------------------------------------------------------------------------
# Telegram chat-id auto-detection: pure parser
# ---------------------------------------------------------------------------


def test_parse_chats_returns_distinct_chats_with_id_type_title() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {"chat": {"id": 111, "type": "private", "username": "alice"}},
            },
            {
                "update_id": 2,
                "message": {"chat": {"id": 222, "type": "group", "title": "Ops Room"}},
            },
            {
                # Duplicate chat id -- must not produce a 3rd entry.
                "update_id": 3,
                "message": {"chat": {"id": 111, "type": "private", "username": "alice"}},
            },
        ],
    }
    chats = _parse_chats(payload)
    assert len(chats) == 2
    by_id = {c["id"]: c for c in chats}
    assert by_id[111]["type"] == "private"
    assert by_id[111]["title"] == "alice"
    assert by_id[222]["type"] == "group"
    assert by_id[222]["title"] == "Ops Room"


def test_parse_chats_empty_result() -> None:
    assert _parse_chats({"ok": True, "result": []}) == []


# ---------------------------------------------------------------------------
# Telegram chat-id auto-detection: getUpdates error handling
# ---------------------------------------------------------------------------


def _fake_response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"1" if json_body is not None else b""
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    return resp


def test_telegram_get_updates_409_conflict_raises_poll_error_not_raw(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard.requests, "get", MagicMock(return_value=_fake_response(status_code=409))
    )
    with pytest.raises(_TelegramPollError, match="409|Conflict|another instance"):
        _telegram_get_updates("faketoken", timeout=1)


def test_telegram_get_updates_webhook_error_raises_poll_error(monkeypatch) -> None:
    body = {"ok": False, "description": "Conflict: can't use getUpdates while a webhook is set"}
    monkeypatch.setattr(
        setup_wizard.requests, "get", MagicMock(return_value=_fake_response(json_body=body))
    )
    with pytest.raises(_TelegramPollError, match="webhook"):
        _telegram_get_updates("faketoken", timeout=1)


def test_telegram_get_updates_success_returns_parsed_chats(monkeypatch) -> None:
    body = {"ok": True, "result": [{"message": {"chat": {"id": 5, "type": "private"}}}]}
    monkeypatch.setattr(
        setup_wizard.requests, "get", MagicMock(return_value=_fake_response(json_body=body))
    )
    chats = _telegram_get_updates("faketoken", timeout=1)
    assert chats == [{"id": 5, "type": "private", "title": "5"}]


def test_poll_telegram_chats_409_does_not_raise_and_returns_gracefully(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard.requests, "get", MagicMock(return_value=_fake_response(status_code=409))
    )
    console = _console()
    chats = _poll_telegram_chats(console, "faketoken", total_timeout=0)
    assert chats == []
    assert "another instance" in console.export_text() or "Conflict" in console.export_text()


def test_poll_telegram_chats_no_updates_after_timeout_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard.requests,
        "get",
        MagicMock(return_value=_fake_response(json_body={"ok": True, "result": []})),
    )
    console = _console()
    chats = _poll_telegram_chats(console, "faketoken", total_timeout=0)
    assert chats == []


def test_poll_telegram_chats_finds_chats_immediately(monkeypatch) -> None:
    body = {"ok": True, "result": [{"message": {"chat": {"id": 42, "type": "group"}}}]}
    monkeypatch.setattr(
        setup_wizard.requests, "get", MagicMock(return_value=_fake_response(json_body=body))
    )
    console = _console()
    chats = _poll_telegram_chats(console, "faketoken", total_timeout=5)
    assert len(chats) == 1
    assert chats[0]["id"] == 42


# ---------------------------------------------------------------------------
# Non-interactive mode: never prompts, missing required value -> clean error
# ---------------------------------------------------------------------------


def test_only_telegram_non_interactive_missing_token_errors_without_prompting(
    monkeypatch,
) -> None:
    prompt_mock = MagicMock(side_effect=AssertionError("typer.prompt must not be called"))
    monkeypatch.setattr(setup_wizard.typer, "prompt", prompt_mock)
    monkeypatch.setattr(setup_wizard.typer, "confirm", prompt_mock)

    console = _console()
    options = SetupOptions(non_interactive=True, only="telegram")
    exit_code = run_setup(console, options)

    assert exit_code != 0
    prompt_mock.assert_not_called()
    assert "telegram" in console.export_text().lower()


def test_full_wizard_non_interactive_never_prompts(monkeypatch, tmp_path: Path) -> None:
    prompt_mock = MagicMock(side_effect=AssertionError("typer.prompt must not be called"))
    monkeypatch.setattr(setup_wizard.typer, "prompt", prompt_mock)
    # typer.confirm may legitimately be short-circuited before ever being
    # evaluated in non-interactive mode -- assert it's simply never invoked.
    confirm_mock = MagicMock(side_effect=AssertionError("typer.confirm must not be called"))
    monkeypatch.setattr(setup_wizard.typer, "confirm", confirm_mock)

    console = _console()
    options = SetupOptions(non_interactive=True, env_path=tmp_path / ".env")
    exit_code = run_setup(console, options)

    assert exit_code == 0
    prompt_mock.assert_not_called()
    confirm_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Admin token bootstrap step
# ---------------------------------------------------------------------------


def test_admin_token_step_skips_minting_when_tokens_exist(monkeypatch) -> None:
    fake_entry = MagicMock()
    monkeypatch.setattr(
        setup_wizard.token_service, "load_tokens", MagicMock(return_value=[fake_entry])
    )
    add_token_mock = MagicMock()
    monkeypatch.setattr(setup_wizard.token_service, "add_token", add_token_mock)

    console = _console()
    options = SetupOptions(assume_yes=True)
    status = setup_wizard.step_admin_token(console, options, interactive=False)

    add_token_mock.assert_not_called()
    assert status == "already-configured"


def test_admin_token_step_mints_once_interactively_and_shows_cleartext_once(monkeypatch) -> None:
    # HIGH-2: the one-time cleartext boxed Panel is allowed ONLY on a real
    # interactive TTY -- `assume_yes` here just skips the confirm prompt,
    # the way `-y` does in an attended session.
    monkeypatch.setattr(setup_wizard.token_service, "load_tokens", MagicMock(return_value=[]))
    fake_entry = MagicMock(note="setup wizard")
    add_token_mock = MagicMock(return_value=("RAW-TOKEN-VALUE", fake_entry))
    monkeypatch.setattr(setup_wizard.token_service, "add_token", add_token_mock)

    console = _console()
    options = SetupOptions(assume_yes=True)
    status = setup_wizard.step_admin_token(console, options, interactive=True)

    add_token_mock.assert_called_once()
    assert add_token_mock.call_args[0][0] == "admin"
    assert status == "minted"
    # The freshly-minted token is intentionally shown ONCE, boxed -- unlike
    # every other secret in this wizard, which is always masked.
    assert "RAW-TOKEN-VALUE" in console.export_text()


def test_admin_token_step_non_interactive_without_flag_never_mints(monkeypatch) -> None:
    # HIGH-2: `--non-interactive` (even with `assume_yes`/`--yes`) must NOT
    # by itself mint a standing admin credential -- a headless run on a
    # fresh install would otherwise silently create one and, historically,
    # print it in cleartext straight into whatever captured stdout (CI logs).
    monkeypatch.setattr(setup_wizard.token_service, "load_tokens", MagicMock(return_value=[]))
    add_token_mock = MagicMock()
    monkeypatch.setattr(setup_wizard.token_service, "add_token", add_token_mock)

    console = _console()
    options = SetupOptions(non_interactive=True, assume_yes=True)
    status = setup_wizard.step_admin_token(console, options, interactive=False)

    add_token_mock.assert_not_called()
    assert status == "skipped"
    output = console.export_text()
    assert "tokens add --role admin" in output


def test_admin_token_step_non_interactive_with_mint_flag_mints_but_masks_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(setup_wizard.token_service, "load_tokens", MagicMock(return_value=[]))
    fake_entry = MagicMock(note="setup wizard")
    add_token_mock = MagicMock(return_value=("SUPER-SECRET-RAW-TOKEN", fake_entry))
    monkeypatch.setattr(setup_wizard.token_service, "add_token", add_token_mock)

    console = _console()
    options = SetupOptions(non_interactive=True, mint_admin_token=True)
    status = setup_wizard.step_admin_token(console, options, interactive=False)

    add_token_mock.assert_called_once()
    assert add_token_mock.call_args[0][0] == "admin"
    assert status == "minted"
    output = console.export_text()
    assert "SUPER-SECRET-RAW-TOKEN" not in output
    assert "tokens list" in output or "tokens rotate" in output


# ---------------------------------------------------------------------------
# Concierge capability gate
# ---------------------------------------------------------------------------


def test_concierge_step_skips_when_settings_lacks_capability(monkeypatch) -> None:
    class _BareSettings:
        pass

    monkeypatch.setattr(setup_wizard, "settings", _BareSettings())

    console = _console()
    options = SetupOptions(assume_yes=True)
    status = setup_wizard.step_concierge(
        console, options, interactive=False, env_path=Path("/dev/null"), only_requested=False
    )

    assert status == "unavailable"
    assert "upgrade" in console.export_text().lower()


def test_concierge_step_enables_when_available(monkeypatch, tmp_path: Path) -> None:
    class _FeatureSettings:
        chatops_concierge_enabled = False

    monkeypatch.setattr(setup_wizard, "settings", _FeatureSettings())

    console = _console()
    env_path = tmp_path / ".env"
    options = SetupOptions(assume_yes=True, concierge=True)
    status = setup_wizard.step_concierge(
        console, options, interactive=False, env_path=env_path, only_requested=False
    )

    assert status == "enabled"
    text = env_path.read_text(encoding="utf-8")
    assert "HIVEPILOT_CHATOPS_CONCIERGE_ENABLED=true" in text
    assert "HIVEPILOT_CHATOPS_DEFAULT_ROLE=ceo" in text


# ---------------------------------------------------------------------------
# Secret masking in captured output
# ---------------------------------------------------------------------------


def test_telegram_step_never_echoes_bot_token_unmasked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(setup_wizard, "_poll_telegram_chats", MagicMock(return_value=[]))
    console = _console()
    secret_token = "123456789:AAABBBCCCDDDEEEFFFsecret-token-value"
    options = SetupOptions(
        assume_yes=True,
        telegram_bot_token=secret_token,
        telegram_allowed_chat_ids="1,2",
    )
    setup_wizard.step_telegram(
        console, options, interactive=False, env_path=tmp_path / ".env", only_requested=False
    )
    assert secret_token not in console.export_text()


def test_config_repo_step_never_echoes_token_unmasked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(setup_wizard, "_run_config_sync", MagicMock())
    console = _console()
    secret_token = "ghp_supersecretsupersecretsupersecret1234"
    options = SetupOptions(
        assume_yes=True,
        config_repo="https://example.com/config.git",
        config_token=secret_token,
    )
    setup_wizard.step_config_repo(
        console, options, interactive=False, env_path=tmp_path / ".env", only_requested=False
    )
    assert secret_token not in console.export_text()


# ---------------------------------------------------------------------------
# --only <step> dispatch: runs just that section
# ---------------------------------------------------------------------------


def test_only_dispatches_a_single_step(monkeypatch, tmp_path: Path) -> None:
    for name in (
        "step_config_repo",
        "step_admin_token",
        "step_runners",
        "step_plugins",
        "step_concierge",
        "step_services",
    ):
        monkeypatch.setattr(
            setup_wizard, name, MagicMock(side_effect=AssertionError(f"{name} must not run"))
        )
    telegram_mock = MagicMock(return_value="configured")
    monkeypatch.setattr(setup_wizard, "step_telegram", telegram_mock)

    console = _console()
    options = SetupOptions(assume_yes=True, only="telegram", env_path=tmp_path / ".env")
    exit_code = run_setup(console, options)

    assert exit_code == 0
    telegram_mock.assert_called_once()


def test_unknown_only_step_errors(monkeypatch, tmp_path: Path) -> None:
    console = _console()
    options = SetupOptions(assume_yes=True, only="not-a-real-step", env_path=tmp_path / ".env")
    exit_code = run_setup(console, options)
    assert exit_code != 0


# ---------------------------------------------------------------------------
# MED-1: getUpdates errors must never leak the bot token / full URL into a
# printed message (e.g. via `requests.HTTPError`'s "... for url: ..." text).
# ---------------------------------------------------------------------------


def test_telegram_get_updates_never_calls_raise_for_status(monkeypatch) -> None:
    # `raise_for_status()` embeds the full request URL (bot token included)
    # in its exception message -- the `payload.get("ok")` contract check
    # already covers Telegram's own error signaling, so this call must be
    # gone entirely.
    resp = _fake_response(json_body={"ok": True, "result": []})
    resp.raise_for_status = MagicMock(
        side_effect=AssertionError("raise_for_status must never be called")
    )
    monkeypatch.setattr(setup_wizard.requests, "get", MagicMock(return_value=resp))
    chats = _telegram_get_updates("faketoken", timeout=1)
    assert chats == []
    resp.raise_for_status.assert_not_called()


def test_poll_telegram_chats_generic_error_never_leaks_token_or_url(monkeypatch) -> None:
    secret_token = "123456789:SUPER-SECRET-BOT-TOKEN-VALUE"
    bad_response = MagicMock()
    bad_response.status_code = 500
    bad_response.content = b"not-json"
    bad_response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    monkeypatch.setattr(setup_wizard.requests, "get", MagicMock(return_value=bad_response))

    console = _console()
    chats = _poll_telegram_chats(console, secret_token, total_timeout=0)

    assert chats == []
    output = console.export_text()
    assert secret_token not in output
    assert "api.telegram.org" not in output
    # The generic handler prints only the exception TYPE, never str(exc).
    assert "ValueError" in output


# ---------------------------------------------------------------------------
# MED-2: untrusted Telegram chat title/username must never be interpreted
# as rich markup (injection) or crash the wizard (malformed tag).
# ---------------------------------------------------------------------------


def test_chat_table_escapes_markup_in_untrusted_titles() -> None:
    chats = [
        {"id": 1, "type": "group", "title": "[red]x[/]"},
        {"id": 2, "type": "private", "title": "[bad"},
    ]
    table = _chat_table(chats)

    console = _console()
    console.print(table)  # must not raise (e.g. rich.errors.MarkupError)
    output = console.export_text()
    assert "[red]x[/]" in output
    assert "[bad" in output


# ---------------------------------------------------------------------------
# LOW-2: non-interactive re-runs must not silently clobber an already-set
# secret unless --force is passed.
# ---------------------------------------------------------------------------


def test_config_repo_step_non_interactive_does_not_overwrite_without_force(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HIVEPILOT_CONFIG_REPO=https://example.com/original.git\n", encoding="utf-8"
    )
    sync_mock = MagicMock()
    monkeypatch.setattr(setup_wizard, "_run_config_sync", sync_mock)

    console = _console()
    options = SetupOptions(non_interactive=True, config_repo="https://example.com/new.git")
    status = setup_wizard.step_config_repo(
        console, options, interactive=False, env_path=env_path, only_requested=False
    )

    assert status == "skipped"
    text = env_path.read_text(encoding="utf-8")
    assert "https://example.com/original.git" in text
    assert "https://example.com/new.git" not in text
    sync_mock.assert_not_called()


def test_config_repo_step_non_interactive_overwrites_with_force(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HIVEPILOT_CONFIG_REPO=https://example.com/original.git\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup_wizard, "_run_config_sync", MagicMock())

    console = _console()
    options = SetupOptions(
        non_interactive=True, force=True, config_repo="https://example.com/new.git"
    )
    status = setup_wizard.step_config_repo(
        console, options, interactive=False, env_path=env_path, only_requested=False
    )

    assert status == "configured"
    text = env_path.read_text(encoding="utf-8")
    assert "https://example.com/new.git" in text


def test_telegram_step_non_interactive_does_not_overwrite_without_force(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_TELEGRAM_BOT_TOKEN=original-token\n", encoding="utf-8")

    console = _console()
    options = SetupOptions(non_interactive=True, telegram_bot_token="new-token")
    status = setup_wizard.step_telegram(
        console, options, interactive=False, env_path=env_path, only_requested=False
    )

    assert status == "skipped"
    text = env_path.read_text(encoding="utf-8")
    assert "original-token" in text
    assert "new-token" not in text


def test_telegram_step_non_interactive_overwrites_with_force(monkeypatch, tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_TELEGRAM_BOT_TOKEN=original-token\n", encoding="utf-8")

    console = _console()
    options = SetupOptions(non_interactive=True, force=True, telegram_bot_token="new-token")
    status = setup_wizard.step_telegram(
        console, options, interactive=False, env_path=env_path, only_requested=False
    )

    assert status == "configured"
    text = env_path.read_text(encoding="utf-8")
    assert "new-token" in text

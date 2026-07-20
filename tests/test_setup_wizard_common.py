"""Unit tests for `hivepilot.services.setup_wizard_common` -- the leaf
module shared by `setup_wizard` and `setup_wizard_extra` (env upsert,
secret masking, display helpers, shared dataclass/constants). Kept as a
leaf (no imports from either sibling module) specifically to avoid a
circular import between them -- see that module's docstring.

The full end-to-end behavior of these helpers, exercised through
`hivepilot.services.setup_wizard`'s re-exports, is already covered by
`tests/test_setup_wizard.py`; this file just confirms the leaf module is
importable and self-consistent on its own.
"""

from __future__ import annotations

import stat
from pathlib import Path

from hivepilot.services.setup_wizard_common import (
    STEP_NAMES,
    SetupOptions,
    _default_env_path,
    _env_get,
    _env_upsert,
    _mask_secret,
)


def test_step_names_are_unique_and_non_empty() -> None:
    assert len(STEP_NAMES) == len(set(STEP_NAMES))
    assert all(STEP_NAMES)


def test_setup_options_defaults() -> None:
    options = SetupOptions()
    assert options.non_interactive is False
    assert options.only is None
    assert options.timeout == 30
    # HIGH-2 / LOW-2: both new opt-in flags must default to the SAFE choice
    # (never mint a cleartext-cabable token, never silently overwrite).
    assert options.mint_admin_token is False
    assert options.force is False


def test_default_env_path_returns_a_path() -> None:
    assert isinstance(_default_env_path(), Path)


def test_env_upsert_writes_key_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_X", "1")
    assert "HIVEPILOT_X=1" in env_path.read_text(encoding="utf-8")


def test_mask_secret_masks() -> None:
    assert "secretsecret" not in _mask_secret("secretsecretvalue")


# ---------------------------------------------------------------------------
# HIGH-1: the .env file (and any directory this upsert creates) must never
# be world/group readable -- it holds bot tokens and PATs.
# ---------------------------------------------------------------------------


def test_env_upsert_sets_file_mode_0600(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_SECRET", "shh")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600


def test_env_upsert_re_chmods_an_existing_looser_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_A=1\n", encoding="utf-8")
    env_path.chmod(0o644)
    _env_upsert(env_path, "HIVEPILOT_B", "2")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600


def test_env_upsert_creates_parent_dir_with_0700(tmp_path: Path) -> None:
    env_path = tmp_path / "nested" / "dir" / ".env"
    _env_upsert(env_path, "HIVEPILOT_X", "1")
    mode = stat.S_IMODE(env_path.parent.stat().st_mode)
    assert mode == 0o700


# ---------------------------------------------------------------------------
# LOW-1: dotenv-style quoting for values containing whitespace/#/$/"/'.
# ---------------------------------------------------------------------------


def test_env_upsert_quotes_value_with_space_and_hash(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_X", "hello #world")
    text = env_path.read_text(encoding="utf-8")
    assert text == 'HIVEPILOT_X="hello #world"\n'
    # Round-trip: a second upsert of the same key still finds/replaces it
    # in place despite the quoting (key-prefix match is quote-agnostic).
    _env_upsert(env_path, "HIVEPILOT_X", "plain")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["HIVEPILOT_X=plain"]


def test_env_upsert_leaves_plain_values_unquoted(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _env_upsert(env_path, "HIVEPILOT_X", "plain-value-123")
    assert env_path.read_text(encoding="utf-8") == "HIVEPILOT_X=plain-value-123\n"


# ---------------------------------------------------------------------------
# LOW-2: `_env_get` -- read-only lookup backing the non-interactive
# don't-clobber-an-existing-secret guard.
# ---------------------------------------------------------------------------


def test_env_get_returns_existing_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_A=1\nHIVEPILOT_B=2\n", encoding="utf-8")
    assert _env_get(env_path, "HIVEPILOT_B") == "2"


def test_env_get_returns_none_when_absent_or_missing_file(tmp_path: Path) -> None:
    assert _env_get(tmp_path / "nope.env", "HIVEPILOT_A") is None
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_A=\n", encoding="utf-8")
    assert _env_get(env_path, "HIVEPILOT_A") is None

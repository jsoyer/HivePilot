"""`Settings` printed every credential it held in clear text.

Observed live: a pytest error dump rendered ``telegram_bot_token=`` with a
working token in it. pydantic's generated ``__repr__`` prints field values,
and a ``Settings`` instance is a local variable in dozens of frames, so every
traceback and CI log carried the full credential set. This codebase redacts
``detail``, ``stderr`` and prompts at choke points; the ``Settings`` object
walked past all of them.

The fix masks in ``__repr_args__`` -- the hook pydantic derives both ``repr()``
and ``str()`` from -- rather than switching the fields to ``SecretStr``.
``SecretStr`` would mask here too, but it changes the VALUE TYPE, so
``f"Bearer {token}"`` starts rendering ``Bearer **********`` at every
auth-header site. No type checker catches that (f-strings accept any object),
so it would trade a visible leak for silent 401s -- a strictly worse defect,
and the exact "silent success" shape this codebase keeps getting bitten by.

The load-bearing test here is `TestEverySecretFieldIsClassified`: masking a
list is only worth anything if the list cannot silently fall behind the
model. A new ``*_token`` field that nobody classified fails CI.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from hivepilot.config import (
    _PUBLIC_DESPITE_SECRET_NAME,
    _SECRET_SETTING_FIELDS,
    Settings,
)

CANARY = "hp-canary-6f2a1c9e-must-never-be-printed"

# What a reader would call a credential from the name alone. Deliberately
# generous: a false positive costs one line in `_PUBLIC_DESPITE_SECRET_NAME`
# with a reason, a false negative costs a leaked secret.
_CREDENTIAL_NAME = re.compile(
    r"token|secret|password|passwd|api_key|_key$|key_id|signing|credential|webhook_url|dsn|private",
    re.I,
)


def _canary_for(annotation: object) -> Any:
    """A populated value of the right shape for a field of this type."""
    text = str(annotation)
    if "dict" in text:
        return {CANARY: {"source": CANARY}}
    if "list" in text:
        return [CANARY]
    return CANARY


class TestTheLeakIsClosed:
    def test_a_populated_secret_is_masked_in_repr(self) -> None:
        s = Settings(telegram_bot_token=CANARY)

        assert CANARY not in repr(s)
        assert "***set***" in repr(s)

    def test_str_is_masked_too(self) -> None:
        """`str()` is the traceback/f-string path and is derived from the same
        hook -- asserting only on `repr()` would leave it untested."""
        s = Settings(telegram_bot_token=CANARY)

        assert CANARY not in str(s)

    @pytest.mark.parametrize("field", sorted(_SECRET_SETTING_FIELDS))
    def test_no_declared_secret_leaks(self, field: str) -> None:
        """Every field in the set, not just the one that was observed leaking.

        Parametrised rather than looped so a regression names the field.
        """
        payload: dict[str, Any] = {field: _canary_for(Settings.model_fields[field].annotation)}
        s = Settings(**payload)

        assert CANARY not in repr(s)
        assert CANARY not in str(s)


class TestDiagnosticsSurvive:
    def test_an_unset_secret_still_reads_as_unset(self) -> None:
        """Masking, not hiding. "Is it configured?" is a real question an
        operator asks of a repr, and a field that vanishes cannot answer it --
        which is why this does not use `Field(repr=False)`."""
        s = Settings(telegram_bot_token=None)

        assert "telegram_bot_token=None" in repr(s)

    def test_a_non_secret_field_is_untouched(self) -> None:
        s = Settings(api_port=8123)

        assert "api_port=8123" in repr(s)

    def test_the_value_itself_is_unchanged(self) -> None:
        """The whole reason for masking the repr instead of adopting
        `SecretStr`: 55 read sites keep receiving a plain `str`, so no auth
        header silently starts sending `**********`."""
        s = Settings(telegram_bot_token=CANARY)

        assert s.telegram_bot_token == CANARY
        assert f"Bearer {s.telegram_bot_token}" == f"Bearer {CANARY}"


class TestEverySecretFieldIsClassified:
    """The durable half. Without these, the mask silently rots as fields are
    added -- which is how the leak got here in the first place."""

    def test_a_credential_shaped_name_must_be_classified(self) -> None:
        classified = _SECRET_SETTING_FIELDS | _PUBLIC_DESPITE_SECRET_NAME
        unclassified = sorted(
            name
            for name in Settings.model_fields
            if _CREDENTIAL_NAME.search(name) and name not in classified
        )

        assert not unclassified, (
            f"{unclassified} look like credentials but are in neither "
            "_SECRET_SETTING_FIELDS nor _PUBLIC_DESPITE_SECRET_NAME. Add each "
            "to one of them -- to the public set only with a reason."
        )

    def test_the_two_sets_are_disjoint(self) -> None:
        assert not (_SECRET_SETTING_FIELDS & _PUBLIC_DESPITE_SECRET_NAME)

    @pytest.mark.parametrize("field", sorted(_SECRET_SETTING_FIELDS | _PUBLIC_DESPITE_SECRET_NAME))
    def test_every_classified_name_still_exists(self, field: str) -> None:
        """A rename or a typo leaves a name in the set that matches nothing,
        and the field it was meant to cover goes back to printing in clear --
        a mask that fails open, silently."""
        assert field in Settings.model_fields

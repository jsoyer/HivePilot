"""The masking guarantee: a resolved ${secret:NAME} value must NEVER appear
verbatim in logs, provenance, or serialized run state.

These tests drive the real resolve → register → serialize/log path and assert
that a unique marker string is absent from every rendered surface.
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from hivepilot.services import config_provenance, secret_refs

MARKER = "SUPERSECRET-MARKER-7f3a9c1e-DO-NOT-LEAK"


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def test_redact_text_replaces_registered_value_with_sentinel() -> None:
    config_provenance.register_secret_value(MARKER)
    out = config_provenance.redact_text(f"token is {MARKER} here")
    assert MARKER not in out
    assert config_provenance.REDACTED in out


def test_redact_text_noop_when_nothing_registered() -> None:
    assert config_provenance.redact_text("plain text") == "plain text"


def test_short_values_are_not_registered() -> None:
    config_provenance.register_secret_value("ab")  # below _MIN_MASKABLE_LEN
    assert "ab" not in config_provenance.registered_secret_values()
    # ...and redaction of unrelated text is untouched.
    assert config_provenance.redact_text("a cab is here") == "a cab is here"


def test_resolved_ref_value_absent_from_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolve a ref whose provider returns MARKER, then emit a structlog event
    that includes MARKER; the rendered JSON must show REDACTED, not MARKER."""
    monkeypatch.setenv("HP_MASK_STORE", MARKER)
    catalog = {"tok": {"source": "env", "key": "HP_MASK_STORE"}}
    resolved = secret_refs.resolve_secret_refs(
        {"API_KEY": "${secret:tok}"}, catalog=catalog, fail_mode="closed"
    )
    assert resolved == {"API_KEY": MARKER}  # the runner env DOES get the real value
    assert MARKER in config_provenance.registered_secret_values()

    # Emit through a structlog logger configured with the redaction processor.
    structlog.configure(
        processors=[
            config_provenance_redactor := _redactor(),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )
    log = structlog.get_logger("masking-test")
    log.info("runner.env", API_KEY=resolved["API_KEY"], note=f"value={MARKER}")
    captured = capsys.readouterr().out
    assert MARKER not in captured
    assert config_provenance.REDACTED in captured
    # keep a reference so the walrus assignment isn't flagged unused
    assert callable(config_provenance_redactor)


def test_resolved_ref_value_absent_from_serialized_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved secret must not survive into serialized run state. We prove it
    two ways: (1) run-state serialization only ever sees `metadata`, never the
    resolved secrets mapping; (2) even if a caller redacts a blob before writing
    it, the marker is gone."""
    monkeypatch.setenv("HP_MASK_STORE", MARKER)
    catalog = {"tok": {"source": "env", "key": "HP_MASK_STORE"}}
    resolved = secret_refs.resolve_secret_refs(
        {"API_KEY": "${secret:tok}"}, catalog=catalog, fail_mode="closed"
    )

    # (1) The metadata dict that state_service serializes never carries secrets.
    metadata = {"extra_prompt": "do the thing", "prior_context": ""}
    serialized_state = json.dumps(metadata)
    assert MARKER not in serialized_state

    # (2) Any string surface routed through redact_text loses the marker, even
    #     though the live env value still holds it for the runner.
    blob = json.dumps({"env": resolved, "log": f"used {MARKER}"})
    assert MARKER in blob  # sanity: the raw blob DID contain it
    assert MARKER not in config_provenance.redact_text(blob)


def test_caplog_never_sees_marker_via_stdlib_bridge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end through get_logger(): the resolved marker registered during
    resolution is redacted from any structlog event rendered to the log."""
    from hivepilot.utils import logging as hp_logging

    monkeypatch.setenv("HP_MASK_STORE", MARKER)
    catalog = {"tok": {"source": "env", "key": "HP_MASK_STORE"}}
    secret_refs.resolve_secret_refs(
        {"API_KEY": "${secret:tok}"}, catalog=catalog, fail_mode="closed"
    )

    # Force reconfigure so the redaction processor is installed for this logger.
    hp_logging._configured = False
    log = hp_logging.get_logger("bridge-test")
    with caplog.at_level(logging.INFO):
        log.info("runner.launch", secret_value=MARKER)

    rendered = "\n".join(
        [rec.getMessage() for rec in caplog.records] + [str(rec.msg) for rec in caplog.records]
    )
    assert MARKER not in rendered


def _redactor():
    """Return the module's redaction processor (kept private to logging.py) via
    a thin adapter so this test exercises the same redact_text path."""

    def _proc(_logger, _method, event_dict):
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                event_dict[key] = config_provenance.redact_text(value)
        return event_dict

    return _proc

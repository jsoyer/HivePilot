"""Pairing a desktop Orca to a headless one means reading two URLs from its log.

`orca serve` announces them once at startup and nowhere else -- there is no
`orca pairing show`. The exact lines, captured from a real running
`orca-serve.service` on 2026-08-17 (labels verbatim, note "Web client URL",
not "Web URL"):

    Web client URL: http://<host>:<port>/web-index.html#pairing=orca%3A%2F%2Fpair%3Fcode%3D<code>
    Pairing URL: orca://pair?code=<code>

The `code` is base64 JSON and it carries a **deviceToken** — a credential that
grants access to the runtime. It is printed in plaintext to the journal, which
is the operator's own business, but the moment HivePilot handles it, it becomes
our business: it must never reach a `RunResult.detail`, a log line, a PR body,
or a notification. `redact_text` only masks values REGISTERED as secrets, so a
pairing code has to be registered or masked explicitly — being sensitive is not
enough on its own.

Every fixture below uses a synthetic code. Committing a live one would be the
leak this file exists to prevent.
"""

from __future__ import annotations

from hivepilot.services.orca_pairing import extract_pairing, mask_pairing_code

FAKE_CODE = "eyJ2IjoyLCJlbmRwb2ludCI6IndzOi8vMTAuMC4wLjE6Njc2OCJ9"

REAL_SHAPE = f"""
Aug 17 00:32:28 host orca-linux.AppImage[123]: [codex-trust-grant] falling back
Aug 17 00:32:29 host orca-linux.AppImage[123]: Web client URL: http://10.0.0.1:6768/web-index.html#pairing=orca%3A%2F%2Fpair%3Fcode%3D{FAKE_CODE}
Aug 17 00:32:29 host orca-linux.AppImage[123]: Pairing URL: orca://pair?code={FAKE_CODE}
Aug 17 00:32:29 host orca-linux.AppImage[123]: [codex-trust-grant] retry-cached
"""


class TestExtractingBothUrls:
    def test_both_urls_are_found_in_a_real_journal_excerpt(self):
        result = extract_pairing(REAL_SHAPE)

        assert result is not None
        assert result.web_url.startswith("http://10.0.0.1:6768/web-index.html#pairing=")
        assert result.pairing_url == f"orca://pair?code={FAKE_CODE}"
        assert result.code == FAKE_CODE

    def test_the_last_announcement_wins(self):
        """A restarting service announces again. An older code may already be
        revoked, so the freshest one is the only useful one."""
        older = REAL_SHAPE.replace(FAKE_CODE, "OLDcode")
        log = older + REAL_SHAPE

        result = extract_pairing(log)

        assert result.code == FAKE_CODE

    def test_a_log_without_the_lines_yields_nothing(self):
        """Absence must be absence, not a half-filled result -- a pairing URL
        with an empty code would be attempted and fail confusingly."""
        assert extract_pairing("nothing interesting here\nstarting up\n") is None

    def test_a_web_url_alone_is_not_enough(self):
        """Both are reported together by the server; one without the other
        means we matched something else."""
        partial = f"Web client URL: http://10.0.0.1:6768/web-index.html#pairing=x{FAKE_CODE}\n"

        assert extract_pairing(partial) is None

    def test_surrounding_journal_noise_is_ignored(self):
        noisy = "libva error: nope\n" + REAL_SHAPE + "Failed to connect to the bus\n"

        assert extract_pairing(noisy).code == FAKE_CODE


class TestTheCodeIsTreatedAsACredential:
    def test_masking_hides_the_code_in_both_urls(self):
        result = extract_pairing(REAL_SHAPE)

        masked = mask_pairing_code(f"{result.web_url} and {result.pairing_url}", result.code)

        assert FAKE_CODE not in masked
        assert "orca://pair?code=" in masked, "the shape stays readable; only the secret goes"

    def test_masking_covers_the_url_encoded_form(self):
        """The web URL carries the code percent-encoded inside a fragment.
        Masking only the raw form would leave it fully readable there."""
        result = extract_pairing(REAL_SHAPE)
        text = result.web_url

        assert FAKE_CODE not in mask_pairing_code(text, result.code)

    def test_masking_an_empty_code_changes_nothing(self):
        """A guard against masking every character of a message when the code
        failed to parse."""
        assert mask_pairing_code("some text", "") == "some text"

    def test_the_result_does_not_print_its_code(self):
        """`repr` reaches logs and tracebacks by accident more often than any
        deliberate log line does."""
        result = extract_pairing(REAL_SHAPE)

        assert FAKE_CODE not in repr(result)

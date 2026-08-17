"""Read the two URLs a headless Orca announces, and treat the code as a secret.

Pairing a desktop Orca to `orca serve` needs two strings the server prints
once at startup and nowhere else — there is no `orca pairing show`. Captured
verbatim from a running `orca-serve.service` on 2026-08-17 (note the label is
"Web client URL", not "Web URL"):

    Web client URL: http://<host>:<port>/web-index.html#pairing=orca%3A%2F%2Fpair%3Fcode%3D<code>
    Pairing URL: orca://pair?code=<code>

`<code>` is base64 JSON of the form::

    {"v":2,"endpoint":"ws://<host>:<port>","deviceToken":"...",
     "publicKeyB64":"...","pairedDeviceId":"...","scope":"runtime"}

That `deviceToken` grants access to the runtime. The server writing it to its
own journal is the operator's business; the moment HivePilot touches it, it is
ours. `redact_text` masks only values REGISTERED as secrets, so a pairing code
is NOT covered by default — hence `mask_pairing_code`, and hence a `__repr__`
that refuses to print it, because reprs reach logs and tracebacks by accident
far more often than any deliberate log line does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

# Anchored on the labels the server actually emits. `[^\s]+` because a code is
# base64 and a URL is unspaced; journal lines carry a timestamp prefix and
# trailing noise, so neither pattern may assume line boundaries.
_WEB_RE = re.compile(r"Web client URL:\s*(\S+)")
_PAIRING_RE = re.compile(r"Pairing URL:\s*(orca://pair\?code=)(\S+)")


@dataclass(frozen=True)
class Pairing:
    """The pair of URLs, plus the credential they carry.

    Frozen so a caller cannot blank the code and leave the URLs — the three
    fields are only meaningful together.
    """

    web_url: str
    pairing_url: str
    code: str

    def __repr__(self) -> str:  # pragma: no cover - exercised via the test
        return f"Pairing(web_url={self.web_url.split('#')[0]!r}, code=<redacted>)"


def extract_pairing(log_text: str) -> Pairing | None:
    """Pull the pairing announcement out of *log_text*, or None.

    Returns None unless BOTH lines are present. The server emits them
    together, so one without the other means we matched something else — and a
    pairing URL with an empty code would be attempted and fail in a way that
    points nowhere.

    The LAST announcement wins: a restarted service announces again, and an
    older code may already be revoked.
    """
    web_matches = _WEB_RE.findall(log_text)
    pairing_matches = _PAIRING_RE.findall(log_text)
    if not web_matches or not pairing_matches:
        return None

    prefix, code = pairing_matches[-1]
    return Pairing(web_url=web_matches[-1], pairing_url=f"{prefix}{code}", code=code)


def mask_pairing_code(text: str, code: str) -> str:
    """Replace *code* in *text*, in both the raw and percent-encoded forms.

    The web URL carries the code percent-encoded inside a fragment, so masking
    only the raw form would leave it fully readable there — the kind of
    half-applied redaction that reads as done.

    An empty *code* returns *text* unchanged rather than masking every
    character, which is what `str.replace("", ...)` would do.
    """
    if not code:
        return text
    masked = text.replace(code, "<redacted>")
    encoded = quote(code, safe="")
    if encoded != code:
        masked = masked.replace(encoded, "<redacted>")
    return masked

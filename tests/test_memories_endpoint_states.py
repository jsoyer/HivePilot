"""A failed search must not be reported as "not configured".

Probed against the live deployment on 2026-08-17:

    GET /v1/memories?query=... -> {"configured": false, "memories": [],
                                   "detail": "mem0 search failed"}

and the real cause, from the api journal:

    POST https://api.mem0.ai/v3/memories/search/ 400
    {'filters': [ErrorDetail(string='This field is required...')]}

Two defects stacked, and the second is why the first survived:

1. `client.search(query, limit=limit)` omits `filters`, which mem0 v3
   requires. The endpoint has therefore never worked against that version.

2. Both a missing configuration and a failing search return
   `configured: false`. From outside, "mem0 is off" and "mem0 is broken" are
   the same answer -- so an endpoint that was dead for a whole major version
   read as an unused feature.

The `configured` flag answers "is mem0 set up". It must keep answering that,
and nothing else. A search that blows up on a configured client reports
`configured: true` with an `error`, because that is the truth and it is what
sends an operator to the right place.
"""

from __future__ import annotations

import pytest

from hivepilot.services import api_service


class _Client:
    def __init__(self, *, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.raises:
            raise self.raises
        return []


@pytest.fixture
def client(monkeypatch):
    made = _Client()
    monkeypatch.setattr(api_service, "_get_mem0_client", lambda: made)
    return made


class TestTheSearchIsCalledTheWayMem0V3Requires:
    def test_filters_are_passed(self, client):
        """The defect in one assertion: without `filters`, mem0 v3 answers
        400 and the endpoint returns an empty list forever."""
        api_service.list_memories(query="anything")

        assert "filters" in client.calls[0]

    def test_the_query_still_reaches_mem0(self, client):
        api_service.list_memories(query="control characters")

        assert client.calls[0]["query"] == "control characters"


class TestTheThreeStatesAreDistinguishable:
    def test_no_client_is_reported_as_not_configured(self, monkeypatch):
        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: None)

        result = api_service.list_memories(query="x")

        assert result["configured"] is False
        assert result["memories"] == []

    def test_a_failing_search_stays_configured_and_carries_an_error(self, monkeypatch):
        """The heart of it. mem0 IS configured; the call failed. Saying
        `configured: false` sends the operator to check a setting that is
        already correct."""
        monkeypatch.setattr(
            api_service, "_get_mem0_client", lambda: _Client(raises=RuntimeError("400 boom"))
        )

        result = api_service.list_memories(query="x")

        assert result["configured"] is True
        assert result["memories"] == []
        assert result.get("error")

    def test_an_empty_result_is_neither_an_error_nor_unconfigured(self, client):
        """'Nothing matched' is a real, successful answer and must not look
        like a failure -- it is the answer an audit needs to trust."""
        result = api_service.list_memories(query="x")

        assert result["configured"] is True
        assert result["memories"] == []
        assert not result.get("error")


class TestTheFailureIsNotSwallowedSilently:
    def test_the_error_detail_does_not_leak_a_stack_trace(self, monkeypatch):
        """Contract kept from the original: never a 500, never a traceback to
        the client."""
        monkeypatch.setattr(
            api_service,
            "_get_mem0_client",
            lambda: _Client(raises=RuntimeError("secret-token-xyz")),
        )

        result = api_service.list_memories(query="x")

        assert "Traceback" not in str(result)

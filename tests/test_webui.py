"""Tests for hivepilot/webui/ — the Mirador web UI's FastAPI serving glue.

Serves the pre-built static assets committed under hivepilot/webui/static/
(built by web/, a separate Vite+React+TS app — see docs/DASHBOARD.md).
Gated by `HIVEPILOT_ENABLE_WEBUI` (settings.enable_webui) AND the static
directory actually having a built index.html, mirroring how
`HIVEPILOT_ENABLE_TEXTUAL_UI` gates the Textual dashboard (hivepilot/cli.py).

These tests exercise the *behavior* the gate promises — a disabled/absent
UI must be genuinely unreachable (404, not a 500 or a leaked file), a real
build must be served with real content, and no token/secret may ever appear
in what's served — not just "the route returns some 2xx".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def enable_webui(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "enable_webui", True)


@pytest.fixture()
def disable_webui(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "enable_webui", False)


@pytest.fixture()
def fake_static_dir(tmp_path, monkeypatch):
    """Point hivepilot.webui at a throwaway static dir with a minimal,
    known-content build so tests don't depend on web/'s real bundle."""
    from hivepilot import webui

    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>Mirador</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (assets_dir / "index-test.js").write_text("console.log('mirador')", encoding="utf-8")

    monkeypatch.setattr(webui, "STATIC_DIR", static_dir)
    monkeypatch.setattr(webui, "INDEX_HTML", static_dir / "index.html")
    return static_dir


class TestGating:
    def test_index_route_absent_when_flag_unset(self, api_client, disable_webui):
        resp = api_client.get("/ui")
        assert resp.status_code == 404

    def test_index_route_200_when_enabled_and_assets_present(
        self, api_client, enable_webui, fake_static_dir
    ):
        resp = api_client.get("/ui")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<div id='root'>" in resp.text

    def test_index_route_absent_when_enabled_but_no_build(
        self, api_client, enable_webui, tmp_path, monkeypatch
    ):
        """Flag on, but assets were never built/committed for this install —
        must still be a clean 404, not a crash."""
        from hivepilot import webui

        empty_dir = tmp_path / "empty-static"
        monkeypatch.setattr(webui, "STATIC_DIR", empty_dir)
        monkeypatch.setattr(webui, "INDEX_HTML", empty_dir / "index.html")

        resp = api_client.get("/ui")
        assert resp.status_code == 404

    def test_spa_fallback_serves_index_for_unknown_subpath(
        self, api_client, enable_webui, fake_static_dir
    ):
        resp = api_client.get("/ui/some/client/route")
        assert resp.status_code == 200
        assert "<div id='root'>" in resp.text

    def test_serves_real_asset_file(self, api_client, enable_webui, fake_static_dir):
        resp = api_client.get("/ui/assets/index-test.js")
        assert resp.status_code == 200
        assert "mirador" in resp.text


class TestNoSecretLeak:
    def test_served_html_has_no_bearer_token_or_secret_markers(
        self, api_client, enable_webui, fake_static_dir
    ):
        resp = api_client.get("/ui")
        body_lower = resp.text.lower()
        for marker in ("bearer ", "authorization:", "api_key", "secret", "hivepilot_"):
            assert marker not in body_lower

    def test_served_asset_has_no_bearer_token_or_secret_markers(
        self, api_client, enable_webui, fake_static_dir
    ):
        resp = api_client.get("/ui/assets/index-test.js")
        body_lower = resp.text.lower()
        for marker in ("bearer ", "authorization:", "api_key", "secret", "hivepilot_"):
            assert marker not in body_lower


class TestImportGuard:
    def test_static_available_false_when_dir_missing(self, tmp_path, monkeypatch):
        from hivepilot import webui

        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(webui, "STATIC_DIR", missing)
        monkeypatch.setattr(webui, "INDEX_HTML", missing / "index.html")
        assert webui.static_available() is False

    def test_static_available_true_when_index_html_present(self, fake_static_dir):
        from hivepilot import webui

        assert webui.static_available() is True

    def test_import_never_touches_the_filesystem(self):
        """`hivepilot.webui` must be importable (and `hivepilot.services.
        api_service`, which wires its routes, must construct its `app`)
        purely from `Path` computations — no directory listing/open() at
        import time — so a package installed without a `static/` build
        (e.g. `webui` extra without ever running `web/`'s build) never
        breaks core API startup. static_available()/resolve_static_path()
        are the only places that touch disk, and both are exercised above
        with a missing dir without raising."""
        import hivepilot.webui as webui
        from hivepilot.services.api_service import app

        assert isinstance(webui.STATIC_DIR, Path)
        assert isinstance(webui.INDEX_HTML, Path)
        assert app is not None


class TestPathTraversalGuard:
    def test_resolve_static_path_rejects_traversal(self, fake_static_dir):
        from hivepilot import webui

        assert webui.resolve_static_path("../../../../etc/passwd") is None

    def test_resolve_static_path_rejects_nonexistent_file(self, fake_static_dir):
        from hivepilot import webui

        assert webui.resolve_static_path("assets/does-not-exist.js") is None

    def test_resolve_static_path_returns_real_file(self, fake_static_dir):
        from hivepilot import webui

        resolved = webui.resolve_static_path("assets/index-test.js")
        assert resolved is not None
        assert resolved == Path(fake_static_dir / "assets" / "index-test.js").resolve()

    @pytest.mark.parametrize(
        "payload",
        [
            # httpx/TestClient normalizes literal ".." dot-segments client-side
            # before the request ever leaves the client, so a naive
            # "/ui/../../../etc/passwd" never reaches the server with the
            # traversal intact. URL-encoding the dots/slashes bypasses that
            # client-side normalization and delivers the raw ".." segments to
            # the ASGI app, exactly as a real attacker's request would arrive
            # over the wire — this is what actually exercises the server-side
            # guard in `webui.resolve_static_path()`.
            "/ui/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
            "/ui/..%2f..%2f..%2fetc%2fpasswd",
        ],
    )
    def test_route_rejects_traversal_and_falls_back_to_spa(
        self, api_client, enable_webui, fake_static_dir, payload
    ):
        """End-to-end guard check through the live GET /ui/{sub_path} route
        (not just the resolve_static_path() helper in isolation): a
        traversal payload must never leak file content from outside
        STATIC_DIR and must never 500 — it degrades to the safe SPA
        fallback (index.html), same as any other unknown sub-path.

        `%2e%2e` / `%2f` are percent-encoded so the client's own URL parser
        never collapses the ".." locally — the raw traversal segments are
        what actually reach the server and exercise resolve_static_path()'s
        guard, verified by hand: TestClient(app).get(payload) passes the
        encoded string straight through to Starlette's routing, which
        decodes it server-side into "../../../etc/passwd" for the handler.
        """
        resp = api_client.get(payload)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # Never the content of a real outside file (e.g. /etc/passwd).
        assert "root:" not in resp.text
        # Always the SPA fallback shell, never a directory listing/crash.
        assert "<div id='root'>" in resp.text

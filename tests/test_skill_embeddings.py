"""HP-114: optional hybrid RRF, JSON cache, 0 network when disabled."""

from __future__ import annotations

import json
import os
import pickle
from pathlib import Path
from unittest.mock import patch

from hivepilot.host_skills import discover
from hivepilot.services import db, state_service
from hivepilot.skill_catalog import SkillCatalog
from hivepilot.skill_embeddings import (
    SKILL_EMBEDDINGS_ENV,
    cached_vector,
    configured_embedding_provider,
    cosine_similarity,
    decode_vector,
    embeddings_enabled,
    revision_hash,
    rrf_combine,
    set_embedding_provider,
    store_vector,
    vectors_for_documents,
)
from hivepilot.skill_ranker import ranking_text, retrieve
from hivepilot.skill_trust import register_revision

_EMBED_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "skill_embeddings.py"
_RANKER_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "skill_ranker.py"


class MapProvider:
    """Deterministic in-process embedder. Counts embed batches."""

    def __init__(self, table: dict[str, tuple[float, ...]], *, model: str = "test-map") -> None:
        self.table = table
        self.model = model
        self.dims = len(next(iter(table.values())))
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        return [self.table[text] for text in texts]


def _record(catalog: SkillCatalog, name: str, description: str):
    revision = catalog.record(
        name=name,
        files={"SKILL.md": f"---\ndescription: {description}\n---\n# {name}\n"},
        description=description,
    )
    register_revision(
        revision_id=revision.revision_id,
        skill_name=name,
        logical_id=revision.logical_id,
        enabled=True,
    )
    return revision


def _hybrid_catalog() -> SkillCatalog:
    catalog = SkillCatalog()
    _record(catalog, "alpha-docs", "documentation utilisateur pomme")
    _record(catalog, "bravo-install", "installation locale unique")
    _record(catalog, "charlie-revue", "documentation revue adversariale")
    return catalog


def _provider_for(catalog: SkillCatalog, query: str) -> MapProvider:
    table: dict[str, tuple[float, ...]] = {
        query: (1.0, 0.0),
        ranking_text("alpha-docs", catalog.active_revision("alpha-docs").description): (
            0.2,
            0.8,
        ),
        ranking_text("bravo-install", catalog.active_revision("bravo-install").description): (
            0.99,
            0.01,
        ),
        ranking_text("charlie-revue", catalog.active_revision("charlie-revue").description): (
            0.3,
            0.7,
        ),
    }
    return MapProvider(table)


class TestDisabledIsPureBm25:
    def setup_method(self) -> None:
        set_embedding_provider(None)
        os.environ.pop(SKILL_EMBEDDINGS_ENV, None)

    def teardown_method(self) -> None:
        set_embedding_provider(None)
        os.environ.pop(SKILL_EMBEDDINGS_ENV, None)

    def test_flag_defaults_off(self) -> None:
        assert embeddings_enabled() is False
        set_embedding_provider(MapProvider({"x": (1.0,)}))
        assert configured_embedding_provider() is None

    def test_provider_off_matches_bm25_hits_and_scores(self) -> None:
        catalog = _hybrid_catalog()
        query = "documentation"
        expected = retrieve(catalog, query)
        noisy = MapProvider({"nope": (1.0,)})
        set_embedding_provider(noisy)
        got = retrieve(catalog, query)
        assert [(hit.name, hit.score) for hit in got] == [(hit.name, hit.score) for hit in expected]
        assert noisy.calls == 0
        assert [hit.name for hit in expected][0] == "alpha-docs"

    def test_disabled_does_not_touch_the_network(self) -> None:
        catalog = _hybrid_catalog()

        def _boom(*_args, **_kwargs):
            raise AssertionError("disabled retrieve reached the network")

        with (
            patch("socket.create_connection", _boom),
            patch("urllib.request.urlopen", _boom),
        ):
            hits = retrieve(catalog, "documentation utilisateur")
        assert hits[0].name == "alpha-docs"

    def test_configured_provider_ignored_when_flag_off(self) -> None:
        catalog = _hybrid_catalog()
        query = "documentation"
        provider = _provider_for(catalog, query)
        set_embedding_provider(provider)
        report = discover(query, catalog=catalog)
        bm25 = retrieve(catalog, query)
        assert [hit.name for hit in report.hits] == [hit.name for hit in bm25]
        assert provider.calls == 0


class TestNoPickle:
    def test_sources_never_import_pickle(self) -> None:
        blob = _EMBED_SOURCE.read_text(encoding="utf-8") + _RANKER_SOURCE.read_text(
            encoding="utf-8"
        )
        for token in ("import pickle", "pickle.dumps", "pickle.loads", "pickle.dump"):
            assert token not in blob

    def test_cache_roundtrip_is_json_floats(self) -> None:
        key = revision_hash("rev-a", "hash-a", "documentation utilisateur")
        stored = store_vector(key, "test-map", 3, (0.25, -0.5, 1.0))
        assert stored == (0.25, -0.5, 1.0)
        raw = _raw_vector(key, "test-map", 3)
        assert raw is not None
        assert raw.startswith("[")
        assert pickle.dumps((0.25, -0.5, 1.0)) not in raw.encode("utf-8")
        assert json.loads(raw) == [0.25, -0.5, 1.0]
        assert decode_vector(raw, dims=3) == stored
        assert cached_vector(key, "test-map", 3) == stored

    def test_pickle_payload_is_a_cache_miss(self) -> None:
        key = revision_hash("rev-b", "hash-b", "pickle junk")
        _ensure_row(key, "test-map", 2, pickle.dumps([1.0, 0.0]))
        assert cached_vector(key, "test-map", 2) is None
        provider = MapProvider({"doc": (1.0, 0.0)})
        vectors, calls = vectors_for_documents(["doc"], [key], provider)
        assert calls == 1
        assert vectors == ((1.0, 0.0),)
        assert cached_vector(key, "test-map", 2) == (1.0, 0.0)


class TestHybridRrf:
    def setup_method(self) -> None:
        set_embedding_provider(None)
        os.environ.pop(SKILL_EMBEDDINGS_ENV, None)

    def teardown_method(self) -> None:
        set_embedding_provider(None)
        os.environ.pop(SKILL_EMBEDDINGS_ENV, None)

    def test_rrf_is_not_cosine_replace(self) -> None:
        catalog = _hybrid_catalog()
        query = "documentation"
        provider = _provider_for(catalog, query)
        bm25_names = [hit.name for hit in retrieve(catalog, query)]
        hybrid_names = [hit.name for hit in retrieve(catalog, query, provider=provider)]
        assert bm25_names[0] == "alpha-docs"
        assert "bravo-install" not in bm25_names
        assert hybrid_names[0] == "alpha-docs"
        assert "bravo-install" in hybrid_names
        assert hybrid_names != ["bravo-install", "charlie-revue", "alpha-docs"]
        assert hybrid_names != bm25_names

    def test_cache_hit_skips_recompute(self) -> None:
        catalog = _hybrid_catalog()
        query = "documentation"
        provider = _provider_for(catalog, query)
        first = retrieve(catalog, query, provider=provider)
        first_calls = provider.calls
        assert first_calls >= 1
        second = retrieve(catalog, query, provider=provider)
        assert [hit.name for hit in second] == [hit.name for hit in first]
        assert provider.calls == first_calls + 1
        assert provider.texts[-1] == query

    def test_host_discover_uses_hybrid_when_enabled(self) -> None:
        catalog = _hybrid_catalog()
        query = "documentation"
        provider = _provider_for(catalog, query)
        os.environ[SKILL_EMBEDDINGS_ENV] = "1"
        set_embedding_provider(provider)
        report = discover(query, catalog=catalog)
        hybrid = retrieve(catalog, query, provider=provider)
        assert [hit.name for hit in report.hits] == [hit.name for hit in hybrid]
        assert report.hits[0].name == "alpha-docs"
        assert any(hit.name == "bravo-install" for hit in report.hits)

    def test_cosine_and_rrf_helpers(self) -> None:
        assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == 1.0
        assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == 0.0
        fused = rrf_combine(([0, 2, 1], [1, 0, 2]), 3)
        assert fused[0] > fused[1] > fused[2]


def _raw_vector(revision_hash_value: str, model: str, dims: int) -> str | None:
    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                """
                SELECT vector FROM skill_embeddings
                WHERE revision_hash = ? AND model = ? AND dims = ?
                """
            ),
            (revision_hash_value, model, dims),
        ).fetchone()
    if row is None:
        return None
    return str(row["vector"])


def _ensure_row(revision_hash_value: str, model: str, dims: int, payload: bytes) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_embeddings (revision_hash, model, dims, vector)
                VALUES (?, ?, ?, ?)
                """
            ),
            (revision_hash_value, model, dims, payload.decode("latin-1")),
        )

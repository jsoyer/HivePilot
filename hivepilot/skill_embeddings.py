"""HP-114 optional hybrid embeddings — RRF fuse with BM25, JSON cache, no pickle.

OpenSpace embeddings pattern rewritten in Python. This module does **not**
vendor OpenSpace, persist pickle files, replace BM25 with cosine-only
ranking, or call a cloud skill host. The provider is injectable and
**off by default**. Disabled ⇒ zero embed calls and zero network.

Contracts:

- Cache key is ``(revision_hash, model, dims)``. Vectors are JSON float
  arrays in SQLite (or Postgres). Never pickle.
- ``revision_hash`` covers HP-98 ``revision_id`` + ``content_hash`` plus
  the ranking surface (name + description) so a name change cannot reuse
  another skill's vector.
- Reciprocal rank fusion (RRF) of BM25 ranks and cosine ranks. Cosine
  does not replace BM25.
- ``HIVEPILOT_SKILL_EMBEDDINGS`` must be a truthy flag **and** a provider
  must be registered (or passed in) before hybrid runs. Flag off ignores
  a registered provider so tests and hosts stay on pure BM25.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Protocol, Sequence

from hivepilot.services import db, state_service

SKILL_EMBEDDINGS_ENV = "HIVEPILOT_SKILL_EMBEDDINGS"
RRF_K = 60
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class EmbeddingProvider(Protocol):
    """Minimal embedder. Implementations may be local; none ship enabled."""

    @property
    def model(self) -> str: ...

    @property
    def dims(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SkillEmbeddingError(ValueError):
    """Invalid cache key, vector, or provider metadata."""


_registered_provider: EmbeddingProvider | None = None


def embeddings_enabled() -> bool:
    """True only when the optional hybrid flag is explicitly on."""
    raw = (os.environ.get(SKILL_EMBEDDINGS_ENV) or "").strip().casefold()
    return raw in _TRUTHY


def set_embedding_provider(provider: EmbeddingProvider | None) -> None:
    """Register (or clear) the process-wide optional provider."""
    global _registered_provider
    _registered_provider = provider


def configured_embedding_provider() -> EmbeddingProvider | None:
    """Provider used by hosts. ``None`` when the flag is off — no embed I/O."""
    if not embeddings_enabled():
        return None
    return _registered_provider


def revision_hash(revision_id: str, content_hash: str, ranking_text: str) -> str:
    """Stable cache identity for one revision's ranking surface."""
    rev = (revision_id or "").strip()
    digest = (content_hash or "").strip()
    if not rev:
        raise SkillEmbeddingError("revision_id is required")
    if not digest:
        raise SkillEmbeddingError("content_hash is required")
    raw = f"{rev}\0{digest}\0{ranking_text or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def encode_vector(values: Sequence[float]) -> str:
    """JSON array of floats. Never pickle."""
    return json.dumps([float(x) for x in values], separators=(",", ":"))


def decode_vector(raw: str, *, dims: int) -> tuple[float, ...] | None:
    """Parse a JSON vector. Pickle / wrong length / junk → miss."""
    if not raw or dims < 1:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or len(data) != dims:
        return None
    try:
        return tuple(float(x) for x in data)
    except (TypeError, ValueError):
        return None


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Dot / (‖a‖ ‖b‖). Zero-length vectors score 0. No numpy."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def rrf_combine(
    rankings: Sequence[Sequence[int]],
    n_docs: int,
    *,
    k: int = RRF_K,
) -> tuple[float, ...]:
    """Reciprocal rank fusion over index lists (best first)."""
    if n_docs < 0:
        raise SkillEmbeddingError("n_docs must be >= 0")
    if k < 1:
        raise SkillEmbeddingError("rrf k must be >= 1")
    scores = [0.0] * n_docs
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            if idx < 0 or idx >= n_docs:
                raise SkillEmbeddingError("rrf ranking index out of range")
            scores[idx] += 1.0 / (k + rank)
    return tuple(scores)


def cached_vector(revision_hash_value: str, model: str, dims: int) -> tuple[float, ...] | None:
    """Return the JSON-cached vector, or None on miss / corrupt row."""
    key, model_name, width = _cache_identity(revision_hash_value, model, dims)
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                """
                SELECT vector FROM skill_embeddings
                WHERE revision_hash = ? AND model = ? AND dims = ?
                """
            ),
            (key, model_name, width),
        ).fetchone()
    if row is None:
        return None
    return decode_vector(str(row["vector"]), dims=width)


def store_vector(
    revision_hash_value: str,
    model: str,
    dims: int,
    values: Sequence[float],
) -> tuple[float, ...]:
    """Upsert a JSON vector. Rejects dim mismatch."""
    key, model_name, width = _cache_identity(revision_hash_value, model, dims)
    if len(values) != width:
        raise SkillEmbeddingError(f"vector length {len(values)} != dims {width}")
    encoded = encode_vector(values)
    parsed = decode_vector(encoded, dims=width)
    if parsed is None:
        raise SkillEmbeddingError("vector is not a JSON float array")
    _ensure_table()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_embeddings (revision_hash, model, dims, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(revision_hash, model, dims) DO UPDATE SET
                    vector = excluded.vector
                """
            ),
            (key, model_name, width, encoded),
        )
    return parsed


def vectors_for_documents(
    documents: Sequence[str],
    cache_keys: Sequence[str],
    provider: EmbeddingProvider,
) -> tuple[tuple[tuple[float, ...] | None, ...], int]:
    """Load or compute document vectors. Returns (vectors, embed_calls)."""
    if len(documents) != len(cache_keys):
        raise SkillEmbeddingError("documents and cache_keys length mismatch")
    model = _model_name(provider)
    dims = _dims(provider)
    found: list[tuple[float, ...] | None] = []
    missing_idx: list[int] = []
    for idx, key in enumerate(cache_keys):
        cached = cached_vector(key, model, dims)
        found.append(cached)
        if cached is None:
            missing_idx.append(idx)
    calls = 0
    if missing_idx:
        texts = [documents[i] for i in missing_idx]
        embedded = _embed_batch(provider, texts)
        calls = 1
        for loc, vector in zip(missing_idx, embedded):
            stored = store_vector(cache_keys[loc], model, dims, vector)
            found[loc] = stored
    return tuple(found), calls


def fuse_bm25_cosine(
    query: str,
    documents: Sequence[str],
    cache_keys: Sequence[str],
    names: Sequence[str],
    revision_ids: Sequence[str],
    bm25: Sequence[float],
    provider: EmbeddingProvider,
    *,
    rrf_k: int = RRF_K,
) -> tuple[float, ...]:
    """RRF of BM25 and cosine. Provider errors fall back to BM25 scores."""
    n_docs = len(documents)
    if not (n_docs == len(cache_keys) == len(names) == len(revision_ids) == len(bm25)):
        raise SkillEmbeddingError("hybrid inputs must be the same length")
    if n_docs == 0:
        return ()
    try:
        doc_vectors, _calls = vectors_for_documents(documents, cache_keys, provider)
        query_vectors = _embed_batch(provider, [query])
    except Exception:
        return tuple(bm25)
    if not query_vectors:
        return tuple(bm25)
    query_vector = query_vectors[0]
    cosines = [
        cosine_similarity(query_vector, vec) if vec is not None else None for vec in doc_vectors
    ]
    bm25_order = _rank_indices(bm25, names, revision_ids, require_positive=True)
    cosine_order = [
        idx
        for idx, _ in sorted(
            ((i, score) for i, score in enumerate(cosines) if score is not None),
            key=lambda item: (-item[1], names[item[0]], revision_ids[item[0]]),
        )
    ]
    return rrf_combine((bm25_order, cosine_order), n_docs, k=rrf_k)


def _rank_indices(
    scores: Sequence[float],
    names: Sequence[str],
    revision_ids: Sequence[str],
    *,
    require_positive: bool,
) -> list[int]:
    if require_positive:
        indexed = [i for i, score in enumerate(scores) if score > 0.0]
    else:
        indexed = list(range(len(scores)))
    indexed.sort(key=lambda i: (-scores[i], names[i], revision_ids[i]))
    return indexed


def _cache_identity(revision_hash_value: str, model: str, dims: int) -> tuple[str, str, int]:
    key = (revision_hash_value or "").strip()
    model_name = (model or "").strip()
    if not key:
        raise SkillEmbeddingError("revision_hash is required")
    if not model_name:
        raise SkillEmbeddingError("model is required")
    if dims < 1:
        raise SkillEmbeddingError("dims must be >= 1")
    return key, model_name, dims


def _model_name(provider: EmbeddingProvider) -> str:
    name = (provider.model or "").strip()
    if not name:
        raise SkillEmbeddingError("provider.model is required")
    return name


def _dims(provider: EmbeddingProvider) -> int:
    width = int(provider.dims)
    if width < 1:
        raise SkillEmbeddingError("provider.dims must be >= 1")
    return width


def _embed_batch(
    provider: EmbeddingProvider, texts: Sequence[str]
) -> tuple[tuple[float, ...], ...]:
    dims = _dims(provider)
    raw = provider.embed(texts)
    if len(raw) != len(texts):
        raise SkillEmbeddingError("provider returned the wrong batch size")
    vectors: list[tuple[float, ...]] = []
    for item in raw:
        vector = tuple(float(x) for x in item)
        if len(vector) != dims:
            raise SkillEmbeddingError(f"provider vector length {len(vector)} != dims {dims}")
        vectors.append(vector)
    return tuple(vectors)


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_embeddings (
                revision_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                dims INTEGER NOT NULL,
                vector TEXT NOT NULL,
                created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (revision_hash, model, dims)
            )
            """
        )

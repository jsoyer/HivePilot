"""HP-107 local BM25 skill retrieval — deterministic, 0 model queries.

OpenSpace ``skill_engine/skill_ranker`` BM25 stage, rewritten in Python.
This module does **not** vendor a BM25 package, call an embedding API,
persist pickle caches, talk to OpenSpace cloud, or implement HP-114
hybrid RRF / HP-109 apply / HP-112 host skills. HP-108 skill→tools
lives in ``hivepilot.skill_capabilities``.

Contracts:

- Corpus is HP-98 **active** revisions only.
- HP-105 ``enabled`` / provisional filters run **before** scoring so
  disabled and unknown rows never enter IDF.
- Progressive disclosure: ranking uses name + description. ``disclose``
  returns the ``SKILL.md`` body after selection.
- Order is deterministic: ``(-score, name, revision_id)``. Same query +
  same eligible set ⇒ same hits.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from hivepilot.skill_catalog import SkillCatalog, SkillRevision
from hivepilot.skill_trust import TRUSTED, SkillTrust, get

DEFAULT_TOP_K = 10
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_SPLIT = re.compile(r"[^\w]+", flags=re.UNICODE)
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class SkillRankerError(ValueError):
    """Invalid retrieve / disclose arguments."""


@dataclass(frozen=True)
class SkillHit:
    """Ranked skill card. No body — call ``disclose`` after selection."""

    revision_id: str
    logical_id: str
    name: str
    description: str
    score: float
    trust_state: str
    enabled: bool
    known: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "logical_id": self.logical_id,
            "name": self.name,
            "description": self.description,
            "score": self.score,
            "trust_state": self.trust_state,
            "enabled": self.enabled,
            "known": self.known,
        }


@dataclass(frozen=True)
class SkillDisclosure:
    """Full skill payload after the operator (or host) selected a hit."""

    revision_id: str
    logical_id: str
    name: str
    description: str
    body: str
    files: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "logical_id": self.logical_id,
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "files": dict(self.files),
        }


@dataclass(frozen=True)
class _Eligible:
    revision: SkillRevision
    trust: SkillTrust


def tokenize(text: str) -> tuple[str, ...]:
    """Unicode word tokens. ``casefold`` keeps French ranking stable."""
    return tuple(token for token in _TOKEN_SPLIT.split((text or "").casefold()) if token)


def ranking_text(name: str, description: str) -> str:
    """Index surface for BM25. Body is intentionally absent."""
    return f"{name or ''} {description or ''}".strip()


def skill_body(files: Mapping[str, str] | None) -> str:
    """``SKILL.md`` body with YAML frontmatter stripped."""
    if not files:
        return ""
    raw = files.get("SKILL.md")
    if raw is None:
        raw = files.get("skill.md")
    if not raw:
        return ""
    return _FRONTMATTER.sub("", raw, count=1)


def bm25_scores(
    query: str,
    documents: Sequence[str],
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> tuple[float, ...]:
    """Okapi BM25 (ATIRE IDF). Pure Python — no vendor package, no numpy."""
    query_terms = tuple(sorted(set(tokenize(query))))
    if not query_terms or not documents:
        return tuple(0.0 for _ in documents)

    tokenized = [tokenize(doc) for doc in documents]
    n_docs = len(tokenized)
    lengths = [len(tokens) for tokens in tokenized]
    avgdl = (sum(lengths) / n_docs) if n_docs else 0.0

    df: dict[str, int] = {}
    tfs: list[dict[str, int]] = []
    for tokens in tokenized:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        tfs.append(counts)
        for token in counts:
            df[token] = df.get(token, 0) + 1

    scores: list[float] = []
    for freq, length in zip(tfs, lengths):
        score = 0.0
        for term in query_terms:
            tf = freq.get(term, 0)
            if tf <= 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log((n_docs - n_q + 0.5) / (n_q + 0.5) + 1.0)
            denom = tf + k1 * (1.0 - b + b * (length / avgdl if avgdl else 0.0))
            score += idf * (tf * (k1 + 1.0) / denom) if denom else 0.0
        scores.append(score)
    return tuple(scores)


def eligible_revisions(
    catalog: SkillCatalog,
    *,
    tenant: str = "default",
    include_provisional: bool = True,
) -> tuple[SkillRevision, ...]:
    """Active revisions that pass HP-105 enabled / provisional gates."""
    return tuple(row.revision for row in _eligible(catalog, tenant, include_provisional))


def _eligible(
    catalog: SkillCatalog,
    tenant: str,
    include_provisional: bool,
) -> tuple[_Eligible, ...]:
    rows: list[_Eligible] = []
    for revision in catalog.list_active():
        trust = get(
            revision.revision_id,
            tenant=tenant,
            skill_name=revision.name,
            logical_id=revision.logical_id,
        )
        if not _passes_trust_filter(trust, include_provisional=include_provisional):
            continue
        rows.append(_Eligible(revision=revision, trust=trust))
    return tuple(rows)


def _passes_trust_filter(trust: SkillTrust, *, include_provisional: bool) -> bool:
    if not trust.known or not trust.enabled:
        return False
    if include_provisional:
        return True
    return trust.trust_state == TRUSTED


class SkillRanker:
    """Local BM25 over a catalog. Filters first, then scores, then discloses."""

    def __init__(self, catalog: SkillCatalog, *, tenant: str = "default") -> None:
        self.catalog = catalog
        self.tenant = (tenant or "default").strip() or "default"

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        include_provisional: bool = True,
    ) -> tuple[SkillHit, ...]:
        """Rank eligible skills. Hits never carry a body."""
        if top_k < 1:
            raise SkillRankerError("top_k must be >= 1")
        if not (query or "").strip():
            return ()
        pool = _eligible(self.catalog, self.tenant, include_provisional)
        if not pool:
            return ()
        documents = [ranking_text(row.revision.name, row.revision.description) for row in pool]
        scores = bm25_scores(query, documents)
        hits = [
            SkillHit(
                revision_id=row.revision.revision_id,
                logical_id=row.revision.logical_id,
                name=row.revision.name,
                description=row.revision.description,
                score=score,
                trust_state=row.trust.trust_state,
                enabled=row.trust.enabled,
                known=row.trust.known,
            )
            for row, score in zip(pool, scores)
            if score > 0.0
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.name, hit.revision_id))
        return tuple(hits[:top_k])

    def disclose(self, revision_id: str) -> SkillDisclosure | None:
        """Return the selected revision's files and stripped body."""
        rev = (revision_id or "").strip()
        if not rev:
            raise SkillRankerError("revision_id is required")
        for revision in self.catalog.list_active():
            if revision.revision_id == rev:
                return SkillDisclosure(
                    revision_id=revision.revision_id,
                    logical_id=revision.logical_id,
                    name=revision.name,
                    description=revision.description,
                    body=skill_body(revision.files),
                    files=dict(revision.files),
                )
        return None


def retrieve(
    catalog: SkillCatalog,
    query: str,
    *,
    tenant: str = "default",
    top_k: int = DEFAULT_TOP_K,
    include_provisional: bool = True,
) -> tuple[SkillHit, ...]:
    """Module-level retrieve used by tests and later hosts."""
    return SkillRanker(catalog, tenant=tenant).retrieve(
        query,
        top_k=top_k,
        include_provisional=include_provisional,
    )


def disclose(
    catalog: SkillCatalog,
    revision_id: str,
    *,
    tenant: str = "default",
) -> SkillDisclosure | None:
    """Module-level disclose. ``tenant`` is accepted for API symmetry."""
    _ = tenant
    return SkillRanker(catalog).disclose(revision_id)

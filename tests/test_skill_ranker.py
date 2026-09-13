"""HP-107: local BM25 retrieval, HP-105 prefilters, FR golden ranking."""

from __future__ import annotations

from pathlib import Path

from hivepilot.skill_catalog import SkillCatalog
from hivepilot.skill_events import record_skill_event
from hivepilot.skill_ranker import (
    SkillRanker,
    SkillRankerError,
    bm25_scores,
    disclose,
    eligible_revisions,
    ranking_text,
    retrieve,
    skill_body,
    tokenize,
)
from hivepilot.skill_trust import (
    PROVISIONAL,
    TRUSTED,
    evaluate_promotion,
    get,
    register_revision,
    set_enabled,
)

_RANKER_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "skill_ranker.py"

# Distinct French surfaces so BM25 order is unambiguous.
_GOLDEN_FR: tuple[tuple[str, str, str], ...] = (
    (
        "revue-code",
        "Revue adversariale d'un diff de code. Vérifie les conventions et les régressions.",
        "# Revue\n\nInspecter le diff ligne par ligne.\n",
    ),
    (
        "redaction-docs",
        "Rédiger la documentation utilisateur et les guides d'installation.",
        "# Docs\n\nDéployer une application en production avec un plan de rollback.\n",
    ),
    (
        "deploiement-prod",
        "Déployer une application en production avec un plan de rollback.",
        "# Prod\n\nChecklist de mise en ligne.\n",
    ),
    (
        "traduction-fr",
        "Traduire un texte technique de l'anglais vers le français.",
        "# Traduction\n\nGlossaire métier.\n",
    ),
)

_GOLDEN_QUERIES: tuple[tuple[str, str], ...] = (
    ("rédiger la documentation utilisateur", "redaction-docs"),
    ("revue adversariale d'un diff", "revue-code"),
    ("déployer en production rollback", "deploiement-prod"),
    ("traduire un texte technique", "traduction-fr"),
)


def _record(
    catalog: SkillCatalog,
    name: str,
    description: str,
    body: str,
    *,
    enabled: bool = True,
    extra_files: dict[str, str] | None = None,
):
    files = {"SKILL.md": f"---\ndescription: {description}\n---\n{body}"}
    if extra_files:
        files.update(extra_files)
    revision = catalog.record(name=name, files=files, description=description)
    trust = register_revision(
        revision_id=revision.revision_id,
        skill_name=name,
        logical_id=revision.logical_id,
        enabled=enabled,
    )
    return revision, trust


def _golden_catalog(*, shuffle: bool = False) -> SkillCatalog:
    catalog = SkillCatalog()
    rows = list(_GOLDEN_FR)
    if shuffle:
        rows = list(reversed(rows))
    for name, description, body in rows:
        _record(catalog, name, description, body)
    return catalog


def _promote(catalog: SkillCatalog, name: str) -> None:
    revision = catalog.active_revision(name)
    assert revision is not None
    for run_id in (1, 2):
        record_skill_event(
            event_type="completed",
            revision_id=revision.revision_id,
            logical_id=revision.logical_id,
            skill_name=name,
            run_id=run_id,
            step="impl",
        )
    evaluate_promotion(revision.revision_id)
    assert get(revision.revision_id).trusted is True


class TestLocalBm25:
    def test_source_has_no_model_or_vendor_hooks(self) -> None:
        source = _RANKER_SOURCE.read_text(encoding="utf-8")
        for token in (
            "from rank_bm25",
            "import rank_bm25",
            "import pickle",
            "openai",
            "text-embedding",
            "urllib.request",
            "sentence_transformers",
            "openspace.skill_engine",
        ):
            assert token not in source

    def test_tokenize_is_unicode_and_casefold(self) -> None:
        tokens = tokenize("Rédiger la Documentation")
        assert "rédiger" in tokens
        assert "documentation" in tokens
        assert tokenize("RÉDIGER") == tokenize("rédiger")

    def test_identical_queries_same_scores(self) -> None:
        docs = ["alpha beta", "beta gamma", "delta"]
        first = bm25_scores("beta", docs)
        second = bm25_scores("beta", docs)
        assert first == second
        assert first[1] > first[2]


class TestFilterBeforeScoring:
    def test_unknown_and_disabled_are_excluded(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "visible", "Guide d'installation visible", "# ok\n")
        ghost = catalog.record(
            name="fantome",
            files={"SKILL.md": "terme unique fantome"},
            description="terme unique fantome",
        )
        disabled, _ = _record(
            catalog,
            "masque",
            "terme unique masque pour le déploiement",
            "# secret\n",
            enabled=True,
        )
        set_enabled(disabled.revision_id, False)

        names = {rev.name for rev in eligible_revisions(catalog)}
        assert names == {"visible"}
        assert get(ghost.revision_id).known is False
        assert get(ghost.revision_id).enabled is False

        hits = retrieve(catalog, "terme unique masque")
        assert hits == ()
        assert retrieve(catalog, "terme unique fantome") == ()
        assert [hit.name for hit in retrieve(catalog, "installation visible")] == ["visible"]

    def test_provisional_included_until_opted_out(self) -> None:
        catalog = _golden_catalog()
        _promote(catalog, "revue-code")
        all_hits = retrieve(catalog, "documentation utilisateur")
        assert [hit.name for hit in all_hits][0] == "redaction-docs"
        assert get(catalog.active_revision("redaction-docs").revision_id).trust_state == PROVISIONAL

        trusted_only = retrieve(
            catalog,
            "revue adversariale",
            include_provisional=False,
        )
        assert [hit.name for hit in trusted_only] == ["revue-code"]
        assert trusted_only[0].trust_state == TRUSTED

    def test_disabled_does_not_enter_idf_corpus(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "alpha", "pomme poire", "# a\n")
        _record(catalog, "beta", "pomme kiwi", "# b\n")
        rare, _ = _record(catalog, "gamma", "pomme unique-idf", "# c\n")
        set_enabled(rare.revision_id, False)
        hits = retrieve(catalog, "pomme")
        assert {hit.name for hit in hits} == {"alpha", "beta"}
        pool = eligible_revisions(catalog)
        filtered = bm25_scores(
            "pomme",
            [ranking_text(row.name, row.description) for row in pool],
        )
        by_name = {hit.name: hit.score for hit in hits}
        expected = {row.name: score for row, score in zip(pool, filtered)}
        assert by_name == expected
        with_disabled = bm25_scores(
            "pomme",
            [
                ranking_text("alpha", "pomme poire"),
                ranking_text("beta", "pomme kiwi"),
                ranking_text("gamma", "pomme unique-idf"),
            ],
        )
        assert with_disabled[0] != expected["alpha"]


class TestProgressiveDisclosure:
    def test_hits_have_no_body_until_disclose(self) -> None:
        catalog = _golden_catalog()
        hits = retrieve(catalog, "rédiger la documentation")
        assert hits
        assert "body" not in hits[0].to_dict()
        assert not hasattr(hits[0], "body")
        assert not hasattr(hits[0], "files")

        card = disclose(catalog, hits[0].revision_id)
        assert card is not None
        assert "Déployer une application en production" in card.body
        assert card.body.startswith("# Docs")
        assert "---" not in card.body
        assert "SKILL.md" in card.files

    def test_body_is_not_in_the_index(self) -> None:
        catalog = _golden_catalog()
        index = ranking_text("redaction-docs", catalog.active_revision("redaction-docs").description)
        assert "rollback" not in index
        hits = retrieve(catalog, "déployer en production rollback")
        assert hits[0].name == "deploiement-prod"
        assert all(hit.name != "redaction-docs" for hit in hits)

    def test_skill_body_strips_frontmatter(self) -> None:
        text = skill_body({"SKILL.md": "---\ndescription: x\n---\n# Corps\n"})
        assert text == "# Corps\n"

    def test_disclose_unknown_revision_is_none(self) -> None:
        catalog = _golden_catalog()
        assert disclose(catalog, "missing") is None

    def test_disclose_requires_id(self) -> None:
        try:
            SkillRanker(SkillCatalog()).disclose("")
        except SkillRankerError as exc:
            assert "revision_id" in str(exc)
        else:
            raise AssertionError("expected SkillRankerError")


class TestGoldenRankingFr:
    def test_french_queries_rank_expected_skill_first(self) -> None:
        catalog = _golden_catalog()
        for query, expected in _GOLDEN_QUERIES:
            hits = retrieve(catalog, query)
            assert hits, query
            assert hits[0].name == expected, (query, [hit.name for hit in hits])
            assert hits[0].score > 0
            assert all(left.score >= right.score for left, right in zip(hits, hits[1:]))

    def test_order_is_stable_across_insert_order_and_repeats(self) -> None:
        first = [hit.name for hit in retrieve(_golden_catalog(), "documentation utilisateur")]
        second = [hit.name for hit in retrieve(_golden_catalog(), "documentation utilisateur")]
        shuffled = [
            hit.name for hit in retrieve(_golden_catalog(shuffle=True), "documentation utilisateur")
        ]
        assert first == second == shuffled
        assert first[0] == "redaction-docs"

    def test_empty_query_returns_nothing(self) -> None:
        catalog = _golden_catalog()
        assert retrieve(catalog, "") == ()
        assert retrieve(catalog, "   ") == ()

    def test_top_k_and_invalid_limit(self) -> None:
        catalog = _golden_catalog()
        hits = retrieve(catalog, "la", top_k=1)
        assert len(hits) == 1
        try:
            retrieve(catalog, "la", top_k=0)
        except SkillRankerError as exc:
            assert "top_k" in str(exc)
        else:
            raise AssertionError("expected SkillRankerError")

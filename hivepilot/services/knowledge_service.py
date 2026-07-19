"""Knowledge/context service for agent prompts.

Default path reads the declared ``knowledge_files`` directly (no ML deps) — this
keeps the core install lightweight and lets the CLI start without langchain/torch.

If the optional embedding stack is installed (``hivepilot[langchain]`` —
langchain, langchain-community, faiss-cpu, sentence-transformers, torch) a FAISS
similarity search is used instead, which is worthwhile for large corpora. For a
couple of small doc files the plain read is equivalent and far cheaper.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

FEEDBACK_DIR = settings.base_dir / ".hivepilot" / "feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
EMBED_DIR = settings.base_dir / ".hivepilot" / "embeddings"

# Cap per-file content in the plain-read path to keep prompts bounded.
_MAX_CHARS_PER_FILE = 4000


def build_context(project_path: Path, files: Iterable[Path]) -> str:
    """Build prompt context from *files* under *project_path*.

    Uses the optional embedding RAG when available, otherwise reads the files
    directly. Appends recent AI feedback either way.
    """
    file_list = list(files)
    sections = _embedding_context(project_path, file_list)
    if sections is None:
        sections = _plain_context(project_path, file_list)

    feedback = _latest_feedback(project_path, limit=5)
    if feedback:
        sections.append("Recent AI feedback:\n" + "\n".join(feedback))
    return "\n\n".join(sections)


def append_feedback(project_path: Path, task_name: str, summary: str) -> None:
    # Choke point: `summary` is `f"{target} -> ... ({result.detail or ...})"`
    # built from a task run's own result detail, which can echo a resolved
    # ${secret:NAME} value. Redact before it's appended to the vault feedback log.
    from hivepilot.services.config_provenance import redact_text

    summary = redact_text(summary)
    log_path = FEEDBACK_DIR / f"{project_path.name}.jsonl"
    entry = {
        "task": task_name,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    logger.info("knowledge.feedback.append", project=project_path.name)


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _plain_context(project_path: Path, files: list[Path]) -> list[str]:
    """Read each knowledge file directly (truncated). No external deps."""
    sections: list[str] = []
    for file in files:
        full = project_path / file
        if not full.exists():
            continue
        text = full.read_text(encoding="utf-8", errors="ignore")
        if len(text) > _MAX_CHARS_PER_FILE:
            text = text[:_MAX_CHARS_PER_FILE] + "\n…(truncated)"
        sections.append(f"# {file}\n{text}")
    return sections


def _embedding_context(project_path: Path, files: list[Path]) -> list[str] | None:
    """Optional RAG path. Returns ``None`` if the embedding stack is unavailable
    or the embedding build fails, so the caller falls back to the plain read."""
    if not files:
        return None
    try:
        from langchain.text_splitter import RecursiveCharacterTextSplitter
        from langchain_community.embeddings import HuggingFaceEmbeddings
        from langchain_community.vectorstores import FAISS
    except Exception:
        return None

    try:
        EMBED_DIR.mkdir(parents=True, exist_ok=True)
        embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        digest = _hash_files(project_path, files)
        vector_path = EMBED_DIR / f"{project_path.name}-{digest}"
        if vector_path.exists():
            logger.info("knowledge.cache_hit", project=str(project_path))
            vectors = FAISS.load_local(
                str(vector_path), embeddings, allow_dangerous_deserialization=True
            )
        else:
            contents = [
                (project_path / f).read_text(encoding="utf-8", errors="ignore")
                for f in files
                if (project_path / f).exists()
            ]
            splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
            documents = splitter.create_documents(["\n".join(contents)])
            vectors = FAISS.from_documents(documents, embeddings)
            vectors.save_local(str(vector_path))
            logger.info("knowledge.cache_build", project=str(project_path), chunks=len(documents))
        docs = vectors.similarity_search("Provide key documentation context", k=5)
        return [doc.page_content for doc in docs]
    except Exception as exc:  # noqa: BLE001 — never let optional RAG break a run
        logger.warning("knowledge.embedding_failed", error=str(exc))
        return None


def _latest_feedback(project_path: Path, limit: int = 5) -> list[str]:
    log_path = FEEDBACK_DIR / f"{project_path.name}.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines[-limit:]]
    return [f"[{e['timestamp']}] {e['task']}: {e['summary']}" for e in entries]


def _hash_files(project_path: Path, files: Iterable[Path]) -> str:
    hasher = hashlib.sha256()
    for file in sorted(files):
        full = project_path / file
        if full.exists():
            hasher.update(full.read_bytes())
    return hasher.hexdigest()[:12]


def build_lessons_context(
    project: str, role: str | None, task: str | None, *, limit: int | None = None
) -> str:
    """Return the formatted ``Lessons learned`` block for *project*/*role*/
    *task* -- VALIDATED lessons only (Auto-Learning Lessons Loop PRD,
    Sprint 3's fail-closed gate), ranked score desc / recency desc, capped
    at ``limit`` (default: `settings.lesson_inject_limit`).

    GATE: returns ``""`` when `settings.enable_lesson_distillation` is
    False -- checked FIRST, before touching the database at all, so the
    flags-off path is byte-identical to before this Sprint (no import of
    `state_service`/`lessons_service`, no query, no injected section) --
    and also returns ``""`` when there are simply no validated lessons for
    this key. Both callers (`ClaudeRunner._build_prompt`/`PromptCliRunner.
    _augment_prompt`) must treat an empty string exactly like "no Knowledge
    context": omit the section entirely.

    Calls `state_service.mark_lesson_used` for every lesson actually
    returned (best-effort -- a persistence hiccup here must never break
    prompt assembly for the run itself).
    """
    from hivepilot.config import settings

    if not settings.enable_lesson_distillation:
        return ""

    from hivepilot.services import state_service
    from hivepilot.services.lessons_service import retrieve_lessons

    effective_limit = limit if limit is not None else settings.lesson_inject_limit
    lessons = retrieve_lessons(project, role=role, task=task, limit=effective_limit)
    if not lessons:
        return ""

    for lesson in lessons:
        if lesson.id is not None:
            try:
                state_service.mark_lesson_used(lesson.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lessons.mark_used_failed", lesson_id=lesson.id, error=str(exc))

    return "\n".join(f"- {lesson.text}" for lesson in lessons)

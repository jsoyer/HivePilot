from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

EMBED_DIR = settings.base_dir / ".hivepilot" / "embeddings"
EMBED_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_DIR = settings.base_dir / ".hivepilot" / "feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")


def build_context(project_path: Path, files: Iterable[Path]) -> str:
    vectors = _load_or_build(project_path, files)
    docs = vectors.similarity_search("Provide key documentation context", k=5)
    sections = [doc.page_content for doc in docs]
    feedback = _latest_feedback(project_path, limit=5)
    if feedback:
        sections.append("Recent AI feedback:\n" + "\n".join(feedback))
    return "\n\n".join(sections)


def append_feedback(project_path: Path, task_name: str, summary: str) -> None:
    log_path = FEEDBACK_DIR / f"{project_path.name}.jsonl"
    entry = {
        "task": task_name,
        "summary": summary,
        "timestamp": datetime.utcnow().isoformat(),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    logger.info("knowledge.feedback.append", project=project_path.name)


def _latest_feedback(project_path: Path, limit: int = 5) -> list[str]:
    log_path = FEEDBACK_DIR / f"{project_path.name}.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines[-limit:]]
    return [f"[{e['timestamp']}] {e['task']}: {e['summary']}" for e in entries]


def _load_or_build(project_path: Path, files: Iterable[Path]):
    digest = _hash_files(project_path, files)
    vector_path = EMBED_DIR / f"{project_path.name}-{digest}"
    if vector_path.exists():
        logger.info("knowledge.cache_hit", project=project_path)
        return FAISS.load_local(str(vector_path), embeddings, allow_dangerous_deserialization=True)

    contents = []
    for file in files:
        full = project_path / file
        if full.exists():
            contents.append(full.read_text(encoding="utf-8", errors="ignore"))
    text = "\n".join(contents)
    documents = splitter.create_documents([text])
    vectorstore = FAISS.from_documents(documents, embeddings)
    vectorstore.save_local(str(vector_path))
    logger.info("knowledge.cache_build", project=project_path, chunks=len(documents))
    return vectorstore


def _hash_files(project_path: Path, files: Iterable[Path]) -> str:
    hasher = hashlib.sha256()
    for file in sorted(files):
        full = project_path / file
        if full.exists():
            hasher.update(full.read_bytes())
    return hasher.hexdigest()[:12]

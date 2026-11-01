import json, os
from pathlib import Path
from typing import Any
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

BASE = Path(__file__).resolve().parent
KNOWLEDGE_PATH = BASE / "knowledge.json"
CHROMA_PATH = BASE / "chroma_db"
COLLECTION = "oracle_legacy_knowledge"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

def load_issues() -> list[dict[str, Any]]:
    if not KNOWLEDGE_PATH.exists():
        raise FileNotFoundError(f"Put knowledge.json at {KNOWLEDGE_PATH}")
    data = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        raise ValueError("knowledge.json must contain an issues list")
    return issues

def confidence_level(issue: dict) -> str:
    c = issue.get("confidence", {})
    return str(c.get("level", "unknown")) if isinstance(c, dict) else str(c or "unknown")

def issue_to_document(issue: dict) -> Document:
    signals = issue.get("detection_signals", {})
    sources = issue.get("sources", [])
    urls = [s.get("url", "") for s in sources if s.get("url")]
    text = "\n".join([
        f"Title: {issue.get('title', '')}",
        f"Category: {issue.get('category', '')}",
        f"Symptoms: {'; '.join(issue.get('symptoms', []))}",
        f"Keywords: {'; '.join(signals.get('keywords', []))}",
        f"Error codes: {'; '.join(signals.get('error_codes', []))}",
        f"Scenario: {issue.get('reported_scenario', '')}",
        f"Verified facts: {'; '.join(issue.get('verified_facts', []))}",
        f"Diagnostics: {'; '.join(issue.get('diagnostic_steps', []))}",
        f"Recommendations: {'; '.join(issue.get('recommended_actions', []))}",
        f"Do not: {'; '.join(issue.get('do_not', []))}",
        f"Sources: {'; '.join(urls)}",
    ])
    versions = issue.get("database_versions", {})
    target = versions.get("reported_target_version") or versions.get("target_version") or ""
    metadata = {
        "issue_id": str(issue.get("id", "unknown")),
        "title": str(issue.get("title", "")),
        "category": str(issue.get("category", "unknown")),
        "confidence_level": confidence_level(issue),
        "target_version": str(target),
        "source_type": ",".join(sorted({str(s.get("type", "unknown")) for s in sources})),
        "source_urls": " | ".join(urls),
    }
    return Document(page_content=text, metadata=metadata)

def get_embeddings():
    return HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)

def get_vector_store():
    return Chroma(collection_name=COLLECTION, embedding_function=get_embeddings(),
                  persist_directory=str(CHROMA_PATH))

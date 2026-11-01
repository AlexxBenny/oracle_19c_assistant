import re
from collections import defaultdict
from typing import Any
from rank_bm25 import BM25Okapi
from store import get_vector_store, load_issues, issue_to_document, confidence_level

def tokenize(text: str) -> list[str]:
    # Keep error codes and Oracle identifiers intact.
    return re.findall(r"[A-Za-z]+-\d+|[A-Za-z_][A-Za-z0-9_$#]*|\d+", text.lower())

_issues = load_issues()
_docs = [issue_to_document(i) for i in _issues]
_by_id = {str(i.get("id")): i for i in _issues}
_bm25 = BM25Okapi([tokenize(d.page_content) for d in _docs]) if _docs else None
_vector_store = get_vector_store()


def _keyword_search(query: str, limit: int):
    if not _bm25 or not _docs:
        return []
    scores = _bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [_docs[i] for i in order if scores[i] > 0][:limit]

def _rrf(lists, k=60):
    scores = defaultdict(float)
    for docs in lists:
        seen = set()
        for rank, doc in enumerate(docs, 1):
            ident = str(doc.metadata.get("issue_id", ""))
            if ident and ident not in seen:
                scores[ident] += 1 / (k + rank)
                seen.add(ident)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)

def hybrid_retrieve(query: str, oracle_version: str = "19c",
                    top_k: int = 3, candidate_k: int = 5) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    # Don't append version as a keyword; it causes irrelevant version-only matches.
    vector_docs = _vector_store.similarity_search(query, k=max(top_k, candidate_k))
    keyword_docs = _keyword_search(query, max(top_k, candidate_k))
    fused = _rrf([vector_docs, keyword_docs])
    results = []
    for ident, score in fused:
        issue = _by_id.get(ident)
        if not issue:
            continue
        results.append({
            "id": ident,
            "title": issue.get("title", ""),
            "category": issue.get("category", ""),
            "confidence": issue.get("confidence", {"level": "unknown"}),
            "diagnostic_steps": issue.get("diagnostic_steps", []),
            "recommended_actions": issue.get("recommended_actions", []),
            "do_not": issue.get("do_not", []),
            "sources": issue.get("sources", []),
            "retrieval_score": round(score, 6),
        })
        if len(results) >= top_k:
            break
    return results

def get_retrieval_status():
    return {"knowledge_records": len(_issues),
            "chroma_records": _vector_store._collection.count(),
            "retrievers": ["chroma_vector_similarity", "bm25_keyword"],
            "fusion": "reciprocal_rank_fusion"}

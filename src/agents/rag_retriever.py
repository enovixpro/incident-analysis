"""
RAG retriever.

CONCEPT: RAG. For each incident, query the vector store for similar past
incidents. The remediation agent uses these matches as grounding context.

This is wired against the real ChromaDB even in scaffold mode because it's
deterministic utility code, not agent logic.
"""
from __future__ import annotations

from src.state import IncidentState, RAGMatch, make_step, now_iso
from src.tools import vectorstore


def run(state: IncidentState) -> dict:
    started = now_iso()
    matches: list[RAGMatch] = []
    errors: list[str] = []

    for inc in state.incidents:
        query = f"{inc.title}\n{inc.summary}"
        try:
            hits = vectorstore.query_similar(query, k=3)
        except Exception as e:
            errors.append(f"rag: {e}")
            hits = []

        for h in hits:
            matches.append(
                RAGMatch(
                    incident_id=inc.id,
                    past_id=h.get("id", ""),
                    past_title=h["title"],
                    past_summary=h["summary"],
                    past_remediation=h["remediation"],
                    similarity=h["similarity"],
                )
            )

    out = {
        "rag_matches": matches,
        "trace": [make_step("rag_retriever", started, note=f"Retrieved {len(matches)} similar past incident(s)")],
    }
    if errors:
        out["errors"] = errors
    return out

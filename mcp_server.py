"""
MCP server — exposes the incident-analysis pipeline to any MCP client
(Claude Desktop, Claude CLI, Cursor, …).

Tools:
  - analyze_logs        — run the full multi-agent pipeline on a raw log string
  - analyze_log_file    — convenience: read a log file from disk, then analyze
  - search_past_incidents — RAG-only lookup over the seeded ChromaDB collection

Transport: stdio. Configure your client to spawn this process as a subprocess.

CRITICAL: stdio MCP servers must never write to stdout — that channel is reserved
for protocol frames. We force `LANGSMITH_TRACING=false` and route the agents'
stdlib logging to stderr so any stray prints from upstream code don't corrupt
the protocol.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

# Keep stdout clean for MCP frames.
os.environ.pop("LANGSMITH_TRACING", None)

logging.basicConfig(
    level=os.getenv("MCP_LOG_LEVEL", "WARNING").upper(),
    stream=sys.stderr,
    format="[mcp %(levelname)s %(name)s] %(message)s",
)
logger = logging.getLogger("incident-suite.mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src.graph import get_graph  # noqa: E402
from src.state import IncidentState  # noqa: E402
from src.tools import vectorstore  # noqa: E402

mcp = FastMCP("incident-suite")


# ---------- Output shaping --------------------------------------------------------

def _shape_pipeline_result(state: IncidentState) -> dict[str, Any]:
    """Trim the full IncidentState down to the actionable fields a caller wants."""
    rem_by_inc = {r.incident_id: r for r in state.remediations}
    crit_by_inc = {c.incident_id: c for c in state.critique}
    rag_by_inc: dict[str, list] = {}
    for m in state.rag_matches:
        rag_by_inc.setdefault(m.incident_id, []).append(m)

    incidents = []
    for inc in state.incidents:
        rem = rem_by_inc.get(inc.id)
        crit = crit_by_inc.get(inc.id)
        rag = rag_by_inc.get(inc.id, [])
        incidents.append({
            "id": inc.id,
            "title": inc.title,
            "category": inc.category.value,
            "severity": inc.severity.value,
            "summary": inc.summary,
            "affected_components": inc.affected_components,
            "event_count": inc.event_count,
            "root_cause": rem.root_cause if rem else None,
            "remediation_steps": rem.steps if rem else [],
            "remediation_rationale": rem.rationale if rem else None,
            "remediation_revision": rem.revision if rem else None,
            "references_past_incidents": rem.references if rem else [],
            "critic": {
                "approved": crit.approved if crit else None,
                "issues": crit.issues if crit else [],
                "suggestion": crit.suggestion if crit else None,
            },
            "rag_matches": [
                {"past_id": m.past_id, "title": m.past_title, "similarity": round(m.similarity, 3)}
                for m in rag
            ],
        })

    return {
        "incidents": incidents,
        "runbook": [
            {"title": e.title, "when_to_use": e.when_to_use, "steps": e.steps}
            for e in state.cookbook
        ],
        "summary": {
            "incident_count": len(state.incidents),
            "critical_or_high": sum(1 for i in state.incidents if i.severity.value in ("HIGH", "CRITICAL")),
            "tickets_created": len(state.jira_tickets),
            "errors": state.errors,
        },
    }


# ---------- Tools -----------------------------------------------------------------

@mcp.tool()
def analyze_logs(raw_logs: str, strict_critic: bool = False) -> dict[str, Any]:
    """
    Run the full multi-agent incident-analysis pipeline on a block of raw logs.

    The pipeline parses the logs, classifies them into discrete incidents, scores
    severity, retrieves similar past incidents from a vector store, generates a
    remediation plan, and runs that plan through a self-critique safety check.

    Args:
        raw_logs: The log content as a string. Supports JSON, syslog, and free-form lines.
        strict_critic: If True, the critic uses a stricter rubric biased toward
            rejection — useful for high-stakes incidents where you want the loop
            to fire and the plan refined.

    Returns:
        A dict with `incidents` (each containing severity, root cause, remediation
        steps, RAG citations, and critic verdict), `runbook` (generalized
        runbook entries distilled from this run), and `summary` (counts).
    """
    if not raw_logs or not raw_logs.strip():
        return {"error": "raw_logs is empty", "incidents": [], "runbook": [], "summary": {}}

    logger.info("analyze_logs: %d bytes, strict=%s", len(raw_logs), strict_critic)
    graph = get_graph()
    initial = IncidentState(raw_logs=raw_logs, strict_critic=strict_critic)
    result = graph.invoke(initial)
    final = IncidentState.model_validate(result)
    return _shape_pipeline_result(final)


@mcp.tool()
def analyze_log_file(path: str, strict_critic: bool = False) -> dict[str, Any]:
    """
    Same as `analyze_logs` but reads from a file path on the server's filesystem.
    Useful when the caller is the on-call engineer pointing at /var/log/something.

    Args:
        path: Absolute or relative path to the log file.
        strict_critic: See `analyze_logs`.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"file not found: {path}"}
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    try:
        raw = p.read_text(errors="replace")
    except Exception as e:
        return {"error": f"could not read file: {e}"}
    return analyze_logs(raw, strict_critic=strict_critic)


@mcp.tool()
def search_past_incidents(query: str, k: int = 3) -> list[dict[str, Any]]:
    """
    Search the runbook archive for incidents similar to a free-text description.
    Useful for "have we seen this before?" queries before running the full pipeline.

    Args:
        query: Description of the symptom or failure pattern.
        k: How many matches to return (default 3, max 10).

    Returns:
        A list of past incidents with title, summary, the remediation that worked,
        category, and a similarity score in [0, 1].
    """
    if not query or not query.strip():
        return []
    k = max(1, min(10, k))
    hits = vectorstore.query_similar(query, k=k)
    return [
        {
            "id": h.get("id"),
            "title": h.get("title"),
            "summary": h.get("summary"),
            "remediation": h.get("remediation"),
            "similarity": round(float(h.get("similarity", 0.0)), 3),
        }
        for h in hits
    ]


# ---------- Entry point -----------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")

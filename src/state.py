"""
Shared state for the incident-analysis graph.

CONCEPT: Structured output / typed state with reducers.
Every agent reads the state and returns a partial dict of its updates.
Fields written by multiple nodes (trace, errors) use `operator.add` as their
reducer so concurrent writes from parallel branches accumulate cleanly instead
of conflicting. This is the idiomatic LangGraph pattern.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from operator import add
from typing import Annotated, Optional

from pydantic import BaseModel, Field


# ---------- Enums ----------

class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[self.value]


class IncidentCategory(str, Enum):
    CRASH_LOOP = "CRASH_LOOP"
    OOM = "OOM"
    NETWORK = "NETWORK"
    DATABASE = "DATABASE"
    AUTH = "AUTH"
    LATENCY = "LATENCY"
    DEPLOYMENT = "DEPLOYMENT"
    DISK = "DISK"
    UNKNOWN = "UNKNOWN"


# ---------- Atomic objects ----------

class LogEvent(BaseModel):
    timestamp: Optional[str] = None
    level: str = "INFO"
    source: Optional[str] = None
    message: str
    raw: str


class Incident(BaseModel):
    id: str
    title: str
    category: IncidentCategory = IncidentCategory.UNKNOWN
    severity: Severity = Severity.LOW
    summary: str
    affected_components: list[str] = Field(default_factory=list)
    sample_events: list[str] = Field(default_factory=list)
    event_count: int = 0


class RAGMatch(BaseModel):
    incident_id: str
    past_id: str = ""
    past_title: str
    past_summary: str
    past_remediation: str
    similarity: float


class Remediation(BaseModel):
    incident_id: str
    root_cause: str
    steps: list[str]
    rationale: str
    references: list[str] = Field(default_factory=list)
    revision: int = 1


class CritiqueResult(BaseModel):
    incident_id: str
    approved: bool
    issues: list[str] = Field(default_factory=list)
    suggestion: Optional[str] = None
    # Captured from extended-thinking blocks on the critic's response. Same reasoning
    # text appears on every CritiqueResult from a single critic invocation, since the
    # critic reasons about all remediations in one batched call.
    thinking: Optional[str] = None


class SlackMessage(BaseModel):
    incident_id: str
    channel: str
    text: str
    posted_at: Optional[str] = None
    dry_run: bool = True
    response: dict = Field(default_factory=dict)


class JiraTicket(BaseModel):
    incident_id: str
    key: str
    summary: str
    description: str
    priority: str
    dry_run: bool = True
    url: Optional[str] = None
    error: Optional[str] = None


class CookbookEntry(BaseModel):
    title: str
    when_to_use: str
    steps: list[str]


class TraceStep(BaseModel):
    agent: str
    started_at: str
    finished_at: Optional[str] = None
    status: str = "ok"  # ok | error
    note: Optional[str] = None


# ---------- Top-level state ----------

class IncidentState(BaseModel):
    """
    Single state object that travels through the graph.

    Fields with `Annotated[..., add]` accumulate via list concatenation when
    multiple nodes write them in the same step (e.g., parallel fan-out).
    Other fields use last-write-wins, which is safe because they have a
    single writer in the graph.
    """
    # Inputs (set once, never overwritten in steady state)
    raw_logs: str = ""
    filename: Optional[str] = None

    # Single-writer pipeline outputs (last-write-wins is fine)
    parsed_events: list[LogEvent] = Field(default_factory=list)
    incidents: list[Incident] = Field(default_factory=list)
    rag_matches: list[RAGMatch] = Field(default_factory=list)
    remediations: list[Remediation] = Field(default_factory=list)
    critique: list[CritiqueResult] = Field(default_factory=list)
    slack_messages: list[SlackMessage] = Field(default_factory=list)
    jira_tickets: list[JiraTicket] = Field(default_factory=list)
    cookbook: list[CookbookEntry] = Field(default_factory=list)

    # Control flow
    critic_retries: int = 0
    max_critic_retries: int = 1

    # Demo / quality knobs
    strict_critic: bool = False  # when True, critic uses a stricter rubric biased toward rejection

    # Accumulators — written by EVERY node (incl. parallel), reducer required
    trace: Annotated[list[TraceStep], add] = Field(default_factory=list)
    errors: Annotated[list[str], add] = Field(default_factory=list)


# ---------- Helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_step(agent: str, started_at: str, status: str = "ok", note: Optional[str] = None) -> TraceStep:
    """Build a TraceStep capturing duration from a recorded start timestamp."""
    return TraceStep(
        agent=agent,
        started_at=started_at,
        finished_at=now_iso(),
        status=status,
        note=note,
    )


def rag_surface_threshold() -> float:
    """Minimum similarity score for a past incident to be surfaced into outputs
    (JIRA description, Slack footer, dashboard reference block).

    The remediation agent always reads RAG matches as context. This threshold
    only controls whether the past incident gets cited *visibly* in outputs —
    so it can be looser than what the agent finds useful for grounding.
    """
    try:
        return float(os.getenv("RAG_SURFACE_THRESHOLD", "0.4"))
    except ValueError:
        return 0.4


def select_strong_matches(
    matches: list[RAGMatch], incident_id: str, max_results: int = 2
) -> list[RAGMatch]:
    """Return the top-N RAG matches for this incident above the surface threshold,
    sorted by descending similarity."""
    threshold = rag_surface_threshold()
    relevant = [m for m in matches if m.incident_id == incident_id and m.similarity >= threshold]
    return sorted(relevant, key=lambda m: -m.similarity)[:max_results]

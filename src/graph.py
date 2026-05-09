"""
LangGraph orchestrator for the incident analysis pipeline.

CONCEPTS demonstrated in this file:
- Multi-agent orchestration (StateGraph)
- Supervisor / orchestrator pattern (this file owns control flow)
- Conditional routing / dynamic edges (route_after_critic, should_create_ticket)
- Parallel execution / fan-out (Slack, JIRA, Cookbook run concurrently)
- Self-critique loop (critic can route back to remediation)
- Guardrails (severity gate before JIRA ticket creation)

Every node returns a *partial* dict of state updates. Fields with reducers
(trace, errors) accumulate via list concatenation; other fields use
last-write-wins, which is safe because the graph guarantees a single writer
per non-reducer field.
"""
from __future__ import annotations

import os
from typing import Literal

from langgraph.graph import END, StateGraph

from src.agents import (
    classifier,
    severity,
    rag_retriever,
    remediation,
    critic,
    slack_notifier,
    jira_creator,
    cookbook,
)
from src.parsers.log_parser import parse_logs_node
from src.state import IncidentState, Severity, make_step, now_iso


# ---------- Conditional routing functions ----------

def route_after_critic(state: IncidentState) -> Literal["remediation", "fanout"]:
    """
    CONCEPT: Self-critique loop.
    If the critic rejected any remediation AND we haven't exceeded retry budget,
    loop back to remediation. Otherwise, proceed to the parallel fan-out.

    Note: routing functions don't mutate state — they only read it. The retry
    counter is incremented inside the remediation agent when it sees a
    rejected critique.
    """
    any_rejected = any(not c.approved for c in state.critique)
    if any_rejected and state.critic_retries < state.max_critic_retries:
        return "remediation"
    return "fanout"


def should_create_ticket(state: IncidentState) -> Literal["jira_creator", "skip_jira"]:
    """
    CONCEPT: Guardrail. Only HIGH or CRITICAL incidents become JIRA tickets,
    so low-priority noise doesn't pollute the project board.
    """
    has_high_severity = any(
        inc.severity.rank >= Severity.HIGH.rank for inc in state.incidents
    )
    return "jira_creator" if has_high_severity else "skip_jira"


# ---------- Passthrough / utility nodes ----------

def fanout_node(state: IncidentState) -> dict:
    """No-op entrypoint for the parallel section. Returns no state changes
    but emits a trace entry so the UI can show the fan-out boundary."""
    started = now_iso()
    return {
        "trace": [make_step("fanout", started, note="Fanning out to Slack / JIRA / Cookbook")]
    }


def skip_jira_node(state: IncidentState) -> dict:
    """Path taken when no incident hits the HIGH/CRITICAL threshold."""
    started = now_iso()
    return {
        "trace": [make_step("jira_creator", started, note="Skipped — no HIGH/CRITICAL incidents")]
    }


def aggregate_node(state: IncidentState) -> dict:
    """Final aggregation point. Emits a summary trace step."""
    started = now_iso()
    note = (
        f"Done — {len(state.incidents)} incident(s), "
        f"{len(state.slack_messages)} slack, "
        f"{len(state.jira_tickets)} jira, "
        f"{len(state.cookbook)} cookbook entry(ies)"
    )
    return {"trace": [make_step("aggregate", started, note=note)]}


# ---------- Graph construction ----------

def build_graph():
    """
    Linear: parse → classify → severity → rag → remediation → critic
    Conditional loopback OR fan-out to: slack || jira(?) || cookbook
    Then aggregate → END.
    """
    g = StateGraph(IncidentState)

    # Nodes
    g.add_node("parse", parse_logs_node)
    g.add_node("classify", classifier.run)
    g.add_node("score_severity", severity.run)
    g.add_node("rag", rag_retriever.run)
    g.add_node("remediation", remediation.run)
    g.add_node("critic", critic.run)
    g.add_node("fanout", fanout_node)
    g.add_node("slack_notifier", slack_notifier.run)
    g.add_node("jira_creator", jira_creator.run)
    g.add_node("skip_jira", skip_jira_node)
    g.add_node("cookbook", cookbook.run)
    g.add_node("aggregate", aggregate_node)

    # Linear edges
    g.set_entry_point("parse")
    g.add_edge("parse", "classify")
    g.add_edge("classify", "score_severity")
    g.add_edge("score_severity", "rag")
    g.add_edge("rag", "remediation")
    g.add_edge("remediation", "critic")

    # Conditional: critic may loop back to remediation
    g.add_conditional_edges(
        "critic",
        route_after_critic,
        {"remediation": "remediation", "fanout": "fanout"},
    )

    # Fan-out: from `fanout` to three branches in parallel
    g.add_edge("fanout", "slack_notifier")
    g.add_conditional_edges(
        "fanout",
        should_create_ticket,
        {"jira_creator": "jira_creator", "skip_jira": "skip_jira"},
    )
    g.add_edge("fanout", "cookbook")

    # Fan-in: all three branches converge on aggregate
    g.add_edge("slack_notifier", "aggregate")
    g.add_edge("jira_creator", "aggregate")
    g.add_edge("skip_jira", "aggregate")
    g.add_edge("cookbook", "aggregate")

    g.add_edge("aggregate", END)

    return g.compile()


# Module-level singleton — Streamlit imports this
_GRAPH = None


def get_graph():
    """Lazy-init so importing this module doesn't require env vars."""
    global _GRAPH
    if _GRAPH is None:
        # CONCEPT: Observability / tracing
        if os.getenv("LANGSMITH_API_KEY"):
            os.environ.setdefault("LANGSMITH_TRACING", "true")
        _GRAPH = build_graph()
    return _GRAPH

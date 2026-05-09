"""
JIRA ticket creator agent.

CONCEPT: Tool use + guardrail.
Only invoked for HIGH / CRITICAL incidents (the graph's `should_create_ticket`
conditional gate enforces this). Builds a ticket per qualifying incident and
dispatches via the JIRA tool (which falls back to mock mode if creds missing).
"""
from __future__ import annotations

from src.state import IncidentState, JiraTicket, Severity, make_step, now_iso, select_strong_matches
from src.tools import jira


def _priority_for(severity: Severity) -> str:
    return {
        Severity.CRITICAL: "Highest",
        Severity.HIGH: "High",
        Severity.MEDIUM: "Medium",
        Severity.LOW: "Low",
    }[severity]


def _description(inc, remediation, strong_matches) -> str:
    parts = [
        f"# {inc.title}",
        "",
        f"**Severity:** {inc.severity.value}",
        f"**Category:** {inc.category.value}",
        f"**Affected:** {', '.join(inc.affected_components) or 'n/a'}",
        f"**Event count:** {inc.event_count}",
        "",
        "## Summary",
        inc.summary,
    ]
    if remediation:
        parts += [
            "",
            "## Root cause",
            remediation.root_cause,
            "",
            "## Recommended steps",
            *[f"- {s}" for s in remediation.steps],
            "",
            "## Rationale",
            remediation.rationale,
        ]
    if strong_matches:
        # Surface the past remediation that worked so the on-call has prior
        # art directly in the ticket without having to dig.
        parts += ["", "## Reference: similar past incident(s)"]
        for m in strong_matches:
            parts += [
                "",
                f"**{m.past_id or '?'}** (similarity {m.similarity:.2f})",
                f"*Title:* {m.past_title}",
                f"*Past remediation that worked:* {m.past_remediation}",
            ]
    if inc.sample_events:
        parts += [
            "",
            "## Sample log lines",
            "```",
            *inc.sample_events[:5],
            "```",
        ]
    return "\n".join(parts)


def run(state: IncidentState) -> dict:
    started = now_iso()
    rem_by_inc = {r.incident_id: r for r in state.remediations}
    tickets: list[JiraTicket] = []
    errors: list[str] = []

    for inc in state.incidents:
        if inc.severity.rank < Severity.HIGH.rank:
            continue
        rem = rem_by_inc.get(inc.id)
        strong_matches = select_strong_matches(state.rag_matches, inc.id, max_results=2)
        summary = f"[{inc.severity.value}] {inc.title}"
        description = _description(inc, rem, strong_matches)
        priority = _priority_for(inc.severity)
        resp = jira.create_ticket(summary=summary, description=description, priority=priority)
        ticket_error = resp.get("error") if not resp.get("ok") else None
        if ticket_error:
            errors.append(f"jira_creator[{inc.id}]: {ticket_error}")
        tickets.append(
            JiraTicket(
                incident_id=inc.id,
                key=resp.get("key", "UNKNOWN"),
                summary=summary,
                description=description,
                priority=priority,
                dry_run=resp.get("dry_run", True),
                url=resp.get("url"),
                error=ticket_error,
            )
        )

    created_count = sum(1 for t in tickets if t.key not in ("UNKNOWN", "ERROR"))
    failed_count  = sum(1 for t in tickets if t.error)
    note = f"Created {created_count} ticket(s)"
    if failed_count:
        note += f"; {failed_count} failed"
    out: dict = {
        "jira_tickets": tickets,
        "trace": [make_step("jira_creator", started, status="error" if failed_count else "ok", note=note)],
    }
    if errors:
        out["errors"] = errors
    return out

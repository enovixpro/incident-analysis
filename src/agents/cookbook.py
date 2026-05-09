"""
Cookbook synthesizer agent.

CONCEPT: Specialist agent that distills the (incident, remediation) pairs from
this run into reusable runbook entries for the on-call documentation. The
remediation agent thinks tactically about *this* incident; the cookbook agent
generalizes to *the pattern* so the next on-call recognizes it.

The model may produce fewer entries than there are incidents — collapsing
related incidents into one runbook entry is encouraged. It may also produce
zero if nothing in the run is worth generalizing.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import llm, usage
from src.state import (
    CookbookEntry,
    Incident,
    IncidentState,
    Remediation,
    make_step,
    now_iso,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "cookbook.md"

_TOOL = {
    "name": "synthesize_cookbook",
    "description": "Produce reusable runbook entries distilled from this run's incidents.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "description": "Generalized runbook entries. May be empty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The failure pattern, not the incident. e.g. 'Connection pool exhaustion under load'.",
                        },
                        "when_to_use": {
                            "type": "string",
                            "description": "Symptoms an on-call would observe — log signatures, alert names, metric shapes.",
                        },
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3–7 generalized actions. Strip incident-specific identifiers; use 'e.g.' for illustrative values.",
                        },
                    },
                    "required": ["title", "when_to_use", "steps"],
                },
            }
        },
        "required": ["entries"],
    },
}


def _format_pair(inc: Incident, rem: Remediation) -> str:
    affected = ", ".join(inc.affected_components) or "n/a"
    steps = "\n".join(f"    {i+1}. {s}" for i, s in enumerate(rem.steps)) or "    (no steps)"
    return (
        f"Incident {inc.id}\n"
        f"  Title: {inc.title}\n"
        f"  Category: {inc.category.value}    Severity: {inc.severity.value}\n"
        f"  Affected: {affected}\n"
        f"  Summary: {inc.summary}\n"
        f"  Root cause: {rem.root_cause}\n"
        f"  Remediation steps:\n{steps}"
    )


def _fallback_entry(inc: Incident, rem: Remediation | None) -> CookbookEntry:
    return CookbookEntry(
        title=f"Runbook: {inc.category.value} on {', '.join(inc.affected_components) or 'affected component'}",
        when_to_use=f"Symptoms similar to: {inc.summary}",
        steps=(rem.steps if rem else ["(cookbook agent unavailable — see linked incident for steps)"]),
    )


def run(state: IncidentState) -> dict:
    started = now_iso()

    rem_by_inc = {r.incident_id: r for r in state.remediations}
    pairs = [(inc, rem_by_inc.get(inc.id)) for inc in state.incidents if rem_by_inc.get(inc.id)]

    if not pairs:
        return {
            "cookbook": [],
            "trace": [make_step("cookbook", started, note="No incident-remediation pairs to summarize")],
        }

    try:
        client = llm.get_client()
        model = llm.get_model()
        system = _PROMPT_PATH.read_text()

        body = "\n\n".join(_format_pair(inc, rem) for inc, rem in pairs)
        user_msg = (
            f"Incidents and remediations from this run ({len(pairs)}):\n\n{body}\n\n"
            "Distill these into runbook entries and call the synthesize_cookbook tool. "
            "Collapse related incidents into a single entry when they share a pattern. "
            "Return [] if nothing is worth generalizing."
        )

        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "synthesize_cookbook"},
            messages=[{"role": "user", "content": user_msg}],
        )
        usage.record("cookbook", model, resp.usage)
        tool_block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError("model did not invoke synthesize_cookbook tool")

        entries: list[CookbookEntry] = []
        for item in tool_block.input.get("entries") or []:
            try:
                entries.append(CookbookEntry(
                    title=item["title"],
                    when_to_use=item["when_to_use"],
                    steps=list(item.get("steps") or []),
                ))
            except (KeyError, TypeError):
                continue

        return {
            "cookbook": entries,
            "trace": [make_step("cookbook", started, note=f"Synthesized {len(entries)} runbook entry(ies) from {len(pairs)} incident(s)")],
        }

    except Exception as e:
        logger.exception("cookbook synthesis failed")
        entries = [_fallback_entry(inc, rem) for inc, rem in pairs]
        return {
            "cookbook": entries,
            "errors": [f"cookbook: {e}"],
            "trace": [make_step("cookbook", started, status="error", note=f"Fallback {len(entries)} entry(ies) due to error")],
        }

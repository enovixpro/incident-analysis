"""
Severity scoring agent.

CONCEPT: Specialist agent + structured output. Reads each incident produced by
the classifier and assigns a final severity (LOW / MEDIUM / HIGH / CRITICAL).
Severity drives the downstream JIRA gate — only HIGH/CRITICAL incidents become
tickets — so this agent's output is load-bearing for routing.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import llm, usage
from src.state import (
    Incident,
    IncidentState,
    Severity,
    make_step,
    now_iso,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "severity.md"

_TOOL = {
    "name": "score_severity",
    "description": "Assign a severity (LOW/MEDIUM/HIGH/CRITICAL) to each incident.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "description": "One entry per incident. incident_id must match an input id.",
                "items": {
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Must match an incident id from the input.",
                        },
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in Severity],
                        },
                        "rationale": {
                            "type": "string",
                            "description": "One sentence — name the specific signal you used.",
                        },
                    },
                    "required": ["incident_id", "severity", "rationale"],
                },
            }
        },
        "required": ["scores"],
    },
}


def _format_incidents(incidents: list[Incident]) -> str:
    blocks = []
    for inc in incidents:
        affected = ", ".join(inc.affected_components) or "n/a"
        block = (
            f"Incident {inc.id}\n"
            f"  Title: {inc.title}\n"
            f"  Category: {inc.category.value}\n"
            f"  Affected: {affected}\n"
            f"  Event count: {inc.event_count}\n"
            f"  Summary: {inc.summary}"
        )
        if inc.sample_events:
            block += "\n  Sample events:\n" + "\n".join(
                f"    - {ev}" for ev in inc.sample_events[:5]
            )
        blocks.append(block)
    return "\n\n".join(blocks)


def run(state: IncidentState) -> dict:
    started = now_iso()

    if not state.incidents:
        return {
            "incidents": [],
            "trace": [make_step("severity", started, note="No incidents to score")],
        }

    try:
        client = llm.get_client()
        model = llm.get_model()
        system = _PROMPT_PATH.read_text()

        user_msg = (
            f"Incidents to score ({len(state.incidents)}):\n\n"
            f"{_format_incidents(state.incidents)}\n\n"
            "Score each one and call the score_severity tool."
        )

        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "score_severity"},
            messages=[{"role": "user", "content": user_msg}],
        )
        usage.record("severity", model, resp.usage)

        tool_block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError("model did not invoke score_severity tool")

        scores: dict[str, tuple[Severity, str]] = {}
        for entry in tool_block.input.get("scores") or []:
            try:
                scores[entry["incident_id"]] = (
                    Severity(entry["severity"]),
                    entry.get("rationale", ""),
                )
            except (KeyError, ValueError):
                continue

        updated: list[Incident] = []
        rationales: list[str] = []
        for inc in state.incidents:
            new_sev, rationale = scores.get(inc.id, (inc.severity, "no score returned — kept prior"))
            updated.append(inc.model_copy(update={"severity": new_sev}))
            rationales.append(f"{inc.id}={new_sev.value}")

        note = f"Scored {len(updated)} incident(s): " + ", ".join(rationales)
        return {
            "incidents": updated,
            "trace": [make_step("severity", started, note=note)],
        }

    except Exception as e:
        logger.exception("severity scoring failed")
        return {
            "incidents": state.incidents,
            "errors": [f"severity: {e}"],
            "trace": [make_step("severity", started, status="error", note=str(e))],
        }

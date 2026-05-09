"""
Classifier agent.

CONCEPT: Specialist agent + structured output via Anthropic tool use.
Takes parsed log events and groups them into discrete incidents. The tool
schema forces the model to produce field-validated output that maps directly
onto the `Incident` Pydantic model — no JSON parsing of free-form text.

Severity is intentionally not set here; the severity agent assigns it next.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import llm, usage
from src.state import (
    Incident,
    IncidentCategory,
    IncidentState,
    LogEvent,
    make_step,
    now_iso,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classifier.md"
_MAX_EVENTS = 150  # cap context for very long logs

_TOOL = {
    "name": "report_incidents",
    "description": (
        "Report the discrete incidents you identified by grouping the supplied "
        "log events. Each incident represents one root cause / failure mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "incidents": {
                "type": "array",
                "description": "Distinct incidents found in the logs. Empty if nothing notable.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short, specific title (≤ 80 chars).",
                        },
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in IncidentCategory],
                            "description": "Category that best fits the failure mode.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "1–3 sentences: what is happening and the likely blast radius.",
                        },
                        "affected_components": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Service / pod / host names involved.",
                        },
                        "event_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Indices from the numbered event list that belong to this incident.",
                        },
                    },
                    "required": ["title", "category", "summary", "event_indices"],
                },
            }
        },
        "required": ["incidents"],
    },
}


def _format_events(events: list[LogEvent]) -> str:
    lines = []
    for i, ev in enumerate(events):
        src = f" {ev.source}" if ev.source else ""
        ts = f" {ev.timestamp}" if ev.timestamp else ""
        lines.append(f"[{i}]{ts} [{ev.level}]{src}: {ev.message}")
    return "\n".join(lines)


def _build_incident(idx: int, item: dict, events: list[LogEvent]) -> Incident:
    indices = [i for i in (item.get("event_indices") or []) if 0 <= i < len(events)]
    sample = [events[i].message for i in indices[:5]]
    try:
        category = IncidentCategory(item.get("category", "UNKNOWN"))
    except ValueError:
        category = IncidentCategory.UNKNOWN
    return Incident(
        id=f"INC-{idx:03d}",
        title=item.get("title") or "Untitled incident",
        category=category,
        # severity left at its default; the severity agent assigns it next.
        summary=item.get("summary") or "",
        affected_components=list(item.get("affected_components") or []),
        sample_events=sample,
        event_count=len(indices),
    )


def run(state: IncidentState) -> dict:
    started = now_iso()

    if not state.parsed_events:
        return {
            "incidents": [],
            "trace": [make_step("classifier", started, note="No events to classify")],
        }

    events = state.parsed_events[:_MAX_EVENTS]
    truncated = len(state.parsed_events) - len(events)

    try:
        client = llm.get_client()
        model = llm.get_model()
        system = _PROMPT_PATH.read_text()

        user_msg = (
            f"Numbered log events (0..{len(events) - 1}):\n\n"
            f"{_format_events(events)}\n\n"
            "Group these into discrete incidents and call the report_incidents tool."
        )
        if truncated > 0:
            user_msg += (
                f"\n\n(Note: {truncated} additional event(s) were truncated to "
                "keep context bounded.)"
            )

        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "report_incidents"},
            messages=[{"role": "user", "content": user_msg}],
        )
        usage.record("classifier", model, resp.usage)

        tool_block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError("model did not invoke report_incidents tool")

        items = tool_block.input.get("incidents") or []
        incidents = [_build_incident(i + 1, item, events) for i, item in enumerate(items)]

        note = f"Produced {len(incidents)} incident(s)"
        if truncated > 0:
            note += f"; {truncated} event(s) truncated"
        return {
            "incidents": incidents,
            "trace": [make_step("classifier", started, note=note)],
        }

    except Exception as e:
        logger.exception("classifier failed")
        # Fall back to a single placeholder so downstream agents still execute.
        # This also keeps the smoke test green when ANTHROPIC_API_KEY is unset.
        fallback = Incident(
            id="INC-001",
            title="Unclassified incident (classifier unavailable)",
            category=IncidentCategory.UNKNOWN,
            summary=(
                f"Classifier could not run ({e}). Showing the parsed event volume "
                "so downstream agents have something to operate on."
            ),
            sample_events=[ev.message for ev in events[:5]],
            event_count=len(events),
        )
        return {
            "incidents": [fallback],
            "errors": [f"classifier: {e}"],
            "trace": [make_step("classifier", started, status="error", note=str(e))],
        }

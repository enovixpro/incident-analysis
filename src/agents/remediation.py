"""
Remediation agent.

CONCEPT: ReAct-style reasoning + RAG-grounded specialist agent.
For each incident, the agent reasons over the incident summary AND the RAG
matches to propose root cause + ordered fix steps + rationale. Output is
forced into a Pydantic-shaped tool schema so downstream agents (Slack, JIRA,
Cookbook) can treat it as structured data.

Self-critique loop integration:
- If `state.critique` contains a rejection for an incident, this is a retry
  pass for that incident. We pass the prior remediation and the critic's
  feedback into the prompt and bump `critic_retries`.
- Approved incidents are kept as-is to avoid re-spending tokens.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import anthropic

from src import llm, usage
from src.state import (
    CritiqueResult,
    Incident,
    IncidentState,
    RAGMatch,
    Remediation,
    make_step,
    now_iso,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "remediation.md"

_TOOL = {
    "name": "propose_remediation",
    "description": "Propose a root cause and ordered remediation plan for one incident.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause": {
                "type": "string",
                "description": "1–2 sentences naming what is actually broken.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3–7 ordered, concrete actions. Flag destructive ones explicitly.",
            },
            "rationale": {
                "type": "string",
                "description": "1–3 sentences explaining the plan and any trade-offs.",
            },
            "references": {
                "type": "array",
                "items": {"type": "string"},
                "description": "RAG match ids actually used (e.g. ['PAST-003']) or [].",
            },
        },
        "required": ["root_cause", "steps", "rationale", "references"],
    },
}


def _format_incident(inc: Incident) -> str:
    affected = ", ".join(inc.affected_components) or "n/a"
    out = (
        f"Incident {inc.id}\n"
        f"  Title: {inc.title}\n"
        f"  Category: {inc.category.value}\n"
        f"  Severity: {inc.severity.value}\n"
        f"  Affected: {affected}\n"
        f"  Event count: {inc.event_count}\n"
        f"  Summary: {inc.summary}"
    )
    if inc.sample_events:
        out += "\n  Sample events:\n" + "\n".join(
            f"    - {ev}" for ev in inc.sample_events[:5]
        )
    return out


def _format_rag(matches: list[RAGMatch]) -> str:
    if not matches:
        return "(no similar past incidents found in the runbook archive)"
    blocks = []
    for m in matches:
        header = f"Past incident {m.past_id}" if m.past_id else "Past incident"
        blocks.append(
            f"{header} (similarity={m.similarity:.2f})\n"
            f"  Title: {m.past_title}\n"
            f"  Summary: {m.past_summary}\n"
            f"  Remediation that worked: {m.past_remediation}"
        )
    return "\n\n".join(blocks)


def _format_critique(prior: Remediation, critique: CritiqueResult) -> str:
    issues = "\n".join(f"  - {i}" for i in critique.issues) or "  (none listed)"
    return (
        "RETRY: the previous remediation was rejected by the critic.\n\n"
        f"Prior root cause: {prior.root_cause}\n"
        f"Prior steps:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(prior.steps)) + "\n\n"
        f"Critic issues:\n{issues}\n"
        f"Critic suggestion: {critique.suggestion or '(none)'}\n\n"
        "Address the issues directly. Do not just restate the prior plan."
    )


def _generate(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    inc: Incident,
    rag: list[RAGMatch],
    prior: Optional[Remediation],
    critique: Optional[CritiqueResult],
) -> dict:
    parts = [
        _format_incident(inc),
        "",
        "RAG context — similar past incidents:",
        _format_rag(rag),
    ]
    if critique is not None and prior is not None and not critique.approved:
        parts += ["", _format_critique(prior, critique)]
    parts += ["", "Call the propose_remediation tool with your plan."]
    user_msg = "\n".join(parts)

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "propose_remediation"},
        messages=[{"role": "user", "content": user_msg}],
    )
    usage.record("remediation", model, resp.usage)
    tool_block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        raise RuntimeError("model did not invoke propose_remediation tool")
    return tool_block.input


def run(state: IncidentState) -> dict:
    started = now_iso()

    if not state.incidents:
        return {
            "remediations": [],
            "trace": [make_step("remediation", started, note="No incidents to remediate")],
        }

    existing = {r.incident_id: r for r in state.remediations}
    critique_by_id = {c.incident_id: c for c in state.critique}
    rejected_ids = {c.incident_id for c in state.critique if not c.approved}
    is_retry = bool(rejected_ids)

    rag_by_inc: dict[str, list[RAGMatch]] = {}
    for m in state.rag_matches:
        rag_by_inc.setdefault(m.incident_id, []).append(m)

    new_remediations: list[Remediation] = []
    errors: list[str] = []

    try:
        client = llm.get_client()
    except Exception as e:
        client = None
        errors.append(f"remediation: client init failed: {e}")

    model = llm.get_model()
    system = _PROMPT_PATH.read_text()

    for inc in state.incidents:
        prior = existing.get(inc.id)
        critique = critique_by_id.get(inc.id)

        # On retry, keep already-approved remediations as-is.
        if is_retry and prior is not None and inc.id not in rejected_ids:
            new_remediations.append(prior)
            continue

        rev = (prior.revision + 1) if prior else 1

        if client is None:
            new_remediations.append(_fallback(inc, prior, rev, rag_by_inc.get(inc.id, [])))
            continue

        try:
            payload = _generate(
                client, model, system, inc, rag_by_inc.get(inc.id, []), prior, critique
            )
            new_remediations.append(
                Remediation(
                    incident_id=inc.id,
                    root_cause=payload.get("root_cause") or "(no root cause provided)",
                    steps=list(payload.get("steps") or []),
                    rationale=payload.get("rationale") or "",
                    references=list(payload.get("references") or []),
                    revision=rev,
                )
            )
        except Exception as e:
            logger.exception("remediation failed for %s", inc.id)
            errors.append(f"remediation[{inc.id}]: {e}")
            new_remediations.append(_fallback(inc, prior, rev, rag_by_inc.get(inc.id, [])))

    note = f"Produced {len(new_remediations)} remediation(s)"
    if is_retry:
        note += f" (retry pass — {len(rejected_ids)} regenerated)"

    out: dict = {
        "remediations": new_remediations,
        "trace": [make_step(
            "remediation",
            started,
            status="error" if errors else "ok",
            note=note,
        )],
    }
    if is_retry:
        out["critic_retries"] = state.critic_retries + 1
    if errors:
        out["errors"] = errors
    return out


def _fallback(
    inc: Incident,
    prior: Optional[Remediation],
    rev: int,
    rag: list[RAGMatch],
) -> Remediation:
    """Used when the API call fails. Keeps the graph runnable end-to-end."""
    return Remediation(
        incident_id=inc.id,
        root_cause="(remediation agent unavailable — manual triage required)",
        steps=[
            f"Open the source logs for {inc.id} and review the sample events.",
            "Page the on-call owner for the affected component.",
            "If a similar past incident matches, follow its known remediation.",
        ],
        rationale="Fallback plan emitted because the remediation agent could not run.",
        references=[m.past_title for m in rag[:3]],
        revision=rev,
    )

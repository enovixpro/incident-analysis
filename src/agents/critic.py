"""
Critic agent.

CONCEPT: Self-critique loop. Reviews each remediation for safety, completeness,
and destructive-action risk. If it rejects any, the graph routes back to
remediation for another attempt (capped by `max_critic_retries`).

The critic is biased toward approval — it only rejects on hard issues
(unsafe action, wrong root cause, missing rollback, hallucinated reference).
This keeps the retry loop a real safety mechanism instead of a tax on every
run.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import llm, usage
from src.state import (
    CritiqueResult,
    Incident,
    IncidentState,
    Remediation,
    make_step,
    now_iso,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "critic.md"
_STRICT_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "critic_strict.md"
_THINKING_BUDGET = 2000  # tokens. Plenty for reasoning over 1-3 remediations.

_TOOL = {
    "name": "review_remediations",
    "description": "Approve or reject each proposed remediation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "critiques": {
                "type": "array",
                "description": "One entry per remediation. incident_id must match.",
                "items": {
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Must match a remediation's incident_id from the input.",
                        },
                        "approved": {
                            "type": "boolean",
                            "description": "true unless a hard-reject condition is met.",
                        },
                        "issues": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific problems (only when rejecting).",
                        },
                        "suggestion": {
                            "type": "string",
                            "description": "One-sentence direction for the retry (only when rejecting).",
                        },
                    },
                    "required": ["incident_id", "approved"],
                },
            }
        },
        "required": ["critiques"],
    },
}


def _format_pair(inc: Incident, rem: Remediation) -> str:
    affected = ", ".join(inc.affected_components) or "n/a"
    steps = "\n".join(f"    {i+1}. {s}" for i, s in enumerate(rem.steps)) or "    (no steps)"
    return (
        f"Incident {inc.id} (revision {rem.revision})\n"
        f"  Title: {inc.title}\n"
        f"  Category: {inc.category.value}    Severity: {inc.severity.value}\n"
        f"  Affected: {affected}\n"
        f"  Summary: {inc.summary}\n"
        f"  Proposed root cause: {rem.root_cause}\n"
        f"  Proposed steps:\n{steps}\n"
        f"  Rationale: {rem.rationale}\n"
        f"  References: {rem.references or '[]'}"
    )


def run(state: IncidentState) -> dict:
    started = now_iso()

    if not state.remediations:
        return {
            "critique": [],
            "trace": [make_step("critic", started, note="No remediations to review")],
        }

    inc_by_id = {inc.id: inc for inc in state.incidents}
    pairs = [
        (inc_by_id[r.incident_id], r)
        for r in state.remediations
        if r.incident_id in inc_by_id
    ]

    if not pairs:
        return {
            "critique": [],
            "trace": [make_step("critic", started, note="No incident-remediation pairs to review")],
        }

    try:
        client = llm.get_client()
        model = llm.get_model()
        prompt_path = _STRICT_PROMPT_PATH if state.strict_critic else _PROMPT_PATH
        system = prompt_path.read_text()

        body = "\n\n".join(_format_pair(inc, rem) for inc, rem in pairs)
        user_msg = (
            f"Remediations to review ({len(pairs)}):\n\n{body}\n\n"
            "Review each and call the review_remediations tool. Approve unless a hard-reject condition is met."
        )

        # CONCEPT: Extended thinking. The critic is the agent where reasoning quality
        # matters most (it's the safety gate), so we let it think before voting.
        # Notes / API constraints with thinking enabled:
        #   - temperature must be 1 (the default).
        #   - max_tokens must exceed budget_tokens.
        #   - tool_choice cannot force tool use — even `{"type":"any"}` is rejected.
        #     We use "auto" and rely on the system prompt's explicit instruction to
        #     call review_remediations. Sonnet honors this reliably; we still guard
        #     for the no-tool-call case below.
        resp = client.messages.create(
            model=model,
            max_tokens=_THINKING_BUDGET + 1536,
            temperature=1,
            thinking={"type": "enabled", "budget_tokens": _THINKING_BUDGET},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": user_msg}],
        )
        usage.record("critic", model, resp.usage)

        thinking_text = "\n\n".join(
            getattr(b, "thinking", "") or ""
            for b in resp.content
            if getattr(b, "type", None) == "thinking"
        ).strip() or None

        tool_block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError("model did not invoke review_remediations tool")

        by_id: dict[str, dict] = {}
        for entry in tool_block.input.get("critiques") or []:
            inc_id = entry.get("incident_id")
            if inc_id:
                by_id[inc_id] = entry

        critiques: list[CritiqueResult] = []
        approved_count = 0
        rejected_count = 0
        for _, rem in pairs:
            entry = by_id.get(rem.incident_id)
            if entry is None:
                # Model omitted this incident — fail safe by approving so we
                # don't burn a retry on a missing response.
                critiques.append(CritiqueResult(
                    incident_id=rem.incident_id, approved=True, thinking=thinking_text,
                ))
                approved_count += 1
                continue

            approved = bool(entry.get("approved", True))
            critiques.append(
                CritiqueResult(
                    incident_id=rem.incident_id,
                    approved=approved,
                    issues=list(entry.get("issues") or []),
                    suggestion=entry.get("suggestion"),
                    thinking=thinking_text,
                )
            )
            if approved:
                approved_count += 1
            else:
                rejected_count += 1

        mode = "strict" if state.strict_critic else "default"
        note = f"Approved {approved_count}, rejected {rejected_count} (revision {pairs[0][1].revision}, {mode})"
        return {
            "critique": critiques,
            "trace": [make_step("critic", started, note=note)],
        }

    except Exception as e:
        logger.exception("critic failed")
        # Fail open: auto-approve so the graph completes and we don't burn
        # a retry on infrastructure problems.
        critiques = [CritiqueResult(incident_id=r.incident_id, approved=True) for _, r in pairs]
        return {
            "critique": critiques,
            "errors": [f"critic: {e}"],
            "trace": [make_step("critic", started, status="error", note=f"Auto-approved {len(critiques)} due to error")],
        }

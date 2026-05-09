"""
Slack notifier agent.

CONCEPT: Tool use. Formats each incident + remediation into a Slack-friendly
message and dispatches via the Slack tool (which falls back to mock mode if
SLACK_BOT_TOKEN is missing).
"""
from __future__ import annotations

import os

from src.state import IncidentState, SlackMessage, make_step, now_iso, select_strong_matches
from src.tools import slack


def _format(incident, remediation, strong_matches) -> str:
    lines = [
        f"*🚨 {incident.severity.value} — {incident.title}*",
        f"_Category:_ {incident.category.value}    _Events:_ {incident.event_count}",
        "",
        f"*Summary:* {incident.summary}",
    ]
    if remediation:
        lines += [
            "",
            f"*Root cause:* {remediation.root_cause}",
            "*Recommended steps:*",
            *[f"  {i+1}. {s}" for i, s in enumerate(remediation.steps)],
        ]
    if strong_matches:
        # Single best match in Slack — keep the message scannable.
        m = strong_matches[0]
        lines += [
            "",
            f"_Reference:_ similar to *{m.past_id}* (sim {m.similarity:.2f}) — see ticket for prior fix.",
        ]
    return "\n".join(lines)


def run(state: IncidentState) -> dict:
    started = now_iso()
    channel = os.getenv("SLACK_CHANNEL", "#incidents")
    rem_by_inc = {r.incident_id: r for r in state.remediations}
    messages: list[SlackMessage] = []

    for inc in state.incidents:
        strong_matches = select_strong_matches(state.rag_matches, inc.id, max_results=1)
        text = _format(inc, rem_by_inc.get(inc.id), strong_matches)
        resp = slack.post_message(channel=channel, text=text)
        messages.append(
            SlackMessage(
                incident_id=inc.id,
                channel=channel,
                text=text,
                posted_at=resp.get("ts"),
                dry_run=resp.get("dry_run", True),
                response=resp,
            )
        )

    return {
        "slack_messages": messages,
        "trace": [make_step("slack_notifier", started, note=f"Posted {len(messages)} message(s)")],
    }

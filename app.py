"""
Streamlit UI for the Multi-Agent DevOps Incident Analysis Suite.

CONCEPT: Streaming UI. The sidebar shows the agent trace as it builds.
Each tab surfaces a different artifact produced by the graph:
incidents | Slack messages | JIRA tickets | Cookbook | Trace.
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from src.graph import get_graph  # noqa: E402
from src.state import IncidentState  # noqa: E402

st.set_page_config(
    page_title="Incident Analysis Suite",
    page_icon="🚨",
    layout="wide",
)

st.title("🚨 Multi-Agent DevOps Incident Analysis Suite")
st.caption(
    "Upload an ops log → multi-agent pipeline classifies, scores, retrieves "
    "similar past incidents, recommends remediation, and dispatches to Slack + JIRA."
)

# ---------- Sidebar: env status + sample picker ----------
with st.sidebar:
    st.subheader("Environment")
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_slack = bool(os.getenv("SLACK_BOT_TOKEN"))
    has_jira = all(os.getenv(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))
    st.write(f"{'✅' if has_anthropic else '❌'} ANTHROPIC_API_KEY")
    st.write(f"{'✅ live' if has_slack else '🟡 mock'} Slack")
    st.write(f"{'✅ live' if has_jira else '🟡 mock'} JIRA")
    st.divider()

    st.subheader("Sample logs")
    sample_dir = Path("data/sample_logs")
    samples = sorted(sample_dir.glob("*.log")) if sample_dir.exists() else []
    sample_choice = st.selectbox(
        "Pick a sample (or upload below)",
        options=["—"] + [s.name for s in samples],
        index=0,
    )

# ---------- Main: upload + run ----------
uploaded = st.file_uploader("Upload a log file (.log, .txt, .json)", type=["log", "txt", "json"])

raw_logs: str | None = None
filename: str | None = None
if uploaded is not None:
    raw_logs = uploaded.read().decode("utf-8", errors="replace")
    filename = uploaded.name
elif sample_choice != "—":
    raw_logs = (sample_dir / sample_choice).read_text()
    filename = sample_choice

if raw_logs:
    st.text_area("Log preview", raw_logs[:2000], height=180, disabled=True)

run = st.button("🚀 Run pipeline", type="primary", disabled=not raw_logs)

if run and raw_logs:
    with st.status("Running multi-agent graph...", expanded=True) as status:
        graph = get_graph()
        initial = IncidentState(raw_logs=raw_logs, filename=filename)
        # LangGraph's invoke returns a dict-like state; rebuild as Pydantic for convenience
        result_dict = graph.invoke(initial)
        final = IncidentState.model_validate(result_dict)
        status.update(label=f"Done — {len(final.trace)} agent steps", state="complete")

    # ---------- Tabs ----------
    tab_inc, tab_slack, tab_jira, tab_cb, tab_trace = st.tabs(
        ["Incidents", "Slack", "JIRA", "Cookbook", "Trace"]
    )

    with tab_inc:
        if not final.incidents:
            st.info("No incidents detected.")
        for inc in final.incidents:
            with st.expander(f"[{inc.severity.value}] {inc.title}", expanded=True):
                st.write(f"**Category:** {inc.category.value}")
                st.write(f"**Summary:** {inc.summary}")
                if inc.affected_components:
                    st.write(f"**Affected:** {', '.join(inc.affected_components)}")
                rem = next((r for r in final.remediations if r.incident_id == inc.id), None)
                if rem:
                    st.markdown(f"**Root cause:** {rem.root_cause}")
                    st.markdown("**Steps:**")
                    for i, s in enumerate(rem.steps, 1):
                        st.markdown(f"{i}. {s}")
                    if rem.references:
                        st.caption("RAG references: " + " · ".join(rem.references))

    with tab_slack:
        if not final.slack_messages:
            st.info("No Slack messages.")
        for m in final.slack_messages:
            tag = "🟡 mock" if m.dry_run else "✅ live"
            with st.expander(f"{tag} → {m.channel} (incident {m.incident_id})"):
                st.code(m.text, language="markdown")

    with tab_jira:
        if not final.jira_tickets:
            st.info("No JIRA tickets created (no HIGH/CRITICAL incidents).")
        for t in final.jira_tickets:
            tag = "🟡 mock" if t.dry_run else "✅ live"
            with st.expander(f"{tag} {t.key} — {t.summary}"):
                st.write(f"**Priority:** {t.priority}")
                st.markdown(t.description)

    with tab_cb:
        if not final.cookbook:
            st.info("No cookbook entries.")
        for entry in final.cookbook:
            with st.expander(entry.title):
                st.markdown(f"**When to use:** {entry.when_to_use}")
                for i, s in enumerate(entry.steps, 1):
                    st.markdown(f"{i}. {s}")

    with tab_trace:
        for step in final.trace:
            icon = {"ok": "✅", "error": "❌", "running": "⏳"}.get(step.status, "•")
            st.markdown(f"{icon} **{step.agent}** — {step.note or ''}")
            st.caption(f"{step.started_at} → {step.finished_at or '...'}")
        if final.errors:
            st.error("Errors:\n" + "\n".join(final.errors))

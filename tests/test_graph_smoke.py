"""
End-to-end smoke test — exercises the full graph in mock mode.

Validates that:
- The graph builds and runs to completion
- Every agent step produced a trace entry
- Slack/JIRA tools fall back to mock cleanly when no creds are set
"""
from pathlib import Path

import pytest

from src.graph import build_graph
from src.state import IncidentState


@pytest.fixture
def sample_log() -> str:
    path = Path("data/sample_logs/api_5xx_burst.log")
    if not path.exists():
        pytest.skip("sample log missing")
    return path.read_text()


def test_graph_runs_end_to_end(sample_log: str):
    graph = build_graph()
    initial = IncidentState(raw_logs=sample_log, filename="api_5xx_burst.log")
    result = graph.invoke(initial)
    final = IncidentState.model_validate(result)

    assert len(final.parsed_events) > 0, "parser produced no events"
    assert len(final.incidents) >= 1, "classifier produced no incidents"
    assert len(final.remediations) >= 1, "remediation produced no plans"
    assert len(final.slack_messages) >= 1, "slack notifier sent nothing"

    # Trace should include every named agent
    traced = {step.agent for step in final.trace}
    expected = {"parser", "classifier", "severity", "rag_retriever", "remediation",
                "critic", "slack_notifier", "cookbook"}
    assert expected.issubset(traced), f"missing trace steps: {expected - traced}"

    # In mock mode, Slack messages should be marked dry_run
    assert all(m.dry_run for m in final.slack_messages)

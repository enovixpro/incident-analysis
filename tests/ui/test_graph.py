"""Mermaid pipeline graph tests.

The most important invariant: every LangGraph node we wired in src/graph.py
must be findable in the rendered SVG, indexed by its node name. If the
Mermaid ID format changes, or the regex in app.js drifts, this catches it
before the dashboard silently stops animating.
"""
from __future__ import annotations

from playwright.sync_api import expect

EXPECTED_NODES = {
    "parse", "classify", "score_severity", "rag", "remediation", "critic",
    "fanout", "slack_notifier", "jira_creator", "skip_jira", "cookbook", "aggregate",
}


def test_mermaid_renders_all_nodes(page_with_console):
    """Every expected node name should be parseable from the rendered SVG ids."""
    page, _, _ = page_with_console

    # Wait until renderGraph() has populated the host
    page.wait_for_selector("#mermaid-host g.node", timeout=5000)

    nodes = page.locator("#mermaid-host g.node").all()
    found_names = set()
    for node in nodes:
        node_id = node.get_attribute("id") or ""
        # Same regex as app.js renderGraph()
        import re
        m = re.search(r"flowchart-([A-Za-z_]+)-\d+$", node_id)
        if m:
            found_names.add(m.group(1))

    missing = EXPECTED_NODES - found_names
    extra = found_names - EXPECTED_NODES
    assert not missing, f"missing nodes in SVG: {missing}"
    assert not extra, f"unexpected extra nodes in SVG: {extra}"


def test_app_console_logs_indexed_nodes(page_with_console):
    """The app's own '[pipeline] indexed nodes:' log should list all 12."""
    page, messages, _ = page_with_console
    # Page has loaded; the log should have fired during renderGraph()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#mermaid-host g.node')).length >= 12",
        timeout=5000,
    )
    indexed_logs = [m for m in messages if "indexed nodes" in m]
    assert indexed_logs, "expected '[pipeline] indexed nodes:' console log"
    log_line = indexed_logs[-1]
    # Chromium renders JS arrays as [a, b, c] (no quotes), so substring-check each name.
    # All 12 node names are non-overlapping so this is unambiguous.
    for name in EXPECTED_NODES:
        assert name in log_line, f"node {name} not present in indexed-nodes log: {log_line}"


def test_legend_visible(page_with_console):
    page, _, _ = page_with_console
    legend = page.locator(".legend")
    expect(legend).to_be_visible()
    # All four state dots should be in the legend
    for kind in ("idle", "active", "done", "error"):
        expect(page.locator(f".dot.dot-{kind}")).to_be_visible()

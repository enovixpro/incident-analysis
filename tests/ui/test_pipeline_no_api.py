"""End-to-end pipeline smoke test through the UI, with no LLM API keys.

The conftest scrubs ANTHROPIC_API_KEY / OPENROUTER_API_KEY before launching
the server, so each LLM agent's try/except fires and emits a placeholder
fallback. The graph still runs to completion — incidents land, downstream
agents run on the placeholder, results render. This is the most valuable
smoke test for catching wiring breaks (SSE event format, gate prediction,
result rendering) without burning real API tokens.
"""
from __future__ import annotations

from playwright.sync_api import expect


def test_pipeline_runs_to_completion_via_fallback(page_with_console):
    page, _, _ = page_with_console

    # Pick a sample and run
    page.locator("#sample-select").select_option("api_5xx_burst.log")
    expect(page.locator("#btn-run")).to_be_enabled()
    page.locator("#btn-run").click()

    # Status flips to "complete" once the pipeline (with fallbacks) finishes.
    # Fallback agents are fast (no API round-trip), so this should be quick.
    expect(page.locator("#graph-status")).to_have_text("complete", timeout=30_000)

    # All 12 nodes should have ended in either "done" or "error" state. We
    # accept either because the LLM agents fail-and-fall-back, which trips
    # the error class on those nodes — that's expected without API keys.
    nodes = page.locator("#mermaid-host g.node").all()
    settled = 0
    for n in nodes:
        cls = n.get_attribute("class") or ""
        if "node-done" in cls or "node-error" in cls:
            settled += 1
    assert settled >= 10, f"expected most nodes settled, only {settled} did"

    # Incidents tab should show at least one card (the fallback incident
    # the classifier emits when its API call fails).
    expect(page.locator("#body-incidents .card").first).to_be_visible(timeout=5_000)

    # Slack tab should show a mock message
    page.locator(".tab[data-tab='slack']").click()
    expect(page.locator("#body-slack .card .badge").first).to_have_text("mock")

    # Trace tab should have multiple agent steps recorded
    page.locator(".tab[data-tab='trace']").click()
    trace_items = page.locator(".trace-item").all()
    assert len(trace_items) >= 8, f"expected >= 8 trace items, got {len(trace_items)}"

    # No-API run → all 5 LLM agents fall back → alert banner should show with
    # the auth-failure pattern (no LLM key configured).
    banner = page.locator("#alert-banner")
    expect(banner).to_be_visible()
    headline = page.locator("#alert-headline").text_content()
    assert "API key" in headline or "credit" in headline or "LLM" in headline, \
        f"banner headline should mention the LLM problem; got: {headline}"

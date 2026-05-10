"""Smoke tests — page loads and major scaffolding is present."""
from __future__ import annotations

from playwright.sync_api import expect


def test_page_loads_with_title(page_with_console):
    page, _, _ = page_with_console
    expect(page).to_have_title("VIGIL — AI Incident Analyzer")


def test_topbar_present(page_with_console):
    page, _, _ = page_with_console
    expect(page.locator(".brand-title")).to_have_text("VIGIL")
    expect(page.locator(".brand-subtitle")).to_have_text("AI Incident Analyzer")
    expect(page.locator("#cost-meter")).to_be_visible()
    expect(page.locator("#theme-toggle")).to_be_visible()


def test_brand_status_starts_idle(page_with_console):
    """The replacement for the dead 'multi-agent · live' pill — should start idle
    (grey dot, 'idle' text) on initial load, before any run."""
    page, _, _ = page_with_console
    pill = page.locator("#brand-status")
    expect(pill).to_be_visible()
    expect(pill).to_have_attribute("data-state", "idle")
    expect(page.locator("#brand-status .brand-status-text")).to_have_text("idle")


def test_three_panels_render(page_with_console):
    page, _, _ = page_with_console
    expect(page.locator("#panel-log")).to_be_visible()
    expect(page.locator("#panel-graph")).to_be_visible()
    expect(page.locator("#panel-results")).to_be_visible()


def test_env_pills_resolve(page_with_console):
    """Env pills start as 'pending' and resolve to live/mock/missing on /api/env load."""
    page, _, _ = page_with_console
    # Wait for loadEnv() to run — the anthropic pill should no longer say "Anthropic …"
    pill = page.locator("#pill-anthropic")
    expect(pill).not_to_contain_text("…", timeout=5000)


def test_chat_fab_present_and_drawer_hidden(page_with_console):
    page, _, _ = page_with_console
    expect(page.locator("#chat-toggle")).to_be_visible()
    drawer = page.locator("#chat-drawer")
    # Drawer exists in DOM but isn't open (no chat-open class)
    expect(drawer).not_to_have_class("chat-open")
    # And inert is set so it's non-interactive
    assert drawer.get_attribute("inert") is not None


def test_alert_banner_hidden_on_initial_load(page_with_console):
    """The LLM-error banner has `hidden` in markup. The CSS must respect that
    on initial load — otherwise an empty banner with no text shows up.
    Regression test for the `display: flex` overriding `hidden` attribute."""
    page, _, _ = page_with_console
    expect(page.locator("#alert-banner")).to_be_hidden()

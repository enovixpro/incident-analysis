"""Smoke tests — page loads and major scaffolding is present."""
from __future__ import annotations

from playwright.sync_api import expect


def test_page_loads_with_title(page_with_console):
    page, _, _ = page_with_console
    expect(page).to_have_title("Incident Analysis Suite")


def test_topbar_present(page_with_console):
    page, _, _ = page_with_console
    expect(page.locator(".brand-title")).to_have_text("Incident Analysis Suite")
    expect(page.locator("#cost-meter")).to_be_visible()
    expect(page.locator("#theme-toggle")).to_be_visible()


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

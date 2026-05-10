"""Interaction tests — clicks and toggles work as expected."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect


def test_sample_dropdown_populates(page_with_console):
    """The dropdown should list at least the original two sample logs."""
    page, _, _ = page_with_console
    select = page.locator("#sample-select")
    options = select.locator("option").all_text_contents()
    assert "api_5xx_burst.log" in options
    assert "k8s_pod_crash.log" in options


def test_selecting_sample_loads_log_and_enables_buttons(page_with_console):
    page, _, _ = page_with_console
    page.locator("#sample-select").select_option("api_5xx_burst.log")
    # Wait for fetch + setLog
    expect(page.locator("#log-stream")).to_contain_text("checkout-service")
    expect(page.locator("#btn-run")).to_be_enabled()
    expect(page.locator("#btn-tail")).to_be_enabled()
    expect(page.locator("#log-meta")).to_contain_text("api_5xx_burst.log")


def test_uploading_a_file_enables_tail_button(page_with_console, tmp_path: Path):
    """Tail mode should be enabled for uploads (was a bug we fixed)."""
    page, _, _ = page_with_console
    fake_log = tmp_path / "uploaded.log"
    fake_log.write_text(
        '{"timestamp":"2026-04-19T10:00:01Z","level":"INFO","source":"svc","message":"hi"}\n'
    )
    page.locator("#file-input").set_input_files(str(fake_log))
    expect(page.locator("#log-stream")).to_contain_text("hi")
    expect(page.locator("#btn-run")).to_be_enabled()
    expect(page.locator("#btn-tail")).to_be_enabled()


def test_tab_switching(page_with_console):
    page, _, _ = page_with_console
    # Default is Incidents
    expect(page.locator(".tab[data-tab='incidents']")).to_have_class(
        # has both base class and active class
        "tab tab-active"
    )
    # Click Trace
    page.locator(".tab[data-tab='trace']").click()
    expect(page.locator(".tab[data-tab='trace']")).to_have_class("tab tab-active")
    expect(page.locator("#body-trace")).to_be_visible()
    expect(page.locator("#body-incidents")).to_be_hidden()


def test_theme_toggle_flips_data_theme(page_with_console):
    page, _, _ = page_with_console
    html = page.locator("html")
    initial = html.get_attribute("data-theme")
    page.locator("#theme-toggle").click()
    # After click, attribute flips. Use a function-style assertion to wait briefly.
    page.wait_for_function(
        f"() => document.documentElement.getAttribute('data-theme') !== '{initial}'",
        timeout=2000,
    )
    new_theme = html.get_attribute("data-theme")
    assert new_theme != initial
    assert new_theme in ("light", "dark")


def test_chat_drawer_open_close(page_with_console):
    page, _, _ = page_with_console
    drawer = page.locator("#chat-drawer")
    fab = page.locator("#chat-toggle")

    # Initially closed
    expect(drawer).not_to_have_class("chat-open")
    assert drawer.get_attribute("inert") is not None

    # Open via FAB
    fab.click()
    expect(drawer).to_have_class("chat-drawer chat-open")
    # inert attribute is removed when open
    assert drawer.get_attribute("inert") is None
    # Quick-suggestion chips should be present
    expect(page.locator(".chat-chip").first).to_be_visible()

    # Close via X
    page.locator("#chat-close").click()
    expect(drawer).not_to_have_class("chat-open")
    assert drawer.get_attribute("inert") is not None


def test_tail_mode_button_toggles(page_with_console):
    """Tail Mode is a toggle now — pressed state visible, label changes."""
    page, _, _ = page_with_console
    btn = page.locator("#btn-tail")
    label = page.locator("#upload-label-text")

    # Initially: not pressed, default label
    expect(btn).to_have_attribute("aria-pressed", "false")
    expect(label).to_contain_text("upload a file")

    # Click → pressed, label flips
    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "true")
    expect(label).to_contain_text("Point at a file")

    # Click again → back to default
    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "false")
    expect(label).to_contain_text("upload a file")


def test_strict_critic_toggle_persists_state(page_with_console):
    page, _, _ = page_with_console
    toggle = page.locator("#strict-toggle")
    # Initially unchecked
    expect(toggle).not_to_be_checked()
    toggle.check()
    expect(toggle).to_be_checked()
    toggle.uncheck()
    expect(toggle).not_to_be_checked()

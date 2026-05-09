"""
Playwright UI test fixtures.

Spins up a fresh `uvicorn web.server:app` on a random free port for the test
session, polls until it's ready, then yields the base_url. Tears down at the
end of the session.

The server is launched with the LLM keys cleared so the agents take their
fallback paths — meaning UI tests don't burn API tokens. JIRA / Slack creds
are also cleared so those run in mock mode and we don't accidentally spam a
real channel from a test run.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"


def _free_port() -> int:
    """Bind ephemerally to grab a free port, then immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url() -> Iterator[str]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Sandbox env: no LLM keys (force fallback paths), no live integrations.
    # IMPORTANT: web/server.py calls load_dotenv() at import time, which would
    # re-populate these from .env if they were merely popped. Setting them to
    # the empty string preserves them so dotenv (override=False by default)
    # leaves them alone — and bool("") is False everywhere we check.
    env = os.environ.copy()
    for k in (
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
        "SLACK_BOT_TOKEN", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "LANGSMITH_API_KEY", "LANGSMITH_TRACING",
    ):
        env[k] = ""

    proc = subprocess.Popen(
        [str(PYTHON_BIN), "-m", "uvicorn", "web.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Poll readiness for up to 15s
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/api/env", timeout=1)
            if r.ok:
                break
        except requests.RequestException:
            time.sleep(0.2)
    else:
        proc.kill()
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        raise RuntimeError(f"uvicorn didn't come up on {base_url}\n{out}")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def page_with_console(page, server_url):
    """Page fixture that captures console messages so tests can assert on them
    and surface any uncaught JS errors in the failure message."""
    messages: list[str] = []
    errors: list[str] = []
    page.on("console", lambda msg: messages.append(f"[{msg.type}] {msg.text}"))
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(server_url, wait_until="networkidle")

    yield page, messages, errors

    # Surface any JS errors as test failures even if the test logic passed.
    if errors:
        raise AssertionError("Uncaught JS errors during test:\n" + "\n".join(errors))


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Reduce noise: ignore HTTPS errors (we're hitting http://), set a sane viewport."""
    return {**browser_context_args, "ignore_https_errors": True, "viewport": {"width": 1400, "height": 900}}

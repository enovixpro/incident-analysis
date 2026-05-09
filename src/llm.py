"""
LLM provider routing.

By default we talk to Anthropic directly via the Anthropic SDK. If the env var
`OPENROUTER_API_KEY` is set, we point the same SDK at OpenRouter's
Anthropic-compatible endpoint instead — protocol-identical, so all features the
underlying model supports (tool use, prompt caching, extended thinking) pass
through unchanged.

This is a thin shim. Agents call `get_client()` / `get_model()` and stay
provider-agnostic; nothing in the agents themselves needs to know about
OpenRouter.
"""
from __future__ import annotations

import os
from functools import lru_cache

import anthropic

# Default model when nothing is configured.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"

# NB: do NOT include `/v1` here — the Anthropic SDK appends `/v1/messages`
# itself, so a base URL of `.../api/v1` would double up to `.../api/v1/v1/messages`
# and OpenRouter would serve its catch-all HTML landing page.
OPENROUTER_BASE_URL = "https://openrouter.ai/api"


def using_openrouter() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


def provider_name() -> str:
    return "openrouter" if using_openrouter() else "anthropic"


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Return a configured Anthropic SDK client (Anthropic API or OpenRouter)."""
    if using_openrouter():
        return anthropic.Anthropic(
            api_key=os.environ["OPENROUTER_API_KEY"].strip(),
            base_url=OPENROUTER_BASE_URL,
            # Optional but nice — show up in OpenRouter dashboards.
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/incident-suite"),
                "X-Title": os.getenv("OPENROUTER_TITLE", "Incident Analysis Suite"),
            },
        )
    return anthropic.Anthropic()  # uses ANTHROPIC_API_KEY from env


def get_model() -> str:
    """
    Return the model id to use.

    OpenRouter expects the `provider/model` form (e.g. `anthropic/claude-sonnet-4.5`).
    Anthropic direct expects the bare model id (e.g. `claude-sonnet-4-5`).

    Set `OPENROUTER_MODEL` to override when on OpenRouter; `ANTHROPIC_MODEL`
    when on Anthropic direct.
    """
    if using_openrouter():
        return os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    return os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)

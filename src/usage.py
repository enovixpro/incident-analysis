"""
Per-run token usage + cost accumulator.

CONCEPT: production credibility. Every Anthropic response carries a `usage` block
with input/output token counts and (when prompt caching is wired) cache_read /
cache_creation token counts. This module:

  - exposes a thread-local accumulator each agent appends to after its API call
  - applies a model-aware pricing table to compute USD cost
  - lets the server pump pull "what's new since the last emit" for SSE streaming

Thread-local is sufficient because LangGraph's sync `stream()` serialises node
execution within a single thread, even for the parallel fan-out section.

Caching note: Anthropic prompt caching only kicks in when the cached content
exceeds the model's minimum (1024 tokens for Sonnet/Opus, 2048 for Haiku). Our
per-agent system prompts sit just below that threshold today — instrumentation
is in place so the moment a prompt grows past it, cache hits register
automatically. Cost still tracks correctly either way.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ----- Pricing (USD per 1M tokens) ----------------------------------------------
# Source: https://www.anthropic.com/pricing — keep this aligned with current rates.
# Cache write = 1.25× input; cache read = 0.10× input.

PRICING: dict[str, dict[str, float]] = {
    # Sonnet 4.x family
    "claude-sonnet-4-5":             {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6":             {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    # Opus 4.x family
    "claude-opus-4-7":               {"input": 15.00, "output": 75.00, "cache_create": 18.75, "cache_read": 1.50},
    # Haiku 4.x family
    "claude-haiku-4-5":              {"input": 1.00, "output": 5.00,  "cache_create": 1.25, "cache_read": 0.10},
    # OpenRouter aliases — same underlying models, OpenRouter passes pricing through
    # close to direct rates. These rates are best-effort approximations; for exact
    # billing, query OpenRouter's /models endpoint at runtime.
    "anthropic/claude-sonnet-4.5":   {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "anthropic/claude-sonnet-4-5":   {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "anthropic/claude-sonnet-4.6":   {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "anthropic/claude-opus-4.7":     {"input": 15.00, "output": 75.00, "cache_create": 18.75, "cache_read": 1.50},
    "anthropic/claude-haiku-4.5":    {"input": 1.00, "output": 5.00,  "cache_create": 1.25, "cache_read": 0.10},
}

# Fallback used when the configured model isn't in the table.
_FALLBACK = PRICING["claude-sonnet-4-5"]


def _rates_for(model: str) -> dict[str, float]:
    # Tolerate model ids with date suffixes ("claude-haiku-4-5-20251001") by
    # matching the longest known prefix.
    for key in sorted(PRICING.keys(), key=len, reverse=True):
        if model.startswith(key):
            return PRICING[key]
    return _FALLBACK


# ----- Records ------------------------------------------------------------------

@dataclass
class UsageRecord:
    agent: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        r = _rates_for(self.model)
        return (
            self.input_tokens          * r["input"]
            + self.output_tokens       * r["output"]
            + self.cache_creation_tokens * r["cache_create"]
            + self.cache_read_tokens   * r["cache_read"]
        ) / 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = self.cost_usd
        return d


# ----- Collection ---------------------------------------------------------------
# We use a process-global, lock-protected list rather than thread-local because
# LangGraph's parallel fan-out dispatches nodes across worker threads, so a
# thread-local accumulator initialized in the pump thread wouldn't see records
# appended by the cookbook/slack/jira branches. Single-run demo assumption: only
# one pipeline runs at a time. For multi-tenant operation you'd key this by run_id.

_lock = threading.Lock()
_records: list[UsageRecord] = []


def start_collection() -> None:
    """Reset the accumulator. Call at run start."""
    global _records
    with _lock:
        _records = []


def record(agent: str, model: str, usage_obj: Any) -> None:
    """Called by each agent after a successful messages.create() call."""
    rec = UsageRecord(
        agent=agent,
        model=model,
        input_tokens=int(getattr(usage_obj, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage_obj, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(usage_obj, "cache_creation_input_tokens", 0) or 0),
    )
    with _lock:
        _records.append(rec)


def all_records() -> list[UsageRecord]:
    with _lock:
        return list(_records)


def drain_new(since_index: int) -> tuple[list[UsageRecord], int]:
    """Return records added since `since_index` and the new high-water index."""
    with _lock:
        new_recs = _records[since_index:]
        return new_recs, len(_records)

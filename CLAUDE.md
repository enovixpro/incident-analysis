# CLAUDE.md

Guidance for future Claude sessions working in this repo. Skim before changing anything non-trivial.

---

## What this is

Multi-agent DevOps incident analysis suite. Hackathon project. Takes raw ops logs → groups them into incidents → scores severity → retrieves similar past incidents (RAG) → generates remediation → critic review (with extended thinking + self-critique loop) → fans out to Slack / JIRA / cookbook.

**Three interfaces:**
- **FastAPI dashboard** at [web/](web/) — primary UI, live SSE streaming, animated DAG, embedded chat assistant. `make web`.
- **MCP server** at [mcp_server.py](mcp_server.py) — same pipeline as MCP tools (Claude Desktop, `claude` CLI). `make mcp`.
- **Python API** — `from src.graph import get_graph`. Used by [tests/test_graph_smoke.py](tests/test_graph_smoke.py).

The legacy Streamlit UI ([app.py](app.py), `make run`) still works but isn't the recommended path.

Read [README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and [docs/OPERATING.md](docs/OPERATING.md) for full detail. This file is for what's *not* in those.

---

## Hard-won knowledge — do not relearn these

These all wasted real time during build. If you're touching the related area, read the gotcha first.

### LLM / Anthropic

- **OpenRouter base URL must be `https://openrouter.ai/api`** — NOT `/api/v1`. The Anthropic SDK appends `/v1/messages` itself, so `/api/v1` doubles to `/api/v1/v1/messages` and OpenRouter serves its catch-all HTML landing page. See [src/llm.py](src/llm.py).
- **Extended thinking + forced tool_choice = 400.** Anthropic rejects `tool_choice={"type":"tool", "name":...}` AND `{"type":"any"}` when `thinking` is enabled. Only `"auto"` works. We rely on the system prompt's explicit "Call X tool exactly once" instruction. See [src/agents/critic.py](src/agents/critic.py).
- **Extended thinking constraints**: `temperature=1` only, `max_tokens > budget_tokens`.
- **Prompt caching needs ≥ 1024 tokens** for Sonnet/Opus to actually cache. Only the classifier prompt currently exceeds this. The other agents have `cache_control` set but don't fire today. Don't "fix" this by padding prompts — instrumentation is correct.

### LangGraph

- **Parallel branches run in worker threads.** A `threading.local()` accumulator initialized in the pump thread won't see records appended by the cookbook (which runs in the parallel fan-out). Use a process-global lock-protected list. See [src/usage.py](src/usage.py).
- **Conditional edges are pure** — they only read state, never mutate. Counter increments (e.g., `critic_retries`) happen inside the relevant agent, not the routing function.
- **`stream_mode='updates'`** yields one chunk per node completion. Parallel branches may yield separate chunks or a combined one — handle both.

### MCP

- **stdio MCP servers must NEVER write to stdout.** Stdout is the protocol channel. Route all logging to stderr; pop `LANGSMITH_TRACING` to prevent stray prints from upstream. See top of [mcp_server.py](mcp_server.py).
- **FastMCP returns `list[dict]` as multiple TextContent blocks**, one per element. Not a JSON-encoded list in a single block. Clients handle this fine; just be aware when writing test scripts.

### JIRA

- **`JIRA_PROJECT_KEY=OPS` is the wrong default for everyone.** Probe their JIRA via `GET /rest/api/3/project/search` to find the real project keys before assuming.
- **JIRA Cloud REST v3 rejects plain string descriptions.** Must be ADF (Atlassian Document Format). See `_to_adf` in [src/tools/jira.py](src/tools/jira.py).
- **`Bug` issue type doesn't exist in every project template.** Default to `Task`. Configurable via `JIRA_ISSUE_TYPE`.
- **Don't trust `atlassian-python-api` 4.x** for `create_issue` — used to silently return `key: None` on bad payloads. We use raw `requests` against `/rest/api/3/issue` so errors surface verbatim.

### Frontend

- **Mermaid 11 node IDs are `<svg-id>-flowchart-<name>-<n>`**, not `flowchart-<name>-<n>`. Match with substring regex `/flowchart-([A-Za-z_]+)-\d+$/`, not `^`-anchored. See [web/static/app.js](web/static/app.js) `renderGraph()`.
- **Conditional graph branches must mirror server-side gate logic on the client** or unused branches stay pulsing forever. The two gates are `route_after_critic` and `should_create_ticket`. See `onNodeCompleted` in [web/static/app.js](web/static/app.js).
- **Mermaid theme change requires re-render.** When toggling light/dark, snapshot node states first, re-init mermaid, re-render, then restore states. See `applyTheme()` in [web/static/app.js](web/static/app.js).
- **Static files served by FastAPI's `StaticFiles` aren't cached server-side**, but browsers cache aggressively. Hard refresh (Cmd+Shift+R) after JS/CSS changes — restart not required.

### UI tests (Playwright)

- **Tests scrub LLM keys before launching the server** ([tests/ui/conftest.py](tests/ui/conftest.py)). Each agent's try/except fires and emits a fallback. Tests verify the *plumbing* (rendering, streaming, gate logic, results panels) not the *content* (which depends on the LLM).
- **Selectors should target the existing stable IDs** (`#sample-select`, `#chat-toggle`, `g.node` etc.), not class names. CSS class names get refactored; IDs are functional and stable.
- **For the Mermaid graph, the test mirrors app.js's regex** `/flowchart-([A-Za-z_]+)-\d+$/`. If you change the regex in one place, change it in both. There's also a console-log assertion (`[pipeline] indexed nodes:`) as a second-line check.
- The pipeline-smoke test (`test_pipeline_no_api.py`) accepts nodes ending in either `node-done` or `node-error` because LLM agents fail-and-fall-back when there's no key — that's expected and the pipeline still completes.
- **Don't add new UI tests for content that depends on LLM output.** Brittle and not worth it.

### Chat assistant

- **`/api/chat` is POST-with-streaming-body**, not SSE-via-EventSource. EventSource doesn't support POST, so the chat uses `fetch()` + manual SSE-frame parsing on the client (`parseSseFrame` in app.js). The frame format is the same as our pipeline streaming — keep it that way so the parser can be reused.
- **Chat is stateless on the server** — the client owns conversation history (capped at last 10 messages) and ships the current run state with every turn. Don't add server-side session state without good reason.
- **The assistant must only reason from `<run_state>`**, not invent. The system prompt at [src/prompts/assistant.md](src/prompts/assistant.md) enforces this. If you change the snapshot shape in `snapshotStateForChat()`, update the prompt's "What you can see" section in lockstep.
- **Chat usage is recorded under the agent name `"assistant"`** in [src/usage.py](src/usage.py). Cost meter aggregates it alongside the pipeline agents.

### RAG

- **The embedder is a custom feature-hashing BoW**, not sentence-transformers. Deliberate choice (zero external model download). Trade-off: similarity scores top out around 0.5 even for near-perfect matches; transformer embeddings would give 0.7-0.9.
- **The 0.4 surface threshold is calibrated to BoW.** If you swap embedders, recalibrate. Env var: `RAG_SURFACE_THRESHOLD`.
- **Re-seeding is idempotent.** `make seed` does an upsert, so re-running is safe. Wipe with `rm -rf .chroma/` if you need a clean slate.

---

## Conventions specific to this codebase

- **Every LLM agent follows the same skeleton**: load prompt from `src/prompts/<name>.md` → build user message from state → call `llm.get_client().messages.create(...)` with forced tool choice → parse `tool_use` block → return partial state dict. On any exception, return a placeholder fallback so the graph completes (load-bearing for the smoke test which runs without API keys).
- **Agents stay provider-agnostic.** Always use `from src import llm; llm.get_client()` and `llm.get_model()`. Never instantiate `anthropic.Anthropic()` directly in an agent.
- **System prompts live in markdown files**, not in Python strings. They're versioned alongside code in [src/prompts/](src/prompts/).
- **State updates are partial dicts.** Each node returns `{field: value, ...}` — LangGraph merges. Single-writer fields use last-write-wins; `trace` and `errors` are accumulators (`Annotated[..., add]`).
- **Mock fallbacks are intentional.** Slack and JIRA tools return realistic mock responses when creds are missing. Agents fall back to placeholders on API failure. Removing these breaks the smoke test.
- **No new top-level files** unless absolutely necessary. Use existing locations:
  - New agent → `src/agents/<name>.py` + `src/prompts/<name>.md`
  - New tool → `src/tools/<name>.py`
  - New helper → extend an existing module if it fits, only create a new one if there's clear ownership

---

## Common commands

```bash
make install   # pip install -r requirements.txt
make seed      # load data/seed_incidents.jsonl into Chroma
make web       # FastAPI dashboard on :8000   ← primary
make mcp       # MCP stdio server
make run       # legacy Streamlit on :8501
make test      # pytest unit + graph smoke (must pass without API keys)
make test-ui   # Playwright UI tests (Chromium; ~30-60s; no API keys needed)
make clean     # wipe .chroma/ + caches
```

---

## Codebase map

```
src/
├── state.py           # IncidentState — typed Pydantic anchor + RAG threshold helpers
├── graph.py           # LangGraph wiring + conditional edges + parallel fan-out
├── llm.py             # Provider routing — Anthropic vs OpenRouter
├── usage.py           # Cost tracking — process-global accumulator (NOT thread-local)
├── agents/            # 5 LLM agents + 2 integration agents (slack, jira) + 1 RAG node
├── tools/             # vectorstore (Chroma), slack, jira, seed_vectorstore
├── parsers/           # log_parser (heuristic, no LLM)
└── prompts/           # Per-agent system prompts in markdown (incl. assistant.md)

web/                   # FastAPI dashboard
├── server.py          # SSE backend, run management
└── static/            # HTML / CSS / vanilla JS frontend (no build step)

mcp_server.py          # MCP stdio server — top-level so it's easy to point Claude Desktop at
app.py                 # Legacy Streamlit UI

data/
├── seed_incidents.jsonl   # 30 past incidents — RAG corpus
└── sample_logs/           # Demo input logs

docs/
├── ARCHITECTURE.md    # Internals deep dive
└── OPERATING.md       # Env vars, JIRA setup, troubleshooting
```

---

## What NOT to do

- **Don't add backwards-compat shims** for things that don't exist yet. This is greenfield code.
- **Don't bypass the agent fallback paths.** The smoke test passes without API keys *because* of them.
- **Don't write multi-paragraph docstrings.** The system prompts and CLAUDE.md cover the why; one-line comments cover any non-obvious how.
- **Don't introduce JS build tooling.** The frontend is intentionally vanilla — module imports work fine.
- **Don't make MCP responses streaming** — the protocol expects blocking responses, and the dashboard is the path for live streaming.
- **Don't mix logging with stdout in `mcp_server.py`** — it'll corrupt the JSON-RPC frames.
- **Don't pad system prompts to hit the 1024-token caching threshold.** Wait until prompts grow naturally (e.g., adding a shared SRE handbook preamble).
- **Don't use `threading.local()` for anything that needs to be visible across LangGraph parallel branches.**

---

## Maintaining this file

Update CLAUDE.md when you:
- discover a new gotcha worth recording
- add or change a convention
- add a major component (new agent, new transport, new integration)
- add a new env var or config knob
- learn something the hard way that future-you would want to know

*Don't* update it for routine code edits, bug fixes that don't affect conventions, or things already covered in [README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), or [docs/OPERATING.md](docs/OPERATING.md).

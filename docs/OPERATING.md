# Operating guide

Everything you need to configure, run, and troubleshoot the suite.

---

## Environment variables

Copy [`.env.example`](../.env.example) to `.env` and fill in what you need. The dashboard's topbar pills tell you at a glance which integrations are live vs mock.

### LLM provider — pick one

| Variable | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | one of these two | Anthropic direct |
| `ANTHROPIC_MODEL` | optional | Defaults to `claude-sonnet-4-5` |
| `OPENROUTER_API_KEY` | one of these two | Routes through OpenRouter; **takes precedence** over `ANTHROPIC_API_KEY` when both are set |
| `OPENROUTER_MODEL` | optional | Defaults to `anthropic/claude-sonnet-4.5`. Use the `provider/model` form. |
| `OPENROUTER_REFERER` / `OPENROUTER_TITLE` | optional | Show up in your OpenRouter dashboard for observability |

### Slack (optional)

| Variable | Notes |
|---|---|
| `SLACK_BOT_TOKEN` | If unset, slack runs in mock mode (logs payload, returns fake ts) |
| `SLACK_CHANNEL` | Defaults to `#incidents` |

### JIRA (optional)

| Variable | Notes |
|---|---|
| `JIRA_URL` | e.g. `https://your-org.atlassian.net` |
| `JIRA_EMAIL` | Your Atlassian account email |
| `JIRA_API_TOKEN` | Generate at https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_PROJECT_KEY` | **Must match a real project key in your JIRA**. See [JIRA setup](#jira-setup) below. |
| `JIRA_ISSUE_TYPE` | Defaults to `Task`. Set to `Bug` / `Story` / `Incident` / etc. depending on your project template. |

If any of `JIRA_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` is unset, JIRA runs in mock mode.

### Tracing (optional)

| Variable | Notes |
|---|---|
| `LANGSMITH_API_KEY` | If set, LangGraph emits traces to LangSmith automatically |
| `LANGSMITH_PROJECT` | Defaults to `incident-suite` |
| `LANGSMITH_TRACING` | Set to `true` to enable. Auto-popped if no key is set, to keep the console clean. |

---

## Three ways to run

### 1. Live dashboard (recommended)

```bash
make web                  # uvicorn on http://localhost:8000
```

Pick a sample log from the dropdown (or upload your own), optionally check **Strict critic** to force the loop, hit **Run pipeline** or **Tail mode**. The graph animates per-node; results stream into the right-hand tabs.

### 2. MCP server

For Claude Desktop / `claude` CLI / Cursor / any MCP client.

```bash
make mcp                  # stdio MCP server
```

To wire to Claude Desktop, add this to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "incident-suite": {
      "command": "/absolute/path/to/incident-suite/.venv/bin/python",
      "args": ["/absolute/path/to/incident-suite/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. In any conversation: *"Use the incident-suite tools to analyze this log dump:"* + paste a log. Three tools become available:

- `analyze_logs(raw_logs, strict_critic=False)` — full pipeline
- `analyze_log_file(path, strict_critic=False)` — convenience wrapper for a file path
- `search_past_incidents(query, k=3)` — RAG-only lookup

### 3. Python API (tests, batch jobs)

```python
from src.graph import get_graph
from src.state import IncidentState

graph = get_graph()
result = graph.invoke(IncidentState(raw_logs=open("some.log").read()))
final  = IncidentState.model_validate(result)
print(final.incidents)
print(final.cookbook)
```

This is what [tests/test_graph_smoke.py](../tests/test_graph_smoke.py) does.

---

## JIRA setup

The most common gotcha. The default `JIRA_PROJECT_KEY=OPS` won't match anyone's real JIRA — you need to set it to your actual project key.

### Find your project key

In JIRA, every issue has an id like `KAN-123`. The prefix (`KAN`) is the project key. Or programmatically, against your JIRA Cloud:

```bash
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
     "$JIRA_URL/rest/api/3/project/search" | jq '.values[] | {key, name}'
```

### Find the right issue type

`Bug` doesn't exist in every project template (e.g., business projects). Use the safest default `Task`, or list available types:

```bash
PROJECT_KEY=KAN
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
     "$JIRA_URL/rest/api/3/project/$PROJECT_KEY" | jq '.issueTypes[].name'
```

### What happens if it's wrong

The new JIRA tool ([src/tools/jira.py](../src/tools/jira.py)) surfaces the actual error response from JIRA. Look at the JIRA tab in the dashboard — failed tickets show a red `failed` badge and the verbatim API error. Common ones:

- `"valid project is required"` → `JIRA_PROJECT_KEY` doesn't match a real project
- `"issuetype is required"` or `"issuetype not found"` → set `JIRA_ISSUE_TYPE` to one your project supports
- `401 Unauthorized` → API token is wrong, or the email doesn't match the token's owner
- Description-related errors → unlikely; we send ADF (Atlassian Document Format) which Cloud requires

### Verify it works

Run the dashboard, pick a sample log, hit Run. If severity comes back HIGH or CRITICAL, a real JIRA ticket should land — the JIRA tab will show a clickable link to `/browse/<KEY>`.

---

## Troubleshooting

### The cost meter shows `0%` cache hit

This is expected for most agents today. Anthropic's prompt cache requires content >= 1024 tokens for Sonnet/Opus. Only the classifier prompt currently exceeds the threshold; the others sit just below. The `cache_control` directive is set correctly — when prompts grow past 1024 tokens (e.g., adding a shared SRE handbook preamble), cache hits will register automatically.

### OpenRouter returns HTML instead of JSON

Symptom: `state.errors` shows a long `<!DOCTYPE html>...` blob.

Cause: wrong base URL. The Anthropic SDK appends `/v1/messages` to the base URL itself. Setting base to `https://openrouter.ai/api/v1` would double up to `.../api/v1/v1/messages` — OpenRouter serves its catch-all landing page. Correct base is `https://openrouter.ai/api`. This is hard-coded in [src/llm.py](../src/llm.py) — only relevant if you're modifying it.

### LangSmith 401 spam

Symptom: console flooded with `LangSmithAuthError: 401 Unauthorized`.

Cause: `LANGSMITH_TRACING=true` is set in `.env` but `LANGSMITH_API_KEY` is empty or wrong.

Fix: either set a real key, or comment out `LANGSMITH_TRACING=true`. The web server auto-pops `LANGSMITH_TRACING` when no key is set, but the legacy Streamlit / direct invocations don't, so set them up consistently.

### `skip JIRA` and `JIRA` both highlighted in the graph

Should be fixed by [web/static/app.js](../web/static/app.js)'s gate prediction logic. If you see it: the client mirrors the server's `should_create_ticket` logic to predict which branch will fire, but if you've changed the gate, update the client logic too (search for `hasHighSev` in app.js).

### Smoke test fails on a fresh clone

```
make install
make seed                 # populates .chroma/
make test
```

If `seed` fails, it's usually a Python version mismatch (chromadb wants 3.11+). If a specific test fails, the most common cause is missing `data/sample_logs/` or `data/seed_incidents.jsonl` — verify they exist:

```bash
ls -la data/sample_logs/ data/seed_incidents.jsonl
```

### Mermaid graph doesn't render or animate

1. Check the browser console for `[pipeline] indexed nodes:` — should list 12 names. If it's empty, Mermaid didn't render and the IDs don't match the regex.
2. Hard refresh (`Cmd+Shift+R`) to bypass the browser's static-file cache.
3. Confirm http://localhost:8000/static/app.js loads (200) and contains the latest code.

### "I want to use a non-Anthropic model on OpenRouter"

OpenRouter's Anthropic-compatible `/messages` endpoint only handles Anthropic models. For non-Anthropic models (GPT, Llama, Mistral, …), you'd need to swap the Anthropic SDK for the OpenAI SDK (`openai.OpenAI(base_url="https://openrouter.ai/api/v1", ...)`) and translate the tool-use schema. Significant refactor — not currently supported. Recommended path: stay on Anthropic models via either provider.

---

## Resetting state

```bash
make clean                # remove __pycache__, .pytest_cache, .chroma/
make seed                 # re-index past incidents
```

`.chroma/` is the only persistent on-disk state. Removing it wipes the RAG corpus; `make seed` rebuilds from `data/seed_incidents.jsonl`.

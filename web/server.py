"""
FastAPI backend for the live-streaming dashboard.

Replaces the Streamlit UI. Exposes:
  GET  /                           — the dashboard HTML
  GET  /api/samples                — list sample log filenames
  GET  /api/sample/{name}          — fetch a sample log's contents
  POST /api/run                    — kick off a graph run, returns {run_id}
  POST /api/tail/{name}            — kick off a "tail-mode" run (log lines stream in
                                     visually, then the graph fires); returns {run_id}
  GET  /api/stream/{run_id}        — Server-Sent Events stream of pipeline events

The graph runs on a background thread; events flow through a per-run Queue and out
via SSE. The frontend turns each event into a graph-node animation + result panel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

load_dotenv()

# LangSmith tracing: ON whenever a key is present, OFF otherwise. Use direct
# assignment (not setdefault) so a stray empty-string LANGSMITH_TRACING from a
# Dockerfile ENV or .env doesn't silently disable tracing.
if os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGSMITH_TRACING"] = "true"
else:
    os.environ.pop("LANGSMITH_TRACING", None)

from src import llm, usage  # noqa: E402
from src.graph import get_graph  # noqa: E402
from src.state import IncidentState  # noqa: E402

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
SAMPLES_DIR = Path("data/sample_logs")

app = FastAPI(title="Incident Analysis Suite")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Active runs. Each run_id maps to a Queue of SSE event dicts; None is the close sentinel.
RUNS: dict[str, "Queue[Optional[dict]]"] = {}


# ---------- API models ----------

class RunRequest(BaseModel):
    raw_logs: str
    filename: Optional[str] = None
    strict_critic: bool = False


# ---------- Routes ----------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/env")
def env_status() -> dict:
    return {
        "provider": llm.provider_name(),  # "openrouter" or "anthropic"
        "model": llm.get_model(),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openrouter": llm.using_openrouter(),
        "slack": bool(os.getenv("SLACK_BOT_TOKEN")),
        "jira": all(os.getenv(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")),
    }


@app.get("/api/samples")
def list_samples() -> list[str]:
    if not SAMPLES_DIR.exists():
        return []
    return sorted(p.name for p in SAMPLES_DIR.glob("*.log"))


@app.get("/api/sample/{name}")
def get_sample(name: str) -> dict:
    path = SAMPLES_DIR / name
    if not path.exists() or path.parent.resolve() != SAMPLES_DIR.resolve():
        raise HTTPException(404, "sample not found")
    return {"name": name, "content": path.read_text()}


@app.post("/api/run")
def start_run(req: RunRequest) -> dict:
    run_id = uuid.uuid4().hex[:12]
    q: Queue[Optional[dict]] = Queue()
    RUNS[run_id] = q
    Thread(
        target=_pump_graph,
        args=(q, req.raw_logs, req.filename, req.strict_critic),
        daemon=True,
    ).start()
    return {"run_id": run_id}


class TailRequest(BaseModel):
    """Tail-mode kickoff. Provide either `sample_name` (read from data/sample_logs/)
    or `raw_logs` (caller supplies the content, e.g. an uploaded file)."""
    sample_name: Optional[str] = None
    raw_logs: Optional[str] = None
    filename: Optional[str] = None
    strict_critic: bool = False


@app.post("/api/tail")
def start_tail(req: TailRequest) -> dict:
    if req.sample_name:
        path = SAMPLES_DIR / req.sample_name
        if not path.exists() or path.parent.resolve() != SAMPLES_DIR.resolve():
            raise HTTPException(404, "sample not found")
        raw_logs = path.read_text()
        filename = req.sample_name
    elif req.raw_logs:
        raw_logs = req.raw_logs
        filename = req.filename or "uploaded.log"
    else:
        raise HTTPException(400, "either sample_name or raw_logs is required")

    run_id = uuid.uuid4().hex[:12]
    q: Queue[Optional[dict]] = Queue()
    RUNS[run_id] = q
    Thread(
        target=_pump_tail,
        args=(q, filename, raw_logs, req.strict_critic),
        daemon=True,
    ).start()
    return {"run_id": run_id}


_ASSISTANT_PROMPT_PATH = Path(__file__).parent.parent / "src" / "prompts" / "assistant.md"


class ChatMessage(BaseModel):
    role: str    # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    state: dict = {}   # the dashboard's current aggregate snapshot


@app.post("/api/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    """Stream Claude's response as SSE chunks. Stateless — the client owns history."""
    if not req.messages:
        raise HTTPException(400, "messages required")

    system_prompt = _ASSISTANT_PROMPT_PATH.read_text()
    state_blob = json.dumps(req.state, default=_json_default, indent=2) if req.state else "(no run yet)"
    system = (
        system_prompt
        + "\n\n<run_state>\n" + state_blob + "\n</run_state>"
    )
    msgs = [{"role": m.role, "content": m.content} for m in req.messages if m.role in ("user", "assistant")]

    def gen():
        try:
            from src import llm
            client = llm.get_client()
            model = llm.get_model()
            with client.messages.stream(
                model=model,
                max_tokens=1024,
                system=system,
                messages=msgs,
            ) as stream:
                for delta in stream.text_stream:
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
                final = stream.get_final_message()
            usage.record("assistant", model, final.usage)
            # Send a final usage event so the cost meter ticks up for chat too.
            new_records, _ = usage.drain_new(len(usage.all_records()) - 1)
            if new_records:
                yield f"event: usage\ndata: {json.dumps(new_records[0].to_dict())}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as e:
            logger.exception("chat failed")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/stream/{run_id}")
async def stream(run_id: str) -> EventSourceResponse:
    q = RUNS.get(run_id)
    if q is None:
        raise HTTPException(404, "run not found")

    async def generator():
        loop = asyncio.get_event_loop()
        try:
            while True:
                item = await loop.run_in_executor(None, q.get)
                if item is None:
                    break
                yield item
        finally:
            RUNS.pop(run_id, None)

    return EventSourceResponse(generator())


# ---------- Pumps (background workers) ----------

def _emit(q: "Queue[Optional[dict]]", event: str, payload: Any) -> None:
    q.put({"event": event, "data": json.dumps(payload, default=_json_default)})


def _json_default(o: Any) -> Any:
    # Pydantic v2 models
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    # Enums
    if hasattr(o, "value") and not isinstance(o, (str, int, float, bool, dict, list)):
        return o.value
    return str(o)


def _pump_graph(
    q: "Queue[Optional[dict]]",
    raw_logs: str,
    filename: Optional[str],
    strict_critic: bool = False,
) -> None:
    """Run the graph and forward each node's update as an SSE event."""
    try:
        _emit(q, "run_started", {"filename": filename, "log_bytes": len(raw_logs), "strict_critic": strict_critic})
        usage.start_collection()
        graph = get_graph()
        initial = IncidentState(raw_logs=raw_logs, filename=filename, strict_critic=strict_critic)

        usage_idx = 0
        for chunk in graph.stream(initial, stream_mode="updates"):
            for node_name, delta in chunk.items():
                _emit(q, "node_completed", {
                    "node": node_name,
                    "delta": _summarise_delta(delta),
                })
            usage_idx = _emit_usage_since(q, usage_idx)

        _emit_usage_since(q, usage_idx)  # final flush
        _emit(q, "done", {"summary": "pipeline complete"})
    except Exception as e:
        logger.exception("graph run failed")
        _emit(q, "error", {"error": str(e)})
    finally:
        q.put(None)


def _pump_tail(
    q: "Queue[Optional[dict]]",
    name: str,
    raw_logs: str,
    strict_critic: bool = False,
) -> None:
    """Stream the log file line-by-line for visual effect, then run the graph."""
    try:
        _emit(q, "run_started", {"filename": name, "log_bytes": len(raw_logs), "mode": "tail", "strict_critic": strict_critic})
        lines = [ln for ln in raw_logs.splitlines() if ln.strip()]
        # Pacing: spread the stream across ~2.5 seconds, with a floor and a ceiling.
        delay = max(0.05, min(0.20, 2.5 / max(1, len(lines))))
        for ln in lines:
            _emit(q, "log_line", {"line": ln})
            time.sleep(delay)
        # Brief pause so the user sees the log finish settling before the graph kicks off.
        time.sleep(0.3)
        # Now run the graph on the same content.
        usage.start_collection()
        graph = get_graph()
        initial = IncidentState(raw_logs=raw_logs, filename=name, strict_critic=strict_critic)
        usage_idx = 0
        for chunk in graph.stream(initial, stream_mode="updates"):
            for node_name, delta in chunk.items():
                _emit(q, "node_completed", {
                    "node": node_name,
                    "delta": _summarise_delta(delta),
                })
            usage_idx = _emit_usage_since(q, usage_idx)
        _emit_usage_since(q, usage_idx)
        _emit(q, "done", {"summary": "pipeline complete"})
    except Exception as e:
        logger.exception("tail run failed")
        _emit(q, "error", {"error": str(e)})
    finally:
        q.put(None)


def _emit_usage_since(q: "Queue[Optional[dict]]", since_idx: int) -> int:
    """Emit one `usage` event per new agent record. Returns the new index."""
    new_records, new_idx = usage.drain_new(since_idx)
    for rec in new_records:
        _emit(q, "usage", rec.to_dict())
    return new_idx


# ---------- Delta summarisation ----------
# We don't ship the entire state to the client — just the fields the UI cares about
# for each node. Keeps SSE payloads tight and the frontend code uncluttered.

def _summarise_delta(delta: Any) -> dict:
    if not isinstance(delta, dict):
        return {}
    out: dict = {}
    for key in (
        "parsed_events",
        "incidents",
        "rag_matches",
        "remediations",
        "critique",
        "slack_messages",
        "jira_tickets",
        "cookbook",
        "trace",
        "errors",
        "critic_retries",
    ):
        if key in delta:
            value = delta[key]
            # parsed_events can be very long; ship the count + first 5
            if key == "parsed_events" and isinstance(value, list):
                out[key] = {
                    "count": len(value),
                    "preview": [_json_default(ev) for ev in value[:5]],
                }
            else:
                out[key] = value
    return out

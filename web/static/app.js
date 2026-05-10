// Frontend for the live-streaming dashboard.
// Renders the LangGraph DAG via Mermaid, then animates node state from SSE events.

import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";

// ----- Theme management ----------------------------------------------------------
// Default = system preference, overridable + persisted via the topbar toggle.

const THEME_KEY = "incident-suite:theme";

function resolvedTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function initMermaid(theme) {
  mermaid.initialize({
    startOnLoad: false,
    theme: theme === "light" ? "default" : "dark",
    securityLevel: "loose",
  });
}

async function applyTheme(theme, { rerenderGraph = true } = {}) {
  document.documentElement.setAttribute("data-theme", theme);

  // Snapshot any active node states so we can restore them after re-rendering.
  const states = {};
  for (const [name, el] of Object.entries(nodeEls || {})) {
    if (el.classList.contains("node-active")) states[name] = "active";
    else if (el.classList.contains("node-done")) states[name] = "done";
    else if (el.classList.contains("node-error")) states[name] = "error";
  }

  initMermaid(theme);
  if (rerenderGraph) {
    await renderGraph();
    for (const [name, state] of Object.entries(states)) setNodeState(name, state);
  }
}

initMermaid(resolvedTheme());  // first init so renderGraph() works on first call
document.documentElement.setAttribute("data-theme", resolvedTheme());

// ----- Static graph topology (matches src/graph.py) ---------------------------------
// Used to "pre-light" the next nodes as soon as a predecessor finishes, since LangGraph's
// updates stream only fires on node *completion*. The loop edge (critic → remediation)
// naturally re-activates remediation if the critic emits a second event.

// Mirror src/state.py's rag_surface_threshold(). Tweak here if you tune the env var.
const RAG_SURFACE_THRESHOLD = 0.4;

const TOPOLOGY = {
  parse: ["classify"],
  classify: ["score_severity"],
  score_severity: ["rag"],
  rag: ["remediation"],
  remediation: ["critic"],
  critic: ["fanout", "remediation"],     // approved → fanout, rejected → remediation
  fanout: ["slack_notifier", "jira_creator", "skip_jira", "cookbook"],
  slack_notifier: ["aggregate"],
  jira_creator: ["aggregate"],
  skip_jira: ["aggregate"],
  cookbook: ["aggregate"],
  aggregate: [],
};

// ----- Per-node metadata for tooltips -------------------------------------------
// `desc` is static; `details(agg)` pulls live metrics from the current aggregate.

const NODE_INFO = {
  parse: {
    label: "Log parser",
    desc: "Heuristic parser. Splits raw logs into LogEvents, recognizing JSON, syslog, and free-form lines.",
    details: (a) => a.parsed_events_count != null ? `${a.parsed_events_count} events parsed` : null,
  },
  classify: {
    label: "Classifier agent",
    desc: "Claude tool-use call that groups events into discrete incidents and assigns each a category. Cascading errors collapse into the upstream incident.",
    details: (a) => {
      if (!a.incidents.length) return null;
      const cats = a.incidents.map(i => i.category).join(", ");
      return `${a.incidents.length} incident(s) · ${cats}`;
    },
  },
  score_severity: {
    label: "Severity agent",
    desc: "Scores each incident LOW/MEDIUM/HIGH/CRITICAL using a rubric (paging alerts, error rate %, blast radius). Drives the JIRA gate.",
    details: (a) => {
      if (!a.incidents.length) return null;
      return a.incidents.map(i => `${i.id}=${i.severity}`).join(" · ");
    },
  },
  rag: {
    label: "RAG retriever",
    desc: "Vector lookup against the seeded ChromaDB collection of past incidents. The remediation agent uses these matches as grounding context.",
    details: (a) => {
      if (!a.rag_matches.length) return null;
      const ids = a.rag_matches.map(m => m.past_id || "?").slice(0, 3).join(", ");
      return `${a.rag_matches.length} match(es) · ${ids}`;
    },
  },
  remediation: {
    label: "Remediation agent",
    desc: "RAG-grounded plan: root cause + ordered fix steps + rationale. Re-runs if the critic rejects.",
    details: (a) => {
      if (!a.remediations.length) return null;
      const r = a.remediations[0];
      const total = (a.remediation_history[r.incident_id] || []).length;
      const revInfo = total > 1 ? ` · ${total} revisions` : "";
      return `${a.remediations.length} plan(s) · ${r.steps?.length || 0} step(s)${revInfo}`;
    },
  },
  critic: {
    label: "Critic agent",
    desc: "Safety review with extended thinking. Rejects unsafe / wrong-cause / ill-ordered / unverifiable plans. Loop back to remediation if any reject.",
    details: (a) => {
      if (!a.critique.length) return null;
      const approved = a.critique.filter(c => c.approved).length;
      const total = a.critique.length;
      const hasThink = a.critique.some(c => c.thinking);
      return `${approved}/${total} approved${hasThink ? " · with reasoning" : ""}`;
    },
  },
  fanout: {
    label: "Fan-out",
    desc: "Branch point. Slack, JIRA, and Cookbook run in parallel from here.",
    details: () => null,
  },
  slack_notifier: {
    label: "Slack notifier",
    desc: "Formats each incident + remediation into a Slack message. Mocks if no SLACK_BOT_TOKEN.",
    details: (a) => {
      if (!a.slack_messages.length) return null;
      const live = a.slack_messages.filter(m => !m.dry_run).length;
      return `${a.slack_messages.length} message(s) · ${live ? "live" : "mock"}`;
    },
  },
  jira_creator: {
    label: "JIRA creator",
    desc: "Creates a real JIRA ticket only for HIGH/CRITICAL incidents. Mocks if no JIRA creds.",
    details: (a) => {
      if (!a.jira_tickets.length) return null;
      const failed = a.jira_tickets.filter(t => t.error).length;
      const live   = a.jira_tickets.filter(t => !t.dry_run && !t.error).length;
      const keys = a.jira_tickets.map(t => t.key).join(", ");
      const status = failed ? `${failed} failed` : (live ? "live" : "mock");
      return `${a.jira_tickets.length} ticket(s) · ${status} · ${keys}`;
    },
  },
  skip_jira: {
    label: "JIRA gate · skipped",
    desc: "Severity gate took the no-op branch — none of the incidents reached HIGH/CRITICAL.",
    details: () => null,
  },
  cookbook: {
    label: "Cookbook synthesizer",
    desc: "Distills (incident, remediation) pairs into reusable runbook entries. Generalizes the pattern, strips incident-specific identifiers.",
    details: (a) => {
      if (!a.cookbook.length) return null;
      return `${a.cookbook.length} runbook entry(ies)`;
    },
  },
  aggregate: {
    label: "Aggregate",
    desc: "Final fan-in. Combines results from all parallel branches and emits the run summary.",
    details: (a) => {
      const trace = a.trace.length;
      return trace ? `${trace} trace step(s)` : null;
    },
  },
};

const GRAPH_DEF = `flowchart TD
  parse([📁 Log parser]) --> classify[🤖 Classifier]
  classify --> score_severity[🤖 Severity]
  score_severity --> rag[🔍 RAG]
  rag --> remediation[🤖 Remediation]
  remediation --> critic[🤖 Critic]
  critic -- approved --> fanout{{Fan-out}}
  critic -- rejected --> remediation
  fanout --> slack_notifier[📣 Slack]
  fanout --> jira_creator[🎫 JIRA]
  fanout --> skip_jira[skip JIRA]
  fanout --> cookbook[📖 Cookbook]
  slack_notifier --> aggregate
  jira_creator --> aggregate
  skip_jira --> aggregate
  cookbook --> aggregate([✅ Aggregate])`;

// ----- DOM refs -------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const sampleSelect = $("#sample-select");
const fileInput = $("#file-input");
const uploadLabel = $("#upload-label");
const uploadLabelText = $("#upload-label-text");
const btnRun = $("#btn-run");
const btnTail = $("#btn-tail");
const tailStatus = $("#tail-status");
const tailStatusName = $("#tail-status-name");
const tailLastUpdated = $("#tail-last-updated");
const tailLastUpdatedTime = $("#tail-last-updated-time");
const strictToggle = $("#strict-toggle");
const logStream = $("#log-stream");
const logMeta = $("#log-meta");
const graphStatus = $("#graph-status");
const resultsMeta = $("#results-meta");
const mermaidHost = $("#mermaid-host");

// Per-tab body elements
const bodies = {
  incidents: $("#body-incidents"),
  slack: $("#body-slack"),
  jira: $("#body-jira"),
  cookbook: $("#body-cookbook"),
  trace: $("#body-trace"),
};

// ----- State ----------------------------------------------------------------------

let currentLog = "";          // raw log text
let currentFilename = null;
let nodeEls = {};             // node_name -> SVG element
let aggregate = freshAggregate();
let activeRun = null;         // EventSource

function freshAggregate() {
  return {
    parsed_events_count: null,
    incidents: [],
    rag_matches: [],
    remediations: [],
    remediation_history: {},  // incident_id -> [Remediation in revision order]
    critique: [],
    critique_history: {},     // incident_id -> [CritiqueResult in revision order]
    slack_messages: [],
    jira_tickets: [],
    cookbook: [],
    trace: [],
    errors: [],
    usage: {
      input: 0, output: 0,
      cache_read: 0, cache_create: 0,
      cost_usd: 0,
      records: [],   // per-agent breakdown
    },
  };
}

// ----- Init -----------------------------------------------------------------------

renderGraph().then(() => {
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab))
  );
  fileInput.addEventListener("change", onFile);
  sampleSelect.addEventListener("change", onSample);
  btnRun.addEventListener("click", () => kickoff(false));
  btnTail.addEventListener("click", toggleTailMode);
  uploadLabel.addEventListener("click", onUploadLabelClick);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const next = (document.documentElement.getAttribute("data-theme") === "dark") ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
  document.getElementById("alert-dismiss").addEventListener("click", hideAlert);
  document.getElementById("alert-swap-key").addEventListener("click", () => {
    const form = document.getElementById("alert-form");
    form.hidden = false;
    document.getElementById("alert-key-input").focus();
  });
  document.getElementById("alert-form").addEventListener("submit", onSwapKeySubmit);
  initChat();
  loadEnv();
  loadSamples();
});

// ----- Assistant chat drawer -------------------------------------------------------

const chatDrawer       = document.getElementById("chat-drawer");
const chatToggleBtn    = document.getElementById("chat-toggle");
const chatCloseBtn     = document.getElementById("chat-close");
const chatThread       = document.getElementById("chat-thread");
const chatInput        = document.getElementById("chat-input");
const chatSendBtn      = document.getElementById("chat-send");
const chatComposer     = document.getElementById("chat-composer");
const chatSuggestions  = document.getElementById("chat-suggestions");

let chatHistory = [];     // [{role: "user"|"assistant", content: "..."}]
let chatStreaming = false;

function initChat() {
  chatToggleBtn.addEventListener("click", () => toggleChat(true));
  chatCloseBtn.addEventListener("click",  () => toggleChat(false));
  chatComposer.addEventListener("submit", onChatSubmit);
  chatInput.addEventListener("input", () => {
    chatSendBtn.disabled = chatStreaming || !chatInput.value.trim();
  });
  chatInput.addEventListener("keydown", (ev) => {
    // Cmd/Ctrl+Enter sends; plain Enter inserts a newline.
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      onChatSubmit(ev);
    }
  });
  for (const chip of chatSuggestions.querySelectorAll(".chat-chip")) {
    chip.addEventListener("click", () => {
      chatInput.value = chip.dataset.prompt;
      chatSendBtn.disabled = chatStreaming;
      onChatSubmit(new Event("submit"));
    });
  }
}

function toggleChat(open) {
  chatDrawer.classList.toggle("chat-open", open);
  if (open) {
    chatDrawer.removeAttribute("inert");
    setTimeout(() => chatInput.focus(), 200);
  } else {
    // Move focus out of the drawer BEFORE marking it inert — otherwise the browser
    // (correctly) complains about hiding an element that contains the focused node.
    chatToggleBtn.focus();
    chatDrawer.setAttribute("inert", "");
  }
}

function appendChatMsg(role, content, { variant = null, streaming = false } = {}) {
  const el = document.createElement("div");
  let cls = `chat-msg chat-msg-${role}`;
  if (variant) cls += ` chat-msg-${variant}`;
  if (streaming) cls += " chat-msg-streaming";
  el.className = cls;
  el.textContent = content;
  chatThread.appendChild(el);
  chatThread.scrollTop = chatThread.scrollHeight;
  return el;
}

async function onChatSubmit(ev) {
  ev.preventDefault();
  if (chatStreaming) return;
  const text = chatInput.value.trim();
  if (!text) return;

  chatStreaming = true;
  chatSendBtn.disabled = true;
  chatInput.value = "";

  // Append user message + push to history
  appendChatMsg("user", text);
  chatHistory.push({ role: "user", content: text });

  // Cap history to keep prompt size bounded (last 10 messages)
  const trimmed = chatHistory.slice(-10);

  // Bubble for the streaming assistant reply
  const replyEl = appendChatMsg("assistant", "", { streaming: true });

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: trimmed, state: snapshotStateForChat() }),
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`HTTP ${resp.status}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let assistantText = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Process complete SSE frames (separated by blank lines)
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const { event, data } = parseSseFrame(frame);
        if (!data) continue;
        try {
          const payload = JSON.parse(data);
          if (event === "usage") {
            // Pipe through the existing cost meter
            onUsage(payload);
          } else if (event === "error") {
            throw new Error(payload.error || "stream error");
          } else if (event === "done") {
            // handled at end
          } else if (payload.delta) {
            assistantText += payload.delta;
            replyEl.textContent = assistantText;
            chatThread.scrollTop = chatThread.scrollHeight;
          }
        } catch (e) {
          // Malformed frame — skip
        }
      }
    }

    if (!assistantText) {
      replyEl.textContent = "(empty response)";
      replyEl.classList.add("chat-msg-error");
    } else {
      chatHistory.push({ role: "assistant", content: assistantText });
    }
    replyEl.classList.remove("chat-msg-streaming");
  } catch (e) {
    replyEl.textContent = `Error: ${e.message}`;
    replyEl.classList.add("chat-msg-error");
    replyEl.classList.remove("chat-msg-streaming");
  } finally {
    chatStreaming = false;
    chatSendBtn.disabled = !chatInput.value.trim();
    chatInput.focus();
  }
}

function parseSseFrame(frame) {
  let event = "message", data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) data += (data ? "\n" : "") + line.slice(6);
  }
  return { event, data };
}

function snapshotStateForChat() {
  // Trim the aggregate to the fields the assistant cares about. Avoid shipping
  // the per-event log preview blob — it's bulky and rarely referenced.
  return {
    incidents:           aggregate.incidents,
    rag_matches:         aggregate.rag_matches,
    remediations:        aggregate.remediations,
    remediation_history: aggregate.remediation_history,
    critique:            aggregate.critique,
    critique_history:    aggregate.critique_history,
    slack_messages:      aggregate.slack_messages,
    jira_tickets:        aggregate.jira_tickets,
    cookbook:            aggregate.cookbook,
    trace:               aggregate.trace,
    errors:              aggregate.errors,
    usage:               { records: aggregate.usage.records, total_cost_usd: aggregate.usage.cost_usd },
  };
}

async function renderGraph() {
  const { svg } = await mermaid.render("pipeline-graph", GRAPH_DEF);
  mermaidHost.innerHTML = svg;
  // Index node elements by their LangGraph node name.
  // Mermaid 11 IDs look like:  pipeline-graph-flowchart-<nodeName>-<idx>
  // We match the trailing `flowchart-<nodeName>-<idx>` substring regardless of prefix.
  nodeEls = {};
  mermaidHost.querySelectorAll("g.node").forEach((el) => {
    const m = (el.id || "").match(/flowchart-([A-Za-z_]+)-\d+$/);
    if (m) nodeEls[m[1]] = el;
  });
  attachNodeTooltips();
  console.info("[pipeline] indexed nodes:", Object.keys(nodeEls));
}

// ----- Node tooltips ------------------------------------------------------------

let _tooltipEl = null;

function tooltipEl() {
  if (_tooltipEl) return _tooltipEl;
  _tooltipEl = document.createElement("div");
  _tooltipEl.className = "node-tooltip";
  _tooltipEl.style.display = "none";
  document.body.appendChild(_tooltipEl);
  return _tooltipEl;
}

function nodeStateLabel(el) {
  if (el.classList.contains("node-active")) return { label: "running", cls: "tip-state-active" };
  if (el.classList.contains("node-done"))   return { label: "done",    cls: "tip-state-done" };
  if (el.classList.contains("node-error"))  return { label: "error",   cls: "tip-state-error" };
  return { label: "idle", cls: "tip-state-idle" };
}

function findUsageFor(name) {
  // Map graph node names to the agent labels recorded in usage records.
  const agentName = ({
    classify: "classifier",
    score_severity: "severity",
    remediation: "remediation",
    critic: "critic",
    cookbook: "cookbook",
  })[name];
  if (!agentName) return null;
  // Use the latest record for this agent (handles loop-back retries).
  const recs = aggregate.usage.records.filter(r => r.agent === agentName);
  return recs.length ? recs[recs.length - 1] : null;
}

function buildTooltipHTML(name, el) {
  const info = NODE_INFO[name];
  if (!info) return "";
  const { label, cls } = nodeStateLabel(el);
  const details = info.details ? info.details(aggregate) : null;
  const usage = findUsageFor(name);

  let html = `
    <div class="tip-head">
      <span class="tip-label">${escape(info.label)}</span>
      <span class="tip-state ${cls}">${label}</span>
    </div>
    <p class="tip-desc">${escape(info.desc)}</p>`;
  if (details) {
    html += `<div class="tip-row tip-details">${escape(details)}</div>`;
  }
  if (usage) {
    const tokens = `in ${fmt(usage.input_tokens + usage.cache_creation_tokens)}` +
      (usage.cache_read_tokens ? ` · cache↓ ${fmt(usage.cache_read_tokens)}` : "") +
      ` · out ${fmt(usage.output_tokens)}`;
    html += `<div class="tip-row tip-usage">
      <span class="tip-usage-cost">$${usage.cost_usd.toFixed(4)}</span>
      <span class="tip-usage-tokens">${tokens}</span>
    </div>`;
  }
  return html;
}

function positionTooltip(ev) {
  const tip = tooltipEl();
  const margin = 14;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = ev.clientX + margin;
  let y = ev.clientY + margin;
  if (x + tw > window.innerWidth - 8)  x = ev.clientX - tw - margin;
  if (y + th > window.innerHeight - 8) y = ev.clientY - th - margin;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top  = `${Math.max(8, y)}px`;
}

function attachNodeTooltips() {
  const tip = tooltipEl();
  for (const [name, el] of Object.entries(nodeEls)) {
    el.style.cursor = "help";
    el.addEventListener("mouseenter", (ev) => {
      tip.innerHTML = buildTooltipHTML(name, el);
      tip.style.display = "block";
      positionTooltip(ev);
    });
    el.addEventListener("mousemove", positionTooltip);
    el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  }
}

async function loadEnv() {
  const r = await fetch("/api/env").then((r) => r.json());
  // Provider pill: shows OpenRouter (preferred when set) or Anthropic direct, with the model id.
  let providerLabel, providerKind;
  if (r.provider === "openrouter") {
    providerLabel = `OpenRouter · ${r.model}`;
    providerKind  = "live";
  } else if (r.anthropic) {
    providerLabel = `Anthropic · ${r.model}`;
    providerKind  = "live";
  } else {
    providerLabel = "no LLM key set";
    providerKind  = "missing";
  }
  setPill("pill-anthropic", providerKind, providerLabel);
  setPill("pill-slack", r.slack ? "live" : "mock", r.slack ? "Slack ✓ live" : "Slack · mock");
  setPill("pill-jira", r.jira ? "live" : "mock", r.jira ? "JIRA ✓ live" : "JIRA · mock");
}

async function loadSamples() {
  const r = await fetch("/api/samples").then((r) => r.json());
  for (const name of r) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sampleSelect.appendChild(opt);
  }
}

// ----- Input handlers -------------------------------------------------------------

async function onSample() {
  const name = sampleSelect.value;
  if (!name) return;
  const r = await fetch(`/api/sample/${encodeURIComponent(name)}`).then((r) => r.json());
  setLog(r.content, r.name);
}

function onFile(ev) {
  const f = ev.target.files?.[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => setLog(String(reader.result), f.name);
  reader.readAsText(f);
}

function setLog(content, name) {
  currentLog = content;
  currentFilename = name;
  logStream.textContent = content;
  logStream.scrollTop = logStream.scrollHeight;  // scroll to bottom (newest)
  logMeta.textContent = `${name} · ${content.length.toLocaleString()} bytes`;
  btnRun.disabled = false;
}

// ----- Tail mode (live file watching) ---------------------------------------------

let tailModeOn = false;
let tailFileHandle = null;        // FileSystemFileHandle when supported
let tailIntervalId = null;
const TAIL_REFRESH_MS = 60_000;   // re-read the file once a minute

function toggleTailMode() {
  tailModeOn = !tailModeOn;
  btnTail.setAttribute("aria-pressed", String(tailModeOn));
  uploadLabelText.textContent = tailModeOn ? "📌 Point at a file to tail…" : "or upload a file…";

  if (!tailModeOn) {
    stopTailing();
  } else if (tailFileHandle) {
    // Already had a handle from a previous toggle — restart polling.
    startTailInterval();
    showTailStatus(tailFileHandle.name);
  }
}

async function onUploadLabelClick(ev) {
  // Only intercept when tail mode is on AND the browser supports the File System Access API.
  // Otherwise fall through to the regular <input type="file"> behavior.
  if (!tailModeOn) return;
  if (!window.showOpenFilePicker) {
    // Safari / Firefox: no persistent handle. Let regular upload happen but warn the user once.
    if (!onUploadLabelClick._warned) {
      console.warn("[tail] Browser doesn't support File System Access API — falling back to one-shot upload, no auto-refresh.");
      onUploadLabelClick._warned = true;
    }
    return;
  }

  ev.preventDefault();  // suppress the file input dialog; we'll open our own
  try {
    const [handle] = await window.showOpenFilePicker({
      multiple: false,
      types: [{
        description: "Log files",
        accept: { "text/plain": [".log", ".txt", ".json"] },
      }],
    });
    tailFileHandle = handle;
    await refreshTailFile();
    startTailInterval();
    showTailStatus(handle.name);
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error("[tail] picker failed:", err);
    }
  }
}

async function refreshTailFile() {
  if (!tailFileHandle) return;
  try {
    const file = await tailFileHandle.getFile();
    const content = await file.text();
    setLog(content, file.name);
    updateLastUpdatedTimestamp();
  } catch (err) {
    console.error("[tail] refresh failed (file moved/deleted?):", err);
    stopTailing();
  }
}

function startTailInterval() {
  if (tailIntervalId) clearInterval(tailIntervalId);
  tailIntervalId = setInterval(refreshTailFile, TAIL_REFRESH_MS);
}

function stopTailing() {
  if (tailIntervalId) { clearInterval(tailIntervalId); tailIntervalId = null; }
  hideTailStatus();
}

function showTailStatus(name) {
  tailStatusName.textContent = name;
  tailStatus.hidden = false;
  tailLastUpdated.hidden = false;
  updateLastUpdatedTimestamp();
}

function hideTailStatus() {
  tailStatus.hidden = true;
  tailLastUpdated.hidden = true;
}

function updateLastUpdatedTimestamp() {
  tailLastUpdatedTime.textContent = new Date().toLocaleTimeString();
}

// ----- Run kickoff ----------------------------------------------------------------

async function kickoff(tailMode) {
  if (activeRun) {
    activeRun.close();
    activeRun = null;
  }
  resetUI();
  graphStatus.textContent = tailMode ? "tailing log…" : "running…";

  const strict = !!strictToggle?.checked;

  let runId;
  if (tailMode) {
    // Prefer sample_name when the loaded file is a sample (avoids re-uploading),
    // otherwise ship the raw_logs content (works for uploaded files).
    const isSample = sampleSelect.value && sampleSelect.value === currentFilename;
    const body = isSample
      ? { sample_name: sampleSelect.value, strict_critic: strict }
      : { raw_logs: currentLog, filename: currentFilename, strict_critic: strict };
    const r = await fetch("/api/tail", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());
    runId = r.run_id;
    // Clear the log panel — tail mode will populate it line by line.
    logStream.textContent = "";
  } else {
    const r = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw_logs: currentLog, filename: currentFilename, strict_critic: strict }),
    }).then((r) => r.json());
    runId = r.run_id;
  }

  // Pre-light parse so the user sees something move immediately.
  setNodeState("parse", "active");

  activeRun = new EventSource(`/api/stream/${runId}`);
  activeRun.addEventListener("run_started", (e) => onRunStarted(JSON.parse(e.data)));
  activeRun.addEventListener("log_line", (e) => onLogLine(JSON.parse(e.data)));
  activeRun.addEventListener("node_completed", (e) => onNodeCompleted(JSON.parse(e.data)));
  activeRun.addEventListener("usage", (e) => onUsage(JSON.parse(e.data)));
  activeRun.addEventListener("done", () => onDone());
  activeRun.addEventListener("error", (e) => onError(e));
}

function resetUI() {
  aggregate = freshAggregate();
  for (const el of Object.values(nodeEls)) {
    el.classList.remove("node-active", "node-done", "node-error");
  }
  for (const k of Object.keys(bodies)) {
    bodies[k].innerHTML = `<p class="empty">awaiting…</p>`;
  }
  resultsMeta.textContent = "running…";
  renderCostMeter();
  hideAlert();
  setBrandStatus("running", "analyzing");
}

// Brand status pill in the topbar — reflects the live run state so the
// previously-static "multi-agent · live" text actually means something.
let _brandStatusTimer = null;
function setBrandStatus(state, text) {
  const pill = document.getElementById("brand-status");
  if (!pill) return;
  pill.dataset.state = state;
  pill.querySelector(".brand-status-text").textContent = text;
  if (_brandStatusTimer) { clearTimeout(_brandStatusTimer); _brandStatusTimer = null; }
}
function setBrandStatusFading(state, text, ms) {
  setBrandStatus(state, text);
  _brandStatusTimer = setTimeout(() => setBrandStatus("idle", "idle"), ms);
}

// ----- SSE handlers ---------------------------------------------------------------

function onRunStarted(d) {
  graphStatus.textContent = d.mode === "tail" ? "tailing log…" : "running…";
}

function onLogLine(d) {
  logStream.textContent += d.line + "\n";
  logStream.scrollTop = logStream.scrollHeight;
}

function onNodeCompleted({ node, delta }) {
  const errored = (delta?.errors || []).length > 0;
  setNodeState(node, errored ? "error" : "done");

  // Merge first so the gate-prediction logic below sees the latest state
  // (in particular, severities written by score_severity).
  mergeDelta(delta);

  // Surface critical LLM-provider errors as a top banner the moment they happen,
  // so the user knows immediately why agents are falling back to placeholders.
  maybeShowLLMAlert();

  // Pre-light successors so they appear active until their own completion.
  // For conditional edges we predict which branch will fire so we don't leave
  // the unused branch stuck pulsing forever.
  const hasHighSev = aggregate.incidents.some(
    (i) => i.severity === "HIGH" || i.severity === "CRITICAL"
  );
  const anyRejected = (aggregate.critique || []).some((c) => !c.approved);

  // The retry budget mirrors src/graph.py route_after_critic: the loop only
  // fires if (a) any rejection AND (b) we haven't already used our one retry.
  // Detect "already retried" from remediation_history — any incident with >1
  // revision means remediation has already re-run once. Without this check,
  // the loop edge gets pre-lit on the second critic too and remediation
  // stays stuck pulsing while the rest of the graph turns green.
  const loopAlreadyFired = Object.values(aggregate.remediation_history || {})
    .some((list) => list.length > 1);
  const willLoop = anyRejected && !loopAlreadyFired;

  for (const next of TOPOLOGY[node] || []) {
    // critic → remediation: only when the loop will actually fire.
    if (node === "critic" && next === "remediation" && !willLoop) continue;
    // critic → fanout: skip only when the loop will fire (graph routes to remediation).
    if (node === "critic" && next === "fanout" && willLoop) continue;
    // fanout → jira_creator vs skip_jira: severity gate.
    if (node === "fanout" && next === "jira_creator" && !hasHighSev) continue;
    if (node === "fanout" && next === "skip_jira"   && hasHighSev) continue;

    setNodeState(next, "active");
  }

  renderResults();
}

function onDone() {
  graphStatus.textContent = "complete";
  resultsMeta.textContent = `${aggregate.incidents.length} incident(s) · ${aggregate.slack_messages.length} slack · ${aggregate.jira_tickets.length} jira · ${aggregate.cookbook.length} runbook`;
  if (activeRun) activeRun.close();
  activeRun = null;
  // Show "complete" briefly, then settle back to idle.
  setBrandStatusFading("complete", "complete", 4000);
}

function onError(e) {
  graphStatus.textContent = "error";
  console.error("SSE error", e);
  if (activeRun) activeRun.close();
  activeRun = null;
  setBrandStatusFading("error", "error", 6000);
}

// ----- Alert banner (LLM provider errors) -----------------------------------------
// Pattern-matches the strings currently in aggregate.errors against known classes.
// Show as soon as a known issue is detected; auto-clears on next run start (resetUI).

const _alertEls = {
  banner:  () => document.getElementById("alert-banner"),
  icon:    () => document.getElementById("alert-icon"),
  headline:() => document.getElementById("alert-headline"),
  detail:  () => document.getElementById("alert-detail"),
  action:  () => document.getElementById("alert-action"),
};
let _alertShown = false;   // once shown for a run, don't re-show every tick

function detectLLMAlert(errors) {
  if (!errors || !errors.length) return null;
  const text = errors.join(" \n ");
  const lower = text.toLowerCase();

  // 1. OpenRouter / Anthropic credit or spending limit reached
  if (/key limit exceeded|insufficient.*credit|insufficient.*balance|quota.*exceeded|billing/.test(lower)) {
    const isOpenRouter = /openrouter/.test(lower);
    return {
      level: "billing",
      icon: "💳",
      headline: isOpenRouter
        ? "OpenRouter key has hit its credit limit"
        : "LLM provider credit limit reached",
      detail: "All affected agents fell back to placeholders. Top up your account to continue.",
      actionUrl: isOpenRouter
        ? "https://openrouter.ai/settings/keys"
        : "https://console.anthropic.com/settings/billing",
      actionText: isOpenRouter ? "Manage credits ↗" : "Anthropic billing ↗",
    };
  }

  // 2. Auth failure (invalid / missing key)
  if (/401|unauthorized|invalid.*api.*key|authentication.*method|x-api-key/i.test(text)) {
    return {
      level: "auth",
      icon: "🔒",
      headline: "LLM provider rejected the API key",
      detail: "Authentication failed. Check that ANTHROPIC_API_KEY or OPENROUTER_API_KEY is set correctly.",
      actionUrl: null,
    };
  }

  // 3. Rate limit
  if (/429|rate.?limit|too many requests/i.test(text)) {
    return {
      level: "rate-limit",
      icon: "🐢",
      headline: "Rate limited by LLM provider",
      detail: "Slow down requests or upgrade your plan to increase the per-minute cap.",
      actionUrl: null,
    };
  }

  // 4. Generic — multiple LLM agent failures of unknown cause
  const llmAgentErrors = errors.filter((e) =>
    /^(classifier|severity|remediation|critic|cookbook):/i.test(e)
  );
  if (llmAgentErrors.length >= 2) {
    const first = llmAgentErrors[0].length > 220
      ? llmAgentErrors[0].slice(0, 220) + "…"
      : llmAgentErrors[0];
    return {
      level: "generic",
      icon: "⚠️",
      headline: `${llmAgentErrors.length} LLM agents failed`,
      detail: first,
      actionUrl: null,
    };
  }

  return null;
}

function maybeShowLLMAlert() {
  if (_alertShown) return;
  const alert = detectLLMAlert(aggregate.errors);
  if (alert) showAlert(alert);
}

function showAlert(alert) {
  _alertEls.banner().dataset.level    = alert.level;
  _alertEls.icon().textContent        = alert.icon || "⚠️";
  _alertEls.headline().textContent    = alert.headline;
  _alertEls.detail().textContent      = alert.detail;
  const action = _alertEls.action();
  if (alert.actionUrl) {
    action.href = alert.actionUrl;
    action.textContent = alert.actionText || "Open ↗";
    action.hidden = false;
  } else {
    action.hidden = true;
  }
  // Show the "Use different key" button for billing + auth alerts (where a fresh
  // key would resolve the issue). Keep the form collapsed until clicked.
  const swapBtn = document.getElementById("alert-swap-key");
  swapBtn.hidden = !(alert.level === "billing" || alert.level === "auth");
  document.getElementById("alert-form").hidden = true;
  document.getElementById("alert-form-msg").textContent = "";
  document.getElementById("alert-key-input").value = "";

  _alertEls.banner().hidden = false;
  _alertShown = true;
}

function hideAlert() {
  _alertEls.banner().hidden = true;
  _alertShown = false;
}

async function onSwapKeySubmit(ev) {
  ev.preventDefault();
  const input = document.getElementById("alert-key-input");
  const msg   = document.getElementById("alert-form-msg");
  const key = input.value.trim();
  if (!key) {
    msg.textContent = "key required";
    msg.className = "alert-form-msg alert-form-msg-err";
    return;
  }
  msg.textContent = "applying…";
  msg.className = "alert-form-msg";

  try {
    const r = await fetch("/api/llm-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: "openrouter", key }),
    });
    if (!r.ok) {
      const body = await r.text();
      throw new Error(`HTTP ${r.status} — ${body.slice(0, 120)}`);
    }
    const out = await r.json();
    msg.textContent = `✓ ${out.provider} · ${out.model} — try the run again`;
    msg.className = "alert-form-msg alert-form-msg-ok";
    input.value = "";
    loadEnv();   // refresh topbar pill
    setTimeout(hideAlert, 2500);
  } catch (e) {
    msg.textContent = `failed: ${e.message}`;
    msg.className = "alert-form-msg alert-form-msg-err";
  }
}

function onUsage(rec) {
  const u = aggregate.usage;
  u.input        += rec.input_tokens || 0;
  u.output       += rec.output_tokens || 0;
  u.cache_read   += rec.cache_read_tokens || 0;
  u.cache_create += rec.cache_creation_tokens || 0;
  u.cost_usd     += rec.cost_usd || 0;
  u.records.push(rec);
  renderCostMeter();
}

function renderCostMeter() {
  const u = aggregate.usage;
  const cachedIn = u.cache_read;
  const totalIn  = u.input + u.cache_read + u.cache_create;
  const hitPct   = totalIn > 0 ? Math.round((cachedIn / totalIn) * 100) : null;

  document.getElementById("cost-usd").textContent = `$${u.cost_usd.toFixed(4)}`;
  document.getElementById("cost-in").textContent  = fmt(u.input + u.cache_create);
  document.getElementById("cost-out").textContent = fmt(u.output);
  document.getElementById("cost-cache-read").textContent = fmt(u.cache_read);
  document.getElementById("cost-hit").textContent = hitPct === null ? "—" : `${hitPct}%`;

  // Brief flash to draw the eye
  const meter = document.getElementById("cost-meter");
  meter.classList.add("cost-flash");
  setTimeout(() => meter.classList.remove("cost-flash"), 250);
}

function fmt(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

// ----- State management -----------------------------------------------------------

function setNodeState(name, state) {
  const el = nodeEls[name];
  if (!el) {
    console.warn("[pipeline] no SVG node for", name, "— known:", Object.keys(nodeEls));
    return;
  }
  el.classList.remove("node-active", "node-done", "node-error");
  if (state === "active") el.classList.add("node-active");
  else if (state === "done") el.classList.add("node-done");
  else if (state === "error") el.classList.add("node-error");
  console.info("[pipeline]", name, "→", state);
}

function mergeDelta(delta) {
  if (!delta) return;
  // Lists: replace if present (LangGraph fields are last-write-wins except trace/errors which use add reducers).
  for (const k of ["incidents", "rag_matches", "slack_messages", "jira_tickets", "cookbook"]) {
    if (Array.isArray(delta[k])) aggregate[k] = delta[k];
  }
  if (delta.parsed_events && typeof delta.parsed_events.count === "number") {
    aggregate.parsed_events_count = delta.parsed_events.count;
  }
  // Remediations: also record per-revision history so the UI can show the diff
  // when the critic loop fires.
  if (Array.isArray(delta.remediations)) {
    aggregate.remediations = delta.remediations;
    for (const r of delta.remediations) {
      const list = (aggregate.remediation_history[r.incident_id] ||= []);
      const last = list[list.length - 1];
      if (!last || last.revision !== r.revision) list.push(r);
    }
  }
  // Critique: snapshot per-revision so we can pair "rejected" verdicts with the rev that triggered them.
  if (Array.isArray(delta.critique)) {
    aggregate.critique = delta.critique;
    for (const c of delta.critique) {
      const list = (aggregate.critique_history[c.incident_id] ||= []);
      // Each critique pass appends one entry; we don't have a revision number on critiques
      // so we just push every time the critic runs.
      list.push(c);
    }
  }
  // trace and errors accumulate
  if (Array.isArray(delta.trace)) aggregate.trace.push(...delta.trace);
  if (Array.isArray(delta.errors)) aggregate.errors.push(...delta.errors);
}

// ----- Results rendering ----------------------------------------------------------

function renderResults() {
  renderIncidents();
  renderSlack();
  renderJira();
  renderCookbook();
  renderTrace();
  resultsMeta.textContent = `${aggregate.incidents.length} incident(s) so far · trace ${aggregate.trace.length} step(s)`;
}

function renderIncidents() {
  const body = bodies.incidents;
  if (!aggregate.incidents.length) {
    body.innerHTML = `<p class="empty">No incidents yet.</p>`;
    return;
  }
  const ragByInc = aggregate.rag_matches.reduce((acc, m) => {
    (acc[m.incident_id] ||= []).push(m);
    return acc;
  }, {});
  const finalCrit = Object.fromEntries(aggregate.critique.map((c) => [c.incident_id, c]));

  body.innerHTML = aggregate.incidents.map((inc) => {
    const remHistory = aggregate.remediation_history[inc.id] || [];
    const critHistory = aggregate.critique_history[inc.id] || [];
    const finalRem = remHistory[remHistory.length - 1];
    const finalC = finalCrit[inc.id];
    const rag = ragByInc[inc.id] || [];

    const critBadge = finalC
      ? `<span class="badge ${finalC.approved ? "badge-ok" : "badge-warn"}">critic: ${finalC.approved ? "approved" : "rejected"}</span>`
      : "";
    const loopBadge = remHistory.length > 1
      ? `<span class="badge badge-loop">loop fired · ${remHistory.length} revisions</span>`
      : "";

    return `
      <article class="card">
        <header class="card-head">
          <span class="sev sev-${inc.severity}">${inc.severity}</span>
          <span class="cat">${inc.category}</span>
          ${critBadge}
          ${loopBadge}
        </header>
        <h3>${escape(inc.title)}</h3>
        <p>${escape(inc.summary)}</p>
        ${inc.affected_components?.length ? `<p class="meta"><strong>Affected:</strong> ${inc.affected_components.map(escape).join(", ")}</p>` : ""}
        ${rag.length ? `<p class="meta"><strong>RAG matches:</strong> ${rag.map((m) => `${escape(m.past_id || "?")} (${(m.similarity || 0).toFixed(2)})`).join(" · ")}</p>` : ""}
        ${renderRemediationBlock(remHistory, critHistory)}
        ${renderPastReferenceBlock(rag)}
        ${finalC && !finalC.approved ? renderCritiqueBlock(finalC) : ""}
      </article>
    `;
  }).join("");
}

function renderPastReferenceBlock(matches) {
  const strong = (matches || [])
    .filter(m => (m.similarity || 0) >= RAG_SURFACE_THRESHOLD)
    .sort((a, b) => (b.similarity || 0) - (a.similarity || 0))
    .slice(0, 2);
  if (!strong.length) return "";
  return `
    <div class="past-ref">
      <h4 class="past-ref-head">Reference: prior fix that worked</h4>
      ${strong.map(m => `
        <div class="past-ref-item">
          <header>
            <span class="past-ref-id">${escape(m.past_id || "?")}</span>
            <span class="past-ref-sim">similarity ${(m.similarity || 0).toFixed(2)}</span>
          </header>
          <p class="past-ref-title">${escape(m.past_title || "")}</p>
          <p class="past-ref-rem"><strong>Past remediation:</strong> ${escape(m.past_remediation || "")}</p>
        </div>
      `).join("")}
    </div>`;
}

function renderRemediationBlock(remHistory, critHistory) {
  if (!remHistory.length) return "";
  const final = remHistory[remHistory.length - 1];

  // Common case: no loop. Just render the final remediation.
  if (remHistory.length === 1) {
    const finalCrit = critHistory[critHistory.length - 1];
    return `
      <details open>
        <summary>Remediation (rev ${final.revision || 1})</summary>
        ${renderRemediationBody(final)}
      </details>
      ${renderThinkingBlock(finalCrit?.thinking)}`;
  }

  // Loop case: render a side-by-side diff between rev 1 and rev N (latest).
  const prior = remHistory[0];
  const priorCrit = critHistory[0];   // verdict on prior that triggered the loop
  const priorSteps = new Set((prior.steps || []).map(s => s.trim()));
  const finalSteps = new Set((final.steps || []).map(s => s.trim()));

  return `
    <div class="diff">
      <h4 class="diff-head">Self-critique loop fired — revision diff</h4>
      <div class="diff-grid">
        <section class="diff-col diff-prior">
          <header>rev ${prior.revision} <span class="badge badge-warn">rejected</span></header>
          <p class="meta"><strong>Root cause:</strong> ${escape(prior.root_cause || "")}</p>
          <ol>${(prior.steps || []).map(s => {
            const survived = finalSteps.has(s.trim());
            return `<li class="${survived ? "" : "diff-removed"}">${escape(s)}</li>`;
          }).join("")}</ol>
          ${priorCrit && !priorCrit.approved ? `
            <div class="critique critique-inline">
              <strong>Critic said:</strong>
              <ul>${(priorCrit.issues || []).map(i => `<li>${escape(i)}</li>`).join("")}</ul>
              ${priorCrit.suggestion ? `<p><em>${escape(priorCrit.suggestion)}</em></p>` : ""}
            </div>` : ""}
          ${renderThinkingBlock(priorCrit?.thinking, "compact")}
        </section>
        <section class="diff-col diff-final">
          <header>rev ${final.revision} <span class="badge badge-ok">approved</span></header>
          <p class="meta"><strong>Root cause:</strong> ${escape(final.root_cause || "")}</p>
          <ol>${(final.steps || []).map(s => {
            const isNew = !priorSteps.has(s.trim());
            return `<li class="${isNew ? "diff-added" : ""}">${escape(s)}</li>`;
          }).join("")}</ol>
          <p class="meta"><strong>Rationale:</strong> ${escape(final.rationale || "")}</p>
        </section>
      </div>
    </div>`;
}

function renderThinkingBlock(text, variant = "default") {
  if (!text) return "";
  const cls = variant === "compact" ? "thinking thinking-compact" : "thinking";
  return `
    <details class="${cls}">
      <summary><span class="thinking-icon">🧠</span> critic's reasoning <span class="thinking-meta">extended thinking</span></summary>
      <div class="thinking-body">${escape(text)}</div>
    </details>`;
}

function renderRemediationBody(rem) {
  return `
    <p class="meta"><strong>Root cause:</strong> ${escape(rem.root_cause || "")}</p>
    <ol>${(rem.steps || []).map((s) => `<li>${escape(s)}</li>`).join("")}</ol>
    <p class="meta"><strong>Rationale:</strong> ${escape(rem.rationale || "")}</p>
    ${rem.references?.length ? `<p class="meta"><strong>References:</strong> ${rem.references.map(escape).join(", ")}</p>` : ""}`;
}

function renderCritiqueBlock(crit) {
  return `
    <div class="critique">
      <strong>Critic issues:</strong>
      <ul>${(crit.issues || []).map((i) => `<li>${escape(i)}</li>`).join("")}</ul>
      ${crit.suggestion ? `<p><strong>Suggestion:</strong> ${escape(crit.suggestion)}</p>` : ""}
    </div>`;
}

function renderSlack() {
  const body = bodies.slack;
  if (!aggregate.slack_messages.length) {
    body.innerHTML = `<p class="empty">No Slack messages yet.</p>`;
    return;
  }
  body.innerHTML = aggregate.slack_messages.map((m) => `
    <article class="card">
      <header class="card-head">
        <span class="badge ${m.dry_run ? "badge-mock" : "badge-ok"}">${m.dry_run ? "mock" : "live"}</span>
        <span class="meta">${escape(m.channel)} · incident ${escape(m.incident_id)}</span>
      </header>
      <pre class="codeblock">${escape(m.text)}</pre>
    </article>
  `).join("");
}

function renderJira() {
  const body = bodies.jira;
  if (!aggregate.jira_tickets.length) {
    body.innerHTML = `<p class="empty">No JIRA tickets (gate: HIGH/CRITICAL only).</p>`;
    return;
  }
  body.innerHTML = aggregate.jira_tickets.map((t) => {
    let statusBadge;
    if (t.error)        statusBadge = `<span class="badge badge-warn">failed</span>`;
    else if (t.dry_run) statusBadge = `<span class="badge badge-mock">mock</span>`;
    else                statusBadge = `<span class="badge badge-ok">live</span>`;
    const keyDisplay = t.url
      ? `<a class="meta jira-link" href="${escape(t.url)}" target="_blank" rel="noopener">${escape(t.key)} ↗</a>`
      : `<span class="meta">${escape(t.key)}</span>`;
    return `
      <article class="card">
        <header class="card-head">
          ${statusBadge}
          <span class="badge">${escape(t.priority)}</span>
          ${keyDisplay}
        </header>
        <h3>${escape(t.summary)}</h3>
        ${t.error ? `<div class="critique"><strong>JIRA error:</strong><pre class="codeblock">${escape(t.error)}</pre></div>` : ""}
        <pre class="codeblock">${escape(t.description)}</pre>
      </article>`;
  }).join("");
}

function renderCookbook() {
  const body = bodies.cookbook;
  if (!aggregate.cookbook.length) {
    body.innerHTML = `<p class="empty">No runbook entries yet.</p>`;
    return;
  }
  body.innerHTML = aggregate.cookbook.map((e) => `
    <article class="card">
      <h3>${escape(e.title)}</h3>
      <p class="meta"><strong>When to use:</strong> ${escape(e.when_to_use)}</p>
      <ol>${(e.steps || []).map((s) => `<li>${escape(s)}</li>`).join("")}</ol>
    </article>
  `).join("");
}

function renderTrace() {
  const body = bodies.trace;
  if (!aggregate.trace.length) {
    body.innerHTML = `<p class="empty">No trace yet.</p>`;
    return;
  }
  body.innerHTML = `
    <ul class="trace">
      ${aggregate.trace.map((s) => `
        <li class="trace-item trace-${s.status}">
          <span class="trace-status">${s.status}</span>
          <span class="trace-agent">${escape(s.agent)}</span>
          <span class="trace-note">${escape(s.note || "")}</span>
        </li>
      `).join("")}
    </ul>
    ${aggregate.errors.length ? `<div class="errors"><strong>Errors:</strong><ul>${aggregate.errors.map((e) => `<li>${escape(e)}</li>`).join("")}</ul></div>` : ""}
  `;
}

// ----- Helpers --------------------------------------------------------------------

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("tab-active", t.dataset.tab === name));
  document.querySelectorAll(".tab-body").forEach((b) => b.classList.toggle("tab-active", b.dataset.tab === name));
}

function setPill(id, kind, text) {
  const el = document.getElementById(id);
  el.className = `pill pill-${kind}`;
  el.textContent = text;
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

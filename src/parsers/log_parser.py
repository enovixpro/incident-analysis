"""Heuristic log parser. Pre-LLM step that chunks raw text into LogEvents."""
from __future__ import annotations

import json
import re

from src.state import IncidentState, LogEvent, make_step, now_iso

_SYSLOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.\dZ:+-]*)\s+"
    r"(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\s+"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)


def parse_line(line: str) -> LogEvent | None:
    line = line.rstrip()
    if not line:
        return None

    if line.startswith("{") and line.endswith("}"):
        try:
            obj = json.loads(line)
            return LogEvent(
                timestamp=str(obj.get("timestamp") or obj.get("ts") or obj.get("time") or ""),
                level=str(obj.get("level") or obj.get("severity") or "INFO").upper(),
                source=obj.get("source") or obj.get("logger") or obj.get("component"),
                message=str(obj.get("message") or obj.get("msg") or json.dumps(obj)),
                raw=line,
            )
        except json.JSONDecodeError:
            pass

    m = _SYSLOG_RE.match(line)
    if m:
        rest = m.group("rest")
        source = None
        if ":" in rest[:60]:
            head, _, tail = rest.partition(":")
            if " " not in head:
                source = head.strip()
                rest = tail.strip()
        return LogEvent(
            timestamp=m.group("ts"),
            level=m.group("level").upper(),
            source=source,
            message=rest,
            raw=line,
        )

    return LogEvent(message=line, raw=line)


def parse_logs(text: str) -> list[LogEvent]:
    events: list[LogEvent] = []
    for line in text.splitlines():
        ev = parse_line(line)
        if ev is not None:
            events.append(ev)
    return events


def parse_logs_node(state: IncidentState) -> dict:
    started = now_iso()
    try:
        events = parse_logs(state.raw_logs)
        return {
            "parsed_events": events,
            "trace": [make_step("parser", started, note=f"{len(events)} events parsed")],
        }
    except Exception as e:
        return {
            "errors": [f"parser: {e}"],
            "trace": [make_step("parser", started, status="error", note=str(e))],
        }

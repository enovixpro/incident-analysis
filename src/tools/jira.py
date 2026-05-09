"""
JIRA ticket creation tool.

CONCEPT: Tool use with safe defaults. If JIRA_URL / JIRA_EMAIL / JIRA_API_TOKEN
are missing, we run in dry-run mode that logs the would-be ticket and returns a
mock key (e.g. OPS-MOCK-1).

Live mode talks to JIRA Cloud's REST API v3. Description is sent as Atlassian
Document Format (ADF) — Cloud rejects plain strings for `description` on v3.
We convert the markdown-ish description we get into a minimal ADF doc.

Configurable via env:
  JIRA_URL            — e.g. https://your-org.atlassian.net
  JIRA_EMAIL
  JIRA_API_TOKEN
  JIRA_PROJECT_KEY    — defaults to OPS
  JIRA_ISSUE_TYPE     — defaults to Task (use "Bug" or "Incident" if your
                        project has them; "Bug" doesn't exist in every project
                        template, which silently breaks ticket creation)
"""
from __future__ import annotations

import logging
import os
from itertools import count
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_mock_counter = count(1)


def _has_real_creds() -> bool:
    return all(
        os.getenv(k, "").strip()
        for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    )


def _to_adf(text: str) -> dict[str, Any]:
    """Convert plain/markdown-ish text to a minimal Atlassian Document Format doc.

    Cloud's REST v3 rejects a plain string for `description` — it must be ADF.
    We split on blank lines and emit one paragraph per chunk; markdown formatting
    (`#`, `**`, etc.) is left literal because rendering it properly would require
    a real markdown→ADF converter, which is overkill here.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": p}],
            }
            for p in paragraphs
        ],
    }


def create_ticket(summary: str, description: str, priority: str = "High") -> dict:
    project_key = os.getenv("JIRA_PROJECT_KEY", "OPS").strip() or "OPS"
    issue_type  = os.getenv("JIRA_ISSUE_TYPE", "Task").strip() or "Task"

    if not _has_real_creds():
        n = next(_mock_counter)
        mock_key = f"{project_key}-MOCK-{n}"
        logger.info("[JIRA MOCK] %s [%s] %s", mock_key, priority, summary[:120])
        return {
            "ok": True,
            "key": mock_key,
            "summary": summary,
            "priority": priority,
            "dry_run": True,
            "preview": description,
        }

    url = os.getenv("JIRA_URL", "").rstrip("/") + "/rest/api/3/issue"
    auth = HTTPBasicAuth(os.getenv("JIRA_EMAIL", ""), os.getenv("JIRA_API_TOKEN", ""))
    payload: dict[str, Any] = {
        "fields": {
            "project":     {"key": project_key},
            "summary":     summary,
            "description": _to_adf(description),
            "issuetype":   {"name": issue_type},
        }
    }
    # Priority isn't in every project's screen scheme — skip it if it 400s.
    payload_with_pri = {**payload, "fields": {**payload["fields"], "priority": {"name": priority}}}

    try:
        resp = requests.post(url, json=payload_with_pri, auth=auth, timeout=15)
        if resp.status_code == 400 and "priority" in resp.text.lower():
            logger.warning("JIRA: project doesn't accept priority field, retrying without it")
            resp = requests.post(url, json=payload, auth=auth, timeout=15)

        if resp.status_code >= 400:
            err_body = resp.text[:600]
            logger.error("JIRA %s for %s: %s", resp.status_code, project_key, err_body)
            return {
                "ok": False,
                "key": "ERROR",
                "summary": summary,
                "priority": priority,
                "dry_run": False,
                "error": f"HTTP {resp.status_code}: {err_body}",
            }

        body = resp.json() if resp.content else {}
        key = body.get("key")
        if not key:
            logger.error("JIRA returned no key for %s. Response: %s", project_key, body)
            return {
                "ok": False,
                "key": "ERROR",
                "summary": summary,
                "priority": priority,
                "dry_run": False,
                "error": f"Unexpected response shape: {body}",
            }

        logger.info("JIRA created %s (%s)", key, summary[:80])
        return {
            "ok": True,
            "key": key,
            "summary": summary,
            "priority": priority,
            "dry_run": False,
            "url": f"{os.getenv('JIRA_URL', '').rstrip('/')}/browse/{key}",
        }

    except requests.RequestException as e:
        logger.exception("JIRA request failed")
        return {
            "ok": False,
            "key": "ERROR",
            "summary": summary,
            "priority": priority,
            "dry_run": False,
            "error": str(e),
        }

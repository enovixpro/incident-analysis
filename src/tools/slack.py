"""
Slack notification tool.

CONCEPT: Tool use with safe defaults. If SLACK_BOT_TOKEN isn't set, we run in
dry-run mode that logs the would-be payload and returns a realistic mock
response. This lets the project run end-to-end without any external creds.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def post_message(channel: str, text: str) -> dict:
    """Post a message to Slack, or return a mock response if no token."""
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()

    if not token:
        # Dry-run / mock mode
        mock_ts = datetime.now(timezone.utc).strftime("%s.000000")
        logger.info("[SLACK MOCK] channel=%s text=%s", channel, text[:200])
        return {
            "ok": True,
            "channel": channel,
            "ts": mock_ts,
            "dry_run": True,
            "preview": text,
        }

    # Real Slack
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        resp = client.chat_postMessage(channel=channel, text=text)
        return {
            "ok": resp.get("ok", False),
            "channel": resp.get("channel"),
            "ts": resp.get("ts"),
            "dry_run": False,
        }
    except Exception as e:
        logger.error("Slack post failed, falling back to mock: %s", e)
        return {"ok": False, "error": str(e), "dry_run": True}

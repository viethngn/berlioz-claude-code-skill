#!/usr/bin/env python3
"""Connector interface for the secretary's auto-sync.

A connector knows how to pull raw material from ONE external source (Slack,
Outlook, ...). It does NOT decide what's a task, and it does NOT write to the
store -- turning messages into todos is model judgment, and every write goes
through the committing `tasks_cli.py upsert` path so reconciliation +
git-tracking stay in one place. A connector therefore returns a *plan*:

    {
      "source":   "slack",
      "status":   "ready" | "delegate" | "not_configured" | "error",
      "mode":     "direct" | "agent",      # how the raw material is obtained
      "material": [ {...}, ... ],          # direct mode: fetched raw refs/text
      "instruction": "…",                  # what the agent must do next
      "note":     "…",                     # human-facing status detail
    }

- status "ready"          -> `material` holds fetched raw content; the agent
                             extracts candidates from it and upserts them.
- status "delegate"       -> the connector can't fetch from Python (e.g. Slack
                             via MCP tools, which only the agent can call);
                             `instruction` tells the agent how to gather it.
- status "not_configured" -> source is known but has no credentials yet
                             (e.g. Outlook before connect-outlook); skipped.
- status "error"          -> `note` explains; sync continues with others.

Stdlib only. No connector imports another plugin's code at module load.
"""

from __future__ import annotations

from datetime import date, timedelta


def window_after(within_days: int, today: date | None = None) -> str:
    """The `--after` date (YYYY-MM-DD) for a `within_days`-day lookback.

    Computed here, once, so no connector (and no LLM) has to do date math.
    Default 3 days => 'read the last 3 days, all messages (read + unread)'.
    """
    today = today or date.today()
    return (today - timedelta(days=max(0, within_days))).isoformat()

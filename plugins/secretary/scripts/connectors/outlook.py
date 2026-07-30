#!/usr/bin/env python3
"""Outlook connector.

Real connectivity via `outlook-local-mcp` (github.com/desek/outlook-local-mcp),
installed directly from upstream by the `connect-outlook` skill and
registered as a stdio MCP server through this plugin's own `.mcp.json`
(`scripts/outlook_mcp_server.py` is the launch wrapper). There is no
R-Musubi involvement anywhere in this design -- an earlier version of this
connector reused a separate app's settings file, which turned out to be a
dead end for Outlook specifically (that app never actually reads its own
url/token fields for this integration) and added an unnecessary cross-app
coupling besides.

This module never calls MCP tools itself -- it can't; that's the agent's
job (base.py's docstring states the same constraint for every connector).
It only decides `delegate` vs `not_configured` by checking local,
cheap-to-read state (`outlook_setup.is_setup_complete()`), and building the
instruction text the agent follows.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from base import window_after  # noqa: E402
import outlook_setup  # noqa: E402


def plan(
    project_root: Path,
    *,
    within_days: int = 3,
    accounts_path: Optional[Path] = None,
    marker_path: Optional[Path] = None,
) -> dict:
    """`accounts_path`/`marker_path` let tests point at a tmp dir instead of
    the real `~/.secretary/outlook/` -- same testability idiom the Slack
    connector uses for `fetch_slack_path`/`wiki_root`."""
    if not outlook_setup.is_setup_complete(accounts_path=accounts_path, marker_path=marker_path):
        return {
            "source": "outlook",
            "status": "not_configured",
            "mode": "n/a",
            "note": "Outlook isn't connected yet -- run /connect-outlook to install "
                    "outlook-local-mcp and sign in.",
        }

    after = window_after(within_days)
    instruction = (
        f"Use the Outlook MCP tools (`mail`, `calendar`) to check for items from {after} "
        "to today. Call `mail` with `operation: \"list_messages\"` (or `search_messages` "
        "with a KQL query for a targeted search) for that window, and `calendar` for the "
        "same window -- call either tool with `operation: \"help\"` first if you're unsure "
        "of exact verb names/args, since these can vary by outlook-local-mcp version. This "
        "is READ-ONLY: do not call any send/delete/create/mark-read/move operation. For "
        "each message/event you judge to be an actual action item, extract a candidate "
        "todo and upsert it (source=outlook, source_ref=<message-or-event-id>). If the "
        "Outlook MCP tools report no account/not connected despite this delegate status, "
        "tell the user setup may have lapsed and to re-run /connect-outlook -- don't retry "
        "in a loop."
    )
    return {
        "source": "outlook",
        "status": "delegate",
        "mode": "agent",
        "window": {"after": after, "before": "today"},
        "instruction": instruction,
        "note": None,
    }

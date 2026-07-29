#!/usr/bin/env python3
"""Slack connector. Primary path: the Slack MCP tools already connected in
the agent's session (no token, no llm-wiki dependency) -- the connector can't
call those itself (they're only reachable from the agent), so it returns a
"delegate" plan with a precise instruction. Fallback: if `llm-wiki` is
installed alongside this plugin AND its `.wikirc.json` has a real Slack
token, this connector can run that fetcher directly (status "ready") --
useful when no Slack MCP server is connected, and for offline/fixture tests.

No writes here. Candidate extraction (is this message a task, what's the
title) is model judgment left to the `tasks` skill; this module only gathers
raw material and the plumbing to reach it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from base import window_after  # noqa: E402

_PLACEHOLDER_RE = re.compile(r"REPLACE_ME|xoxp-REPLACE|xoxb-REPLACE", re.IGNORECASE)


def _find_file_upward(start: Path, relative: str, max_levels: int = 8) -> Optional[Path]:
    """Walk up from `start` looking for `<dir>/<relative>`; returns the FILE
    path itself (unlike `_find_marketplace_root`, which returns a directory)."""
    current = start
    for _ in range(max_levels):
        candidate = current / relative
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _find_marketplace_root(start: Path, max_levels: int = 8) -> Optional[Path]:
    """Walk up from `start` looking for a `.claude-plugin/marketplace.json`;
    returns the directory that CONTAINS `.claude-plugin/` (the marketplace
    root plugin sources are declared relative to), matching bootstrap.py's
    `resolve_marketplace` convention -- not the marketplace.json file itself."""
    current = start
    for _ in range(max_levels):
        if (current / ".claude-plugin" / "marketplace.json").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def resolve_fetch_slack(explicit: Optional[Path] = None) -> Optional[Path]:
    """Locate llm-wiki's fetch_slack.py, checked in two layouts:
    same-marketplace-repo (plugins/llm-wiki/... next to plugins/secretary/...)
    and sibling-installed-plugin (a `llm-wiki` dir next to this plugin's own
    install dir). Returns None if neither exists -- llm-wiki isn't installed.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None

    rel = "skills/ingest/scripts/fetch_slack.py"
    # SCRIPT_DIR = <root>/plugins/secretary/scripts/connectors -- three levels
    # up is <root>/plugins, whose sibling `llm-wiki/` holds the fetcher.
    plugins_dir = SCRIPT_DIR.parent.parent.parent
    candidates = [plugins_dir / "llm-wiki" / rel]

    marketplace_root = _find_marketplace_root(SCRIPT_DIR)
    if marketplace_root:
        candidates.append(marketplace_root / "plugins" / "llm-wiki" / rel)

    for c in candidates:
        if c.exists():
            return c
    return None


def resolve_wiki_root(project_root: Path, explicit: Optional[Path] = None) -> Optional[Path]:
    """Dir containing `.wikirc.json` -- checked at the project root and at
    `<project_root>/wiki`, walking up a few levels like llm-wiki's own
    `find_config`. Returns None if no `.wikirc.json` is found anywhere."""
    if explicit is not None:
        return explicit if (explicit / ".wikirc.json").exists() else None
    for base in (project_root / "wiki", project_root):
        found = _find_file_upward(base, ".wikirc.json", max_levels=5)
        if found:
            return found.parent
    return None


def has_real_token(wiki_root: Path) -> bool:
    path = wiki_root / ".wikirc.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    token = (data.get("slack") or {}).get("token") or ""
    return bool(token) and not _PLACEHOLDER_RE.search(token)


def plan(
    project_root: Path,
    *,
    channels: Optional[list] = None,
    search: Optional[str] = None,
    within_days: int = 3,
    mode: str = "auto",
    fetch_slack_path: Optional[Path] = None,
    wiki_root: Optional[Path] = None,
) -> dict:
    """Build the sync plan for Slack. `mode`: "auto" (MCP primary, mention
    fetcher fallback), "mcp" (MCP only, no fallback mention), or "fetcher"
    (force the direct llm-wiki-fetcher path, actually running it now)."""
    after = window_after(within_days)
    channels = channels or []
    fetcher_path = resolve_fetch_slack(fetch_slack_path)
    resolved_wiki_root = resolve_wiki_root(project_root, wiki_root)
    fetcher_available = bool(
        fetcher_path and resolved_wiki_root and has_real_token(resolved_wiki_root)
    )

    if not channels and not search:
        return {
            "source": "slack",
            "status": "not_configured",
            "mode": "n/a",
            "note": "no channels/search configured -- add secretary/sync.json with "
                    "{\"slack\": {\"channels\": [...]}} or {\"search\": \"...\"}",
        }

    if mode == "fetcher":
        if not fetcher_available:
            return {
                "source": "slack",
                "status": "not_configured",
                "mode": "direct",
                "note": "fetcher mode requested but llm-wiki's fetch_slack.py or a real "
                        f"Slack token wasn't found (fetcher={fetcher_path}, "
                        f"wiki_root={resolved_wiki_root})",
            }
        return _run_fetcher(fetcher_path, resolved_wiki_root, channels, search, after)

    # Default: delegate to the agent, which calls the Slack MCP tools directly.
    instruction = (
        f"Use the Slack MCP tools to read {'channel(s) ' + ', '.join(channels) if channels else ''}"
        f"{(' matching search: ' + search) if search else ''} from {after} to today "
        "(all messages, read and unread -- conversations.history/slack_read_channel does not "
        "filter by read state). For each channel: slack_read_channel (or slack_search_public / "
        "slack_search_public_and_private for --search). Then extract candidate todos and upsert."
    )
    fallback_note = None
    if mode == "auto":
        if fetcher_available:
            fallback_note = (
                f"If the Slack MCP server isn't connected this session, fall back to running "
                f"the fetcher directly: python3 {fetcher_path} --wiki-root {resolved_wiki_root} "
                f"--channel <name> --after {after}"
            )
        else:
            fallback_note = (
                "No fetcher fallback available (llm-wiki not installed or no Slack token in "
                ".wikirc.json) -- the Slack MCP tools are the only path right now."
            )

    return {
        "source": "slack",
        "status": "delegate",
        "mode": "agent",
        "window": {"after": after, "before": "today"},
        "channels": channels,
        "search": search,
        "instruction": instruction,
        "note": fallback_note,
    }


def _run_fetcher(
    fetcher_path: Path,
    wiki_root: Path,
    channels: list,
    search: Optional[str],
    after: str,
) -> dict:
    material = []
    errors = []
    targets = [("--channel", c) for c in channels] or ([("--search", search)] if search else [])
    for flag, value in targets:
        try:
            proc = subprocess.run(
                ["python3", str(fetcher_path), "--wiki-root", str(wiki_root), flag, value, "--after", after],
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            errors.append(f"{value}: {e}")
            continue
        if proc.returncode != 0:
            errors.append(f"{value}: {proc.stderr.strip() or proc.stdout.strip()}")
            continue
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            errors.append(f"{value}: non-JSON output from fetch_slack.py")
            continue
        if out.get("status") != "unchanged" and out.get("raw_md"):
            material.append({"target": value, **out})

    status = "ready" if material or not errors else "error"
    return {
        "source": "slack",
        "status": status,
        "mode": "direct",
        "window": {"after": after, "before": "today"},
        "material": material,
        "note": "; ".join(errors) if errors else None,
    }

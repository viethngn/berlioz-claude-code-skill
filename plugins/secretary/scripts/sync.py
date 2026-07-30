#!/usr/bin/env python3
"""Auto-sync entry point: fan out to every configured source connector
(Slack, Outlook) and report what each found. Read-only -- never writes a
task file and never commits. Turning raw material into todos, matching them
against existing tasks, and calling `tasks_cli.py upsert` is the agent's job
(see skills/tasks/SKILL.md); this script only gathers.

Runs BOTH sources every time by default -- Slack and Outlook checks are not
separately invoked, they're just always part of "sync".

Usage:
    python3 sync.py --tasks-root .
    python3 sync.py --tasks-root . --sources slack
    python3 sync.py --tasks-root . --within-days 5 --channels general,eng-team
    python3 sync.py --tasks-root . --slack-mode fetcher   # force the direct llm-wiki-fetcher path

Config (optional, secretary/sync.json under --tasks-root):
    {
      "autoSyncOnStart": true,
      "withinDays": 3,
      "slack": {"channels": ["general"], "search": null, "mode": "auto"},
      "outlook": {"enabled": true}
    }
CLI flags override config; config overrides the built-in defaults. Outlook
has no url/token config -- run /connect-outlook once to install and sign in
to outlook-local-mcp; after that, this script only checks local state
(see connectors/outlook.py) to decide delegate vs not_configured.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "connectors"))

import task_store  # noqa: E402
import slack as slack_connector  # noqa: E402
import outlook as outlook_connector  # noqa: E402

ALL_SOURCES = ("slack", "outlook")


def sync_config_path(root: Path) -> Path:
    return root / "secretary" / "sync.json"


def load_sync_config(root: Path) -> dict:
    path = sync_config_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def existing_source_refs(root: Path, source: str) -> list:
    """sourceRefs already on file for this source (active + archived) -- lets
    the agent skip obviously-seen items before spending effort on extraction.
    Belt-and-suspenders: `upsert_from_source` is idempotent on its own, so
    this is an efficiency aid, not a correctness requirement."""
    tasks = task_store.load_all_tasks(root, include_archived=True)
    return sorted({
        t["sourceRef"] for t in tasks
        if t.get("source") == source and (t.get("sourceRef") or "").strip()
    })


def run_slack(root: Path, cfg: dict, args) -> dict:
    slack_cfg = cfg.get("slack") or {}
    channels = args.channels if args.channels is not None else (slack_cfg.get("channels") or [])
    search = args.search if args.search is not None else slack_cfg.get("search")
    mode = args.slack_mode or slack_cfg.get("mode") or "auto"
    result = slack_connector.plan(
        root,
        channels=channels,
        search=search,
        within_days=args.within_days,
        mode=mode,
    )
    result["existing_source_refs"] = existing_source_refs(root, "slack")
    return result


def run_outlook(root: Path, cfg: dict, args) -> dict:
    outlook_cfg = cfg.get("outlook") or {}
    if not outlook_cfg.get("enabled", True):
        return {
            "source": "outlook",
            "status": "not_configured",
            "mode": "n/a",
            "note": "outlook.enabled is false in secretary/sync.json",
            "existing_source_refs": existing_source_refs(root, "outlook"),
        }
    result = outlook_connector.plan(root, within_days=args.within_days)
    result["existing_source_refs"] = existing_source_refs(root, "outlook")
    return result


RUNNERS = {"slack": run_slack, "outlook": run_outlook}


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary auto-sync: gather from all configured sources")
    parser.add_argument("--tasks-root", type=Path, default=Path.cwd())
    parser.add_argument("--sources", default=None, help="comma-separated subset of: slack,outlook (default: both)")
    parser.add_argument("--within-days", type=int, default=None)
    parser.add_argument("--channels", default=None, help="comma-separated Slack channel names/ids")
    parser.add_argument("--search", default=None, help="Slack search query")
    parser.add_argument("--slack-mode", choices=["auto", "mcp", "fetcher"], default=None)
    args = parser.parse_args()

    root = args.tasks_root.expanduser().resolve()
    if not task_store.tasks_dir(root).exists():
        print(json.dumps({"error": f"no secretary/tasks/ found under {root} -- run /create-secretary first"}))
        return 1

    if args.channels is not None:
        args.channels = [c.strip() for c in args.channels.split(",") if c.strip()]

    cfg = load_sync_config(root)
    args.within_days = args.within_days if args.within_days is not None else cfg.get("withinDays", 3)

    requested = [s.strip() for s in args.sources.split(",")] if args.sources else list(ALL_SOURCES)
    unknown = [s for s in requested if s not in RUNNERS]
    if unknown:
        print(json.dumps({"error": f"unknown source(s): {', '.join(unknown)} -- valid: {', '.join(ALL_SOURCES)}"}))
        return 1

    results = {source: RUNNERS[source](root, cfg, args) for source in requested}

    summary = {
        "sources": results,
        "ready": [s for s, r in results.items() if r.get("status") == "ready"],
        "delegate": [s for s, r in results.items() if r.get("status") == "delegate"],
        "not_configured": [s for s, r in results.items() if r.get("status") == "not_configured"],
        "error": [s for s, r in results.items() if r.get("status") == "error"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

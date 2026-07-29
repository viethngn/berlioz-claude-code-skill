#!/usr/bin/env python3
"""SessionStart hook: surface overdue / due-soon tasks as injected context,
and -- if `secretary/sync.json` has `autoSyncOnStart: true` -- instruct the
agent to auto-sync Slack/Outlook before answering anything about the list.

Read-only -- never writes, never commits, never runs `sync.py` itself (this
hook is a plain Python subprocess with a tight timeout; it can't call the
Slack MCP tools that live in the agent's own session). It only *tells* the
agent to sync; the agent runs `sync.py`, reconciles, and reports. Wired up by
`create-secretary`'s bootstrap.py into the target project's
`.claude/settings.json` as:

    {"type": "command", "command": "python3",
     "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/due_soon.py"]}

`${CLAUDE_PLUGIN_ROOT}` locates this script; `CLAUDE_PROJECT_DIR` (an env
var Claude Code also exports to hooks) locates the TARGET project's task
data, which is why this script takes no required arguments.

Usage:
    python3 due_soon.py                       # reads $CLAUDE_PROJECT_DIR
    python3 due_soon.py --project-dir /path    # manual testing override
    python3 due_soon.py --within-days 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import task_store  # noqa: E402
import sync as sync_mod  # noqa: E402


def _auto_sync_instruction(root: Path) -> Optional[str]:
    """None unless secretary/sync.json sets autoSyncOnStart: true. Sync
    itself needs the agent's own MCP tools and the committing CLI, so this
    hook can only ask for it -- never run it. Fires on EVERY session start
    when enabled, even if nothing is currently due, since syncing is what
    might add newly-due items in the first place."""
    cfg = sync_mod.load_sync_config(root)
    if not cfg.get("autoSyncOnStart"):
        return None
    # SCRIPT_DIR (this hook's own resolved directory), not the unexpanded
    # ${CLAUDE_PLUGIN_ROOT} literal -- that placeholder is only substituted by
    # the harness inside a hook's `args` array, not in printed output text.
    sync_py = SCRIPT_DIR / "sync.py"
    return (
        "Auto-sync is enabled for this project. Before answering anything about "
        f"the task list, run `python3 {sync_py} --tasks-root {root}`, extract "
        "candidate todos from any 'ready'/'delegate' sources per the `tasks` "
        "skill's sync rules, reconcile via `tasks_cli.py upsert` (never blindly "
        "create -- match existing tasks first), then report the digest below "
        "plus anything newly synced."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary due-soon digest hook")
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--within-days", type=int, default=3)
    args = parser.parse_args()

    project_dir = args.project_dir or os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return 0  # no project context -- nothing to do, stay silent

    root = Path(project_dir)
    if not task_store.tasks_dir(root).exists():
        return 0  # project was never bootstrapped with /create-secretary

    tasks = task_store.load_all_tasks(root, include_archived=False)
    digest = task_store.compute_digest(tasks, within_days=args.within_days)
    sync_instruction = _auto_sync_instruction(root)

    if not digest["overdue"] and not digest["due_soon"] and not sync_instruction:
        return 0  # nothing due, auto-sync not enabled -- no banner needed

    context = task_store.format_digest_text(digest)
    if sync_instruction:
        context = f"{sync_instruction}\n\n{context}"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

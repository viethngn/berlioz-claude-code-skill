#!/usr/bin/env python3
"""Day-to-day task CRUD/render CLI for the `secretary` plugin. Stdlib only.

Thin argparse wrapper around scripts/task_store.py -- every mutating
subcommand writes, regenerates the index, and commits via that shared
engine, so this file never touches storage directly.

Usage:
    python3 tasks_cli.py add --title "..." [--due-date YYYY-MM-DD] [--priority low|medium|high]
                             [--parent-id T-0001] [--done-when "..."] [--refs "[[a]], [[b]]"]
                             [--source manual] [--body "..."]
    python3 tasks_cli.py update T-0007 [--title ...] [--status ...] [--due-date ...] ...
    python3 tasks_cli.py done T-0007
    python3 tasks_cli.py remove T-0007 [--reason "..."] [--cascade]
    python3 tasks_cli.py list [--status todo] [--overdue] [--due-within 7] [--parent-id T-0001] [--format text|json]
    python3 tasks_cli.py digest [--within-days 3] [--format text|json]

All subcommands accept `--tasks-root` (default: cwd) -- the project root
that CONTAINS `secretary/`, not `secretary/` itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# skills/tasks/scripts/ -> skills/tasks/ -> skills/ -> plugin root -> scripts/
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import task_store  # noqa: E402


def _print(result, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(result if isinstance(result, str) else json.dumps(result, indent=2, default=str))


def _task_public(task: dict) -> dict:
    return {k: v for k, v in task.items() if not k.startswith("_")} | {
        "warnings": task.get("_warnings", []),
    }


def cmd_add(args, root: Path) -> dict:
    task = task_store.add_task(
        root,
        title=args.title,
        due_date=args.due_date or "",
        priority=args.priority or "medium",
        parent_id=args.parent_id or "",
        done_when=args.done_when or "",
        refs=args.refs or "",
        source=args.source or "manual",
        source_ref=args.source_ref or "",
        source_hash=args.source_hash or "",
        body=args.body or "",
    )
    return _task_public(task)


def cmd_upsert(args, root: Path) -> dict:
    """Reconciling create-or-update, keyed on --source-ref. This is the path
    the sync connectors use so a re-synced Slack/Outlook item updates its
    existing task instead of duplicating it."""
    task = task_store.upsert_from_source(
        root,
        source=args.source or "manual",
        source_ref=args.source_ref or "",
        source_hash=args.source_hash or "",
        title=args.title,
        due_date=args.due_date or "",
        priority=args.priority or "medium",
        refs=args.refs or "",
        done_when=args.done_when or "",
        body=args.body or "",
    )
    result = _task_public(task)
    result["verdict"] = task.get("_verdict")
    return result


def cmd_update(args, root: Path) -> dict:
    task = task_store.update_task(
        root,
        args.id,
        title=args.title,
        status=args.status,
        dueDate=args.due_date,
        priority=args.priority,
        parentId=args.parent_id,
        doneWhen=args.done_when,
        refs=args.refs,
        body=args.body,
    )
    return _task_public(task)


def cmd_done(args, root: Path) -> dict:
    task = task_store.mark_done(root, args.id)
    return _task_public(task)


def cmd_remove(args, root: Path) -> dict:
    task = task_store.archive_task(root, args.id, reason=args.reason or "", cascade=args.cascade)
    return _task_public(task)


def cmd_list(args, root: Path):
    # Load the full set (incl. archived) so a parent whose children have
    # already been completed/removed can still be flagged ready-to-close --
    # but default to showing only active tasks, same as before, unless the
    # user explicitly asked for a specific status.
    all_tasks = task_store.load_all_tasks(root, include_archived=True)
    if args.status:
        tasks = [t for t in all_tasks if t.get("status") == args.status]
    else:
        tasks = [t for t in all_tasks if t.get("status") in task_store.OPEN_STATUSES]

    if args.parent_id:
        tasks = [t for t in tasks if t.get("parentId") == args.parent_id]
    if args.overdue or args.due_within is not None:
        digest = task_store.compute_digest(tasks, within_days=args.due_within or 0)
        if args.overdue and args.due_within is not None:
            tasks = digest["overdue"] + digest["due_soon"]
        elif args.overdue:
            tasks = digest["overdue"]
        else:
            tasks = digest["due_soon"]

    if args.format == "json":
        return [_task_public(t) for t in tasks]
    return task_store.render_tree_text(tasks, title="Tasks", wiki_dir=task_store.wiki_root(root), all_tasks=all_tasks)


def cmd_digest(args, root: Path):
    tasks = task_store.load_all_tasks(root, include_archived=False)
    digest = task_store.compute_digest(tasks, within_days=args.within_days)
    if args.format == "json":
        return {
            "overdue": [_task_public(t) for t in digest["overdue"]],
            "due_soon": [_task_public(t) for t in digest["due_soon"]],
        }
    return task_store.format_digest_text(digest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary task CRUD/render CLI")
    parser.add_argument("--tasks-root", type=Path, default=Path.cwd(), help="project root containing secretary/")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--due-date", default=None)
    p_add.add_argument("--priority", choices=["low", "medium", "high"], default=None)
    p_add.add_argument("--parent-id", default=None)
    p_add.add_argument("--done-when", default=None)
    p_add.add_argument("--refs", default=None)
    p_add.add_argument("--source", default=None)
    p_add.add_argument("--source-ref", default=None)
    p_add.add_argument("--source-hash", default=None)
    p_add.add_argument("--body", default=None)
    p_add.set_defaults(func=cmd_add, format="json")

    p_upsert = sub.add_parser("upsert", help="reconciling create-or-update keyed on --source-ref")
    p_upsert.add_argument("--title", required=True)
    p_upsert.add_argument("--source", required=True)
    p_upsert.add_argument("--source-ref", required=True)
    p_upsert.add_argument("--source-hash", default=None)
    p_upsert.add_argument("--due-date", default=None)
    p_upsert.add_argument("--priority", choices=["low", "medium", "high"], default=None)
    p_upsert.add_argument("--refs", default=None)
    p_upsert.add_argument("--done-when", default=None)
    p_upsert.add_argument("--body", default=None)
    p_upsert.set_defaults(func=cmd_upsert, format="json")

    p_update = sub.add_parser("update")
    p_update.add_argument("id")
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--status", choices=list(task_store.OPEN_STATUSES) + list(task_store.CLOSED_STATUSES), default=None)
    p_update.add_argument("--due-date", default=None)
    p_update.add_argument("--priority", choices=["low", "medium", "high"], default=None)
    p_update.add_argument("--parent-id", default=None)
    p_update.add_argument("--done-when", default=None)
    p_update.add_argument("--refs", default=None)
    p_update.add_argument("--body", default=None)
    p_update.set_defaults(func=cmd_update, format="json")

    p_done = sub.add_parser("done")
    p_done.add_argument("id")
    p_done.set_defaults(func=cmd_done, format="json")

    p_remove = sub.add_parser("remove")
    p_remove.add_argument("id")
    p_remove.add_argument("--reason", default=None)
    p_remove.add_argument("--cascade", action="store_true")
    p_remove.set_defaults(func=cmd_remove, format="json")

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", choices=list(task_store.OPEN_STATUSES) + list(task_store.CLOSED_STATUSES), default=None)
    p_list.add_argument("--parent-id", default=None)
    p_list.add_argument("--overdue", action="store_true")
    p_list.add_argument("--due-within", type=int, default=None)
    p_list.add_argument("--format", choices=["text", "json"], default="text")
    p_list.set_defaults(func=cmd_list)

    p_digest = sub.add_parser("digest")
    p_digest.add_argument("--within-days", type=int, default=3)
    p_digest.add_argument("--format", choices=["text", "json"], default="text")
    p_digest.set_defaults(func=cmd_digest)

    args = parser.parse_args()
    root = args.tasks_root.expanduser().resolve()

    if not task_store.tasks_dir(root).exists():
        print(json.dumps({
            "error": f"no secretary/tasks/ found under {root} -- run /create-secretary first",
        }))
        return 1

    try:
        result = args.func(args, root)
    except (KeyError, ValueError) as e:
        # ValueError: a malformed target task file (parse_task_file) or an
        # invalid id reaching find_task/write_task — _load_dir already
        # tolerates one bad file during list/digest, but update/done/remove
        # target a *specific* file directly and would otherwise crash with a
        # raw traceback instead of this tool's normal clean JSON error.
        print(json.dumps({"error": str(e)}))
        return 1

    fmt = getattr(args, "format", "json")
    _print(result, fmt)
    return 0


if __name__ == "__main__":
    sys.exit(main())

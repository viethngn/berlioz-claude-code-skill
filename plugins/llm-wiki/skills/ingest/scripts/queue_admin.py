#!/usr/bin/env python3
"""Inspect and manage bulk-ingest job queues.

Subcommands:
    list                              List all jobs (id, kind, query, counts)
    show <job-id>                     Full JSON of one queue
    reset <job-id> [--status STATE]   Reset all items (or those matching STATE) to raw_status=pending
    mark <job-id> --ref REF --wiki-done | --wiki-skipped | --wiki-pending
    mark <job-id> --ref REF --raw-pending | --raw-done | --raw-unchanged | --raw-failed
    delete <job-id> [--force]         Delete a queue directory

Every subcommand prints a JSON summary to stdout. Stdlib only.

Named queue_admin.py rather than queue.py because scripts under this
directory get added to sys.path[0] at runtime; a top-level `queue.py`
would shadow the stdlib `queue` module (which urllib3 imports).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from bulk_queue import (
    VALID_RAW_STATUS,
    VALID_WIKI_STATUS,
    job_dir,
    list_jobs,
    load_queue,
)
from config import ConfigError, load_config


def _resolve_wiki_root(args) -> Path:
    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    return cfg.wiki_root


def _cmd_list(args) -> int:
    wiki_root = _resolve_wiki_root(args)
    jobs = list_jobs(wiki_root)
    print(json.dumps({"wiki_root": str(wiki_root), "jobs": jobs}, indent=2, ensure_ascii=False))
    return 0


def _cmd_show(args) -> int:
    wiki_root = _resolve_wiki_root(args)
    try:
        q = load_queue(wiki_root, args.job_id)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(q.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_reset(args) -> int:
    wiki_root = _resolve_wiki_root(args)
    try:
        q = load_queue(wiki_root, args.job_id)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    target = args.status
    changed = 0
    for item in q.items:
        if target and item.raw_status != target:
            continue
        item.raw_status = "pending"
        item.last_error = None
        changed += 1
    q.save()
    print(
        json.dumps(
            {"job_id": args.job_id, "reset_items": changed, "target": target or "all"},
            indent=2,
        )
    )
    return 0


def _cmd_mark(args) -> int:
    wiki_root = _resolve_wiki_root(args)
    try:
        q = load_queue(wiki_root, args.job_id)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    item = q.find(args.ref)
    if item is None:
        print(f"ERROR: ref {args.ref!r} not in job {args.job_id}", file=sys.stderr)
        return 1

    old_raw, old_wiki = item.raw_status, item.wiki_status
    if args.raw_status:
        if args.raw_status not in VALID_RAW_STATUS:
            print(f"ERROR: invalid raw_status: {args.raw_status}", file=sys.stderr)
            return 1
        item.raw_status = args.raw_status
    if args.wiki_status:
        if args.wiki_status not in VALID_WIKI_STATUS:
            print(f"ERROR: invalid wiki_status: {args.wiki_status}", file=sys.stderr)
            return 1
        item.wiki_status = args.wiki_status
    q.save()
    print(
        json.dumps(
            {
                "job_id": args.job_id,
                "ref": args.ref,
                "old": {"raw_status": old_raw, "wiki_status": old_wiki},
                "new": {"raw_status": item.raw_status, "wiki_status": item.wiki_status},
            },
            indent=2,
        )
    )
    return 0


def _cmd_delete(args) -> int:
    wiki_root = _resolve_wiki_root(args)
    directory = job_dir(wiki_root, args.job_id)
    if not directory.exists():
        print(f"ERROR: no job dir at {directory}", file=sys.stderr)
        return 1
    if not args.force:
        print(
            f"ERROR: refusing to delete {directory} — pass --force to confirm.",
            file=sys.stderr,
        )
        return 1
    shutil.rmtree(directory)
    print(json.dumps({"deleted": str(directory)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and manage bulk-ingest job queues")
    parser.add_argument("--wiki-root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all jobs")

    show = sub.add_parser("show", help="Show one job's full queue.json")
    show.add_argument("job_id")

    reset = sub.add_parser("reset", help="Reset items to raw_status=pending")
    reset.add_argument("job_id")
    reset.add_argument(
        "--status",
        choices=sorted(VALID_RAW_STATUS),
        default=None,
        help="Only reset items currently in this state (default: reset all)",
    )

    mark = sub.add_parser("mark", help="Update one item's status")
    mark.add_argument("job_id")
    mark.add_argument("--ref", required=True, help="Item ref (page id or issue key)")
    mark.add_argument("--raw-status", choices=sorted(VALID_RAW_STATUS))
    mark.add_argument("--wiki-status", choices=sorted(VALID_WIKI_STATUS))
    # Convenience shortcuts
    mark.add_argument("--wiki-done", action="store_const", const="done", dest="wiki_status")
    mark.add_argument("--wiki-skipped", action="store_const", const="skipped", dest="wiki_status")
    mark.add_argument("--wiki-pending", action="store_const", const="pending", dest="wiki_status")
    mark.add_argument("--raw-pending", action="store_const", const="pending", dest="raw_status")

    delete = sub.add_parser("delete", help="Delete a job queue directory")
    delete.add_argument("job_id")
    delete.add_argument("--force", action="store_true")

    args = parser.parse_args()

    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "show":
        return _cmd_show(args)
    if args.cmd == "reset":
        return _cmd_reset(args)
    if args.cmd == "mark":
        return _cmd_mark(args)
    if args.cmd == "delete":
        return _cmd_delete(args)
    parser.error(f"unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

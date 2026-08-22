#!/usr/bin/env python3
"""Long-running, resumable bulk fetcher for a discovered job queue.

For each pending / previously-failed item in the queue, this script:
  1. Invokes the appropriate single-source fetcher (fetch_confluence.py,
     fetch_jira.py, or fetch_web.py) via subprocess so it inherits the diff
     gates and error handling of the existing single-source flow.
  2. Runs extract_images.py to download any image_hints (with the SHA-based
     dedup pass).
  3. Runs the image-diff loop from ingest.py against the resulting slug so
     new / changed images are described by nano-banana-pro.
  4. Updates the queue item's raw_status, slug, and image counts.
  5. Writes the queue after every item so Ctrl-C is safe.

Circuit breaker: `--max-consecutive-failures` (default 5) — if this many
items fail in a row, the script exits non-zero so the user can back off
and resume with the same command.

Usage:
    python3 prefetch.py --wiki-root PATH --job-id <id>
    python3 prefetch.py --wiki-root PATH --job-id <id> --max-items 50
    python3 prefetch.py --wiki-root PATH --job-id <id> --retry-failed
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Orchestrator-level; stdlib only.
from bulk_queue import Queue, load_queue
from config import ConfigError, load_config


SCRIPT_DIR = Path(__file__).resolve().parent


class Interrupted(Exception):
    """Raised when the user Ctrl-C's mid-run."""


def _install_sigint_handler():
    def _handler(signum, frame):  # noqa: ARG001
        raise Interrupted()

    signal.signal(signal.SIGINT, _handler)


def _run_script(script: str, extra: list) -> dict:
    cmd = [sys.executable, str(SCRIPT_DIR / script), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{script} exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"stdout": stdout}


def _fetch_one(queue: Queue, item, wiki_root: Path) -> dict:
    """Run the single-item fetcher for one queue entry.

    Returns a dict with `slug`, `status`, `image_hints`, `source_json`.
    """
    if queue.kind in {"confluence_space", "confluence_cql"}:
        return _run_script(
            "fetch_confluence.py",
            [
                "--wiki-root",
                str(wiki_root),
                "--page-id",
                item.ref,
            ],
        )
    if queue.kind == "jira_jql":
        return _run_script(
            "fetch_jira.py",
            [
                "--wiki-root",
                str(wiki_root),
                "--key",
                item.ref,
            ],
        )
    if queue.kind in {"web_sitemap", "web_crawl"}:
        # Robots was already enforced at discovery time for the whole job, so
        # skip the per-page lookup — it would double the request count.
        return _run_script(
            "fetch_web.py",
            [
                "--wiki-root",
                str(wiki_root),
                "--url",
                item.ref,
                "--no-robots-check",
            ],
        )
    raise ValueError(f"unknown queue kind: {queue.kind}")


def _describe_changed_images(wiki_root: Path, slug: str) -> dict:
    """Delegate to ingest.py's image loop by calling it as a subprocess
    against the specific slug. Uses --commit-only-noop trick: we invoke a
    small helper subcommand.

    Simpler: import the function directly.
    """
    # Import lazily so this module stays cheap for `queue_admin.py list`.
    from ingest import do_image_diff_loop  # noqa: WPS433

    return do_image_diff_loop(wiki_root, slug)


def _prefetch_one(
    queue: Queue, item, wiki_root: Path, do_images: bool
) -> dict:
    """Process one queue item end-to-end. Mutates `item` in place.

    Returns a small dict for logging.
    """
    item.fetch_attempts += 1

    try:
        fetch_result = _fetch_one(queue, item, wiki_root)
    except Exception as e:  # noqa: BLE001
        item.raw_status = "failed"
        item.last_error = str(e)[:500]
        return {"ref": item.ref, "status": "failed", "reason": item.last_error}

    slug = fetch_result.get("slug")
    if not slug:
        item.raw_status = "failed"
        item.last_error = "fetcher returned no slug"
        return {"ref": item.ref, "status": "failed", "reason": item.last_error}

    item.slug = slug
    item.title = item.title or str(fetch_result.get("title") or "")
    fetch_status = str(fetch_result.get("status") or "changed")

    if fetch_status == "unchanged":
        item.raw_status = "unchanged"
        item.last_error = None
        return {"ref": item.ref, "status": "unchanged", "slug": slug}

    # Download image_hints (Confluence attachments, Jira attachments)
    source_json = fetch_result.get("source_json")
    if source_json and (fetch_result.get("image_hints") or []):
        try:
            _run_script(
                "extract_images.py",
                [
                    "--wiki-root",
                    str(wiki_root),
                    "--source-json",
                    str(source_json),
                    "--slug",
                    slug,
                ],
            )
        except Exception as e:  # noqa: BLE001
            item.raw_status = "failed"
            item.last_error = f"extract_images failed: {e}"[:500]
            return {"ref": item.ref, "status": "failed", "reason": item.last_error}

    # Run image-diff loop (describe new/changed images)
    if do_images:
        try:
            image_summary = _describe_changed_images(wiki_root, slug)
        except Exception as e:  # noqa: BLE001
            item.raw_status = "failed"
            item.last_error = f"image description failed: {e}"[:500]
            return {"ref": item.ref, "status": "failed", "reason": item.last_error}
        item.images_new = int(image_summary.get("new", 0))
        item.images_changed = int(image_summary.get("changed", 0))

    item.raw_status = "done"
    item.last_error = None
    return {
        "ref": item.ref,
        "status": "done",
        "slug": slug,
        "images_new": item.images_new,
        "images_changed": item.images_changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-fetch every pending item in a job queue")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Process at most N items in this run (0 = no limit)",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help="Abort the run if this many items fail in a row",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also retry items previously marked 'failed' (default: only 'pending')",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Skip the nano-banana-pro description step (still downloads image bytes)",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        queue = load_queue(cfg.wiki_root, args.job_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.retry_failed:
        candidates = [i for i in queue.items if i.raw_status in {"pending", "failed"}]
    else:
        candidates = [i for i in queue.items if i.raw_status == "pending"]

    if args.max_items > 0:
        candidates = candidates[: args.max_items]

    if not candidates:
        print(
            json.dumps(
                {"job_id": args.job_id, "processed": 0, "note": "no pending items — nothing to do"},
                indent=2,
            )
        )
        return 0

    _install_sigint_handler()

    consecutive_failures = 0
    processed: list = []
    aborted_reason: Optional[str] = None
    start = time.time()

    try:
        for item in candidates:
            log = _prefetch_one(queue, item, cfg.wiki_root, do_images=not args.skip_images)
            queue.save()  # checkpoint after every item
            processed.append(log)

            print(json.dumps({"item": log}, ensure_ascii=False), flush=True)

            if item.raw_status == "failed":
                consecutive_failures += 1
                if consecutive_failures >= args.max_consecutive_failures:
                    aborted_reason = (
                        f"circuit breaker: {consecutive_failures} consecutive failures — "
                        "pausing so the user can back off and resume"
                    )
                    break
            else:
                consecutive_failures = 0
    except Interrupted:
        aborted_reason = "SIGINT (Ctrl-C)"
        queue.save()

    elapsed = time.time() - start
    summary = {
        "job_id": args.job_id,
        "processed": len(processed),
        "elapsed_seconds": round(elapsed, 2),
        "aborted": bool(aborted_reason),
        "aborted_reason": aborted_reason,
        "counts": queue.counts(),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if aborted_reason:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

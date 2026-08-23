#!/usr/bin/env python3
"""Long-running, resumable bulk fetcher for a discovered job queue.

Drives both kinds of queue: a bulk query (one space / CQL / JQL / sitemap /
crawl, every item the same type) and a "refresh" queue built by
`discover.py --refresh`, which mixes every source the wiki has ever ingested
and so carries a per-item `source_kind`.

For each pending / previously-failed item in the queue, this script:
  1. Invokes the appropriate single-source fetcher (fetch_confluence.py,
     fetch_jira.py, fetch_web.py, fetch_local.py or fetch_slack.py) via
     subprocess so it inherits the diff gates and error handling of the
     existing single-source flow.
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


# Which fetcher handles each per-item source_kind, and whether it accepts
# --force. A "refresh" queue is heterogeneous, so the item decides; a bulk
# query queue is homogeneous, so queue.kind decides (see _source_kind_of).
_SOURCE_KIND_TO_QUEUE_KIND = {
    "confluence_space": "confluence",
    "confluence_cql": "confluence",
    "jira_jql": "jira",
    "web_sitemap": "web_bulk",
    "web_crawl": "web_bulk",
}
# fetch_local.py has no --force: it always diff-writes from the original file.
_SUPPORTS_FORCE = {"confluence", "jira", "web", "web_bulk", "slack_channel", "slack_thread"}


def _source_kind_of(queue: Queue, item) -> str:
    """Which fetcher this item needs.

    Items in a "refresh" queue carry their own source_kind because the queue
    mixes Confluence, Jira, web, local and Slack. Items in a bulk query queue
    don't, so fall back to the queue's kind — that keeps every pre-existing
    queue on disk working untouched.
    """
    if item.source_kind:
        return item.source_kind
    kind = _SOURCE_KIND_TO_QUEUE_KIND.get(queue.kind)
    if not kind:
        raise ValueError(
            f"cannot pick a fetcher for queue kind {queue.kind!r} without a "
            f"per-item source_kind (ref {item.ref!r})"
        )
    return kind


def _fetch_one(queue: Queue, item, wiki_root: Path, force: bool = False) -> dict:
    """Run the single-item fetcher for one queue entry.

    Returns a dict with `slug`, `status`, `image_hints`, `source_json`.
    """
    source_kind = _source_kind_of(queue, item)
    base = ["--wiki-root", str(wiki_root)]
    extra = ["--force"] if (force and source_kind in _SUPPORTS_FORCE) else []

    if source_kind == "confluence":
        return _run_script("fetch_confluence.py", [*base, "--page-id", item.ref, *extra])
    if source_kind == "jira":
        return _run_script("fetch_jira.py", [*base, "--key", item.ref, *extra])
    if source_kind == "web_bulk":
        # Robots was already enforced at discovery time for the whole job, so
        # skip the per-page lookup — it would double the request count.
        return _run_script(
            "fetch_web.py", [*base, "--url", item.ref, "--no-robots-check", *extra]
        )
    if source_kind == "web":
        # An individually-ingested page never went through a robots-filtered
        # discovery pass, so it keeps the per-fetch check.
        return _run_script("fetch_web.py", [*base, "--url", item.ref, *extra])
    if source_kind == "local":
        return _run_script("fetch_local.py", [*base, "--path", item.ref])
    if source_kind == "slack_channel":
        # No --after: fetch_slack.py resumes from its own watermark (the local
        # one, or the committed fetched_until on a fresh clone).
        return _run_script("fetch_slack.py", [*base, "--channel", item.ref, *extra])
    if source_kind == "slack_thread":
        if not item.thread_ts:
            raise ValueError(f"slack_thread item {item.ref!r} has no thread_ts")
        return _run_script(
            "fetch_slack.py",
            [*base, "--channel", item.ref, "--thread-ts", item.thread_ts, *extra],
        )
    raise ValueError(f"unknown source kind: {source_kind}")


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
    queue: Queue, item, wiki_root: Path, do_images: bool, force: bool = False
) -> dict:
    """Process one queue item end-to-end. Mutates `item` in place.

    Returns a small dict for logging.
    """
    item.fetch_attempts += 1

    try:
        fetch_result = _fetch_one(queue, item, wiki_root, force=force)
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

    # --force means "re-run everything even if the bytes are identical", so it
    # must not take this shortcut — same as ingest.py's single-item flow, which
    # only honors the unchanged gate when --force is absent.
    if fetch_status == "unchanged" and not force:
        item.raw_status = "unchanged"
        item.last_error = None
        if item.prior_wiki_status in {"done", "skipped"}:
            item.wiki_status = item.prior_wiki_status
        return {
            "ref": item.ref,
            "status": "unchanged",
            "slug": slug,
            "wiki_status": item.wiki_status,
        }

    # The fetcher just wrote NEW raw content to disk, so this item's wiki page
    # must be re-synthesized. Retire the carry-over hint now: every failure
    # path below marks the item "failed" *after* the raw bytes have landed, so
    # a later retry (`--resume` implies --retry-failed, and `queue_admin reset`
    # does the same) would refetch, see "unchanged", and restore
    # wiki_status="done" for a page whose wiki side was never touched.
    item.prior_wiki_status = None

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
        "--force",
        action="store_true",
        help=(
            "Bypass each fetcher's content-diff gate so every item is rewritten "
            "and its images re-described (fetch_local.py has no gate to bypass)"
        ),
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
            log = _prefetch_one(
                queue,
                item,
                cfg.wiki_root,
                do_images=not args.skip_images,
                force=args.force,
            )
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

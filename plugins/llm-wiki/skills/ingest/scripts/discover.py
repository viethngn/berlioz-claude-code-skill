#!/usr/bin/env python3
"""Enumerate a Confluence space, Confluence CQL query, or Jira JQL query and
write a `.wiki-state/bulk-jobs/<id>/queue.json` for prefetch.py to consume.

Usage:
    python3 discover.py --wiki-root PATH --space KEY
    python3 discover.py --wiki-root PATH --cql "space=FOO AND label=onboarding"
    python3 discover.py --wiki-root PATH --jql "project=PROJ AND updated > -30d"

Options:
    --replace    Overwrite an existing job with the same (kind, query)
    --limit N    Cap the number of items (useful for testing)

Prints a JSON summary to stdout, including the assigned job_id.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

from _deps import require

require(["requests"])

from bulk_queue import (
    Item,
    Queue,
    VALID_KINDS,
    find_matching,
    load_queue,
    make_job_id,
)
from config import ConfigError, apply_ssl_env, load_config
from rate_limiter import RateLimitFailure, get_limiter


CONFLUENCE_PAGE_SIZE = 50
JIRA_PAGE_SIZE = 50


def _atlassian_headers(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json"}


def enumerate_confluence_space(
    base_url: str, pat: str, space_key: str, verify: bool, limiter, limit: int
) -> List[Item]:
    return _paginate_confluence(
        base_url,
        pat,
        verify,
        limiter,
        limit,
        params_builder=lambda start: {
            "spaceKey": space_key,
            "type": "page",
            "expand": "version",
            "limit": CONFLUENCE_PAGE_SIZE,
            "start": start,
        },
        endpoint="/rest/api/content",
    )


def enumerate_confluence_cql(
    base_url: str, pat: str, cql: str, verify: bool, limiter, limit: int
) -> List[Item]:
    return _paginate_confluence(
        base_url,
        pat,
        verify,
        limiter,
        limit,
        params_builder=lambda start: {
            "cql": cql,
            "limit": CONFLUENCE_PAGE_SIZE,
            "start": start,
        },
        endpoint="/rest/api/content/search",
    )


def _paginate_confluence(
    base_url: str,
    pat: str,
    verify: bool,
    limiter,
    limit: int,
    params_builder,
    endpoint: str,
) -> List[Item]:
    items: List[Item] = []
    start = 0
    while True:
        params = params_builder(start)
        url = f"{base_url}{endpoint}"
        try:
            resp = limiter.request(
                "GET",
                url,
                headers=_atlassian_headers(pat),
                params=params,
                verify=verify,
                timeout=60,
            )
        except RateLimitFailure as e:
            raise SystemExit(f"ERROR: {e}")
        if resp.status_code == 401:
            raise SystemExit("ERROR: 401 Unauthorized. Check atlassian.confluence_pat in .wikirc.json.")
        if resp.status_code == 403:
            raise SystemExit("ERROR: 403 Forbidden — the PAT cannot list this space/query.")
        if resp.status_code == 404:
            raise SystemExit(f"ERROR: 404 Not Found — {endpoint} at {base_url} returned 404.")
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        for r in results:
            if limit and len(items) >= limit:
                break
            page_id = str(r.get("id") or r.get("content", {}).get("id") or "")
            title = str(r.get("title") or "")
            if not page_id:
                # Some CQL results wrap under "content"
                content = r.get("content") or {}
                page_id = str(content.get("id") or "")
                title = title or str(content.get("title") or "")
            if page_id:
                items.append(Item(ref=page_id, title=title))
        if limit and len(items) >= limit:
            break
        size = int(data.get("size") or len(results))
        if size < CONFLUENCE_PAGE_SIZE:
            break
        start += size
        if start > 100000:  # safety cutoff
            break
    return items


def enumerate_jira_jql(
    base_url: str, pat: str, jql: str, verify: bool, limiter, limit: int
) -> List[Item]:
    items: List[Item] = []
    start = 0
    while True:
        params = {
            "jql": jql,
            "startAt": start,
            "maxResults": JIRA_PAGE_SIZE,
            "fields": "summary",
        }
        url = f"{base_url}/rest/api/2/search"
        try:
            resp = limiter.request(
                "GET",
                url,
                headers=_atlassian_headers(pat),
                params=params,
                verify=verify,
                timeout=60,
            )
        except RateLimitFailure as e:
            raise SystemExit(f"ERROR: {e}")
        if resp.status_code == 401:
            raise SystemExit("ERROR: 401 Unauthorized. Check atlassian.jira_pat in .wikirc.json.")
        if resp.status_code == 400:
            raise SystemExit(f"ERROR: Jira rejected the JQL — {resp.text[:200]}")
        if resp.status_code == 403:
            raise SystemExit("ERROR: 403 Forbidden — the PAT cannot run this JQL.")
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues") or []
        for issue in issues:
            if limit and len(items) >= limit:
                break
            key = str(issue.get("key") or "")
            summary = str((issue.get("fields") or {}).get("summary") or "")
            if key:
                items.append(Item(ref=key, title=summary))
        if limit and len(items) >= limit:
            break
        got = len(issues)
        total = int(data.get("total") or (start + got))
        start += got
        if got == 0 or start >= total:
            break
        if start > 100000:
            break
    return items


def determine_query(args) -> Tuple[str, str]:
    picks = [bool(args.space), bool(args.cql), bool(args.jql)]
    if sum(picks) != 1:
        raise SystemExit(
            "ERROR: provide exactly one of --space, --cql, --jql"
        )
    if args.space:
        return "confluence_space", args.space
    if args.cql:
        return "confluence_cql", args.cql
    return "jira_jql", args.jql


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover items for bulk ingest")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--space", help="Confluence space key")
    parser.add_argument("--cql", help="Confluence CQL query")
    parser.add_argument("--jql", help="Jira JQL query")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite an existing job with the same (kind, query)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum items to enumerate (0 = no limit, useful for testing)",
    )
    args = parser.parse_args()

    kind, query = determine_query(args)

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())
    limiter = get_limiter("atlassian", cfg.atlassian)
    verify = cfg.atlassian_verify_ssl()

    # Detect an existing job for the same (kind, query)
    existing = find_matching(cfg.wiki_root, kind, query)
    if existing and not args.replace:
        try:
            q = load_queue(cfg.wiki_root, existing)
            counts = q.counts()
        except Exception:  # noqa: BLE001
            counts = {}
        print(
            json.dumps(
                {
                    "job_id": existing,
                    "kind": kind,
                    "query": query,
                    "reused": True,
                    "counts": counts,
                    "note": "A queue for this (kind, query) already exists. Pass --replace to overwrite, or use `/ingest --resume <job_id>` to continue prefetch.",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    # Enumerate
    if kind == "confluence_space":
        base = cfg.confluence_base_url()
        pat = cfg.confluence_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.confluence_base_url or confluence_pat is empty in .wikirc.json."
            )
        items = enumerate_confluence_space(base, pat, query, verify, limiter, args.limit)
    elif kind == "confluence_cql":
        base = cfg.confluence_base_url()
        pat = cfg.confluence_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.confluence_base_url or confluence_pat is empty in .wikirc.json."
            )
        items = enumerate_confluence_cql(base, pat, query, verify, limiter, args.limit)
    else:
        base = cfg.jira_base_url()
        pat = cfg.jira_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.jira_base_url or jira_pat is empty in .wikirc.json."
            )
        items = enumerate_jira_jql(base, pat, query, verify, limiter, args.limit)

    if not items:
        print(
            json.dumps(
                {"kind": kind, "query": query, "items": 0, "note": "no items found — nothing to ingest"},
                indent=2,
            )
        )
        return 0

    # Create the queue
    job_id = existing if (existing and args.replace) else make_job_id(kind, query)
    queue = Queue(
        wiki_root=cfg.wiki_root, job_id=job_id, kind=kind, query=query, items=items
    )
    queue.save()

    print(
        json.dumps(
            {
                "job_id": job_id,
                "kind": kind,
                "query": query,
                "queue_path": str(queue.path),
                "counts": queue.counts(),
                "reused": False,
                "replaced": bool(existing and args.replace),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

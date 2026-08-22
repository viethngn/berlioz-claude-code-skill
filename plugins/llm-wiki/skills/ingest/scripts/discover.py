#!/usr/bin/env python3
"""Enumerate a Confluence space / CQL query, a Jira JQL query, or a website and
write a `.wiki-state/bulk-jobs/<id>/queue.json` for prefetch.py to consume.

Usage:
    python3 discover.py --wiki-root PATH --space KEY
    python3 discover.py --wiki-root PATH --cql "space=FOO AND label=onboarding"
    python3 discover.py --wiki-root PATH --jql "project=PROJ AND updated > -30d"
    python3 discover.py --wiki-root PATH --sitemap https://example.com/sitemap.xml
    python3 discover.py --wiki-root PATH --site https://example.com
    python3 discover.py --wiki-root PATH --crawl https://example.com --depth 2 --max-pages 100

Options:
    --replace         Overwrite an existing job with the same (kind, query)
    --limit N         Cap the number of items (useful for testing)
    --include REGEX   Web only: keep URLs matching any pattern (repeatable)
    --exclude REGEX   Web only: drop URLs matching any pattern (repeatable)
    --since DATE      Web only: keep sitemap entries with lastmod >= YYYY-MM-DD
    --ignore-robots   Web only: do not enforce robots.txt

`--site` auto-discovers a sitemap (robots.txt Sitemap: directives first, then
the standard /sitemap.xml locations). When no sitemap exists it prints
`{"status": "needs_bounds", …}` and exits 0 rather than crawling blind — the
caller is expected to ask the user for --depth and --max-pages and re-run with
--crawl.

An existing queue for the same (kind, query) is reused — but only when the
discovery options that shaped it (--include/--exclude/--since/--limit/
--ignore-robots/--depth/--max-pages) are unchanged. If they differ, this prints
`{"status": "options_changed", …}` to stderr and exits 1 rather than silently
handing back a differently-scoped queue; re-run with --replace to rebuild.

Prints a JSON summary to stdout, including the assigned job_id.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, List, Tuple
from urllib.parse import urlparse

from _deps import require

require(["requests"])

from bulk_queue import (
    Item,
    Queue,
    VALID_KINDS,
    find_matching,
    job_options,
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


def _validate_web_args(args) -> None:
    """Fail fast with a friendly message instead of a traceback.

    Called once, before any HTTP request, so a typo in --since or a
    schemeless --site never burns a request (or, for --since, silently
    filters out most of the sitemap instead of erroring — string comparison
    on an unvalidated date is a real footgun: "2026-06-15" < "2026-6-1"
    lexicographically).
    """
    for flag, value in (("--site", args.site), ("--sitemap", args.sitemap), ("--crawl", args.crawl)):
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SystemExit(
                f"ERROR: {flag} {value!r} is not a valid http(s) URL "
                "(missing scheme or host)."
            )
        # Embedded credentials would leak into slugs/filenames and into the
        # committed source.json for every page of the job.
        if parsed.username or parsed.password:
            raise SystemExit(
                f"ERROR: {flag} {value!r} contains embedded credentials "
                "(user:pass@host). Strip them from the URL and put the credentials "
                "in `web.extra_headers` in .wikirc.json instead."
            )

    if args.since:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.since):
            raise SystemExit(
                f"ERROR: --since {args.since!r} must be YYYY-MM-DD (zero-padded), "
                "e.g. --since 2026-06-01."
            )
        try:
            date.fromisoformat(args.since)
        except ValueError as e:
            raise SystemExit(f"ERROR: --since {args.since!r} is not a valid date: {e}")

    if args.crawl:
        if args.depth is None or args.max_pages is None:
            raise SystemExit(
                "ERROR: --crawl requires explicit --depth and --max-pages bounds."
            )
        if args.depth < 0:
            raise SystemExit(f"ERROR: --depth must be >= 0, got {args.depth}.")
        if args.max_pages < 1:
            raise SystemExit(f"ERROR: --max-pages must be >= 1, got {args.max_pages}.")


def enumerate_web(args, cfg) -> List[Item]:
    """Enumerate a website's pages via sitemap or bounded crawl.

    Raises SystemExit(WebNeedsBounds) semantics via the caller: for --site with
    no discoverable sitemap this returns None so main() can emit the
    `needs_bounds` payload instead of guessing crawl limits.
    """
    _validate_web_args(args)
    from web_discover import (  # imported here so Atlassian-only runs stay light
        RobotsCache,
        WebDiscoveryError,
        apply_filters,
        collect_sitemap_urls,
        crawl,
        filter_by_robots,
        find_sitemaps,
        load_robots,
    )
    from web_url import normalize_url, origin_of

    apply_ssl_env("web", cfg.web_verify_ssl())
    limiter = get_limiter("web", cfg.web)
    respect_robots = cfg.web_respect_robots() and not args.ignore_robots

    target = normalize_url(args.sitemap or args.site or args.crawl)
    # Only this origin may receive web.extra_headers (Cookie/Authorization).
    # Discovery legitimately reaches other hosts via cross-origin sitemap
    # entries; those must not get the entry-point site's credentials.
    primary_origin = origin_of(target)
    robots = None
    robots_cache = None
    if respect_robots:
        robots = load_robots(target, cfg, limiter, primary_origin=primary_origin)
        if robots.disallow_all:
            raise SystemExit(
                "ERROR: robots.txt disallows automated access to this site. "
                "Pass --ignore-robots if you have permission to ingest it anyway."
            )
        # Seed the cache with the entry-point origin's robots so it isn't
        # fetched twice. A sitemap can list URLs on other origins, each
        # checked against its own robots.txt lazily as they're encountered.
        robots_cache = RobotsCache(
            cfg, limiter, seed=(primary_origin, robots), primary_origin=primary_origin
        )

    if args.crawl:
        # Bounds and URL shape already validated by _validate_web_args above;
        # the try/except is a backstop against crawl()'s own ValueError so a
        # bad call from elsewhere can never surface as a raw traceback.
        try:
            entries = crawl(
                target,
                cfg,
                limiter,
                depth=args.depth,
                max_pages=args.max_pages,
                robots=robots,
                respect_robots=respect_robots,
            )
        except ValueError as e:
            raise SystemExit(f"ERROR: {e}")
    else:
        # A robots.txt URL isn't a sitemap — it's a pointer to them.
        if args.sitemap and target.lower().endswith("/robots.txt"):
            sitemaps = find_sitemaps(
                target, cfg, limiter, robots=robots, primary_origin=primary_origin
            )
            if not sitemaps:
                raise SystemExit(
                    f"ERROR: {target} names no Sitemap: directive and no sitemap was "
                    "found at the standard locations. Re-run with --crawl <url> "
                    "--depth N --max-pages M."
                )
        elif args.sitemap:
            sitemaps = [target]
        else:
            sitemaps = find_sitemaps(
                target, cfg, limiter, robots=robots, primary_origin=primary_origin
            )
            if not sitemaps:
                return None  # caller emits `needs_bounds`
        try:
            entries = collect_sitemap_urls(
                sitemaps,
                cfg,
                limiter,
                robots_cache=robots_cache if respect_robots else None,
                primary_origin=primary_origin,
            )
        except WebDiscoveryError as e:
            raise SystemExit(f"ERROR: {e}")
        if not entries and args.sitemap:
            raise SystemExit(
                f"ERROR: {target} yielded no page URLs. Check that it is a sitemap "
                "(<urlset> or <sitemapindex>) and that its <loc> entries are pages."
            )

    try:
        entries = apply_filters(
            entries,
            include=args.include or [],
            exclude=args.exclude or [],
            since=args.since,
        )
    except WebDiscoveryError as e:
        raise SystemExit(f"ERROR: {e}")

    # Per-page robots runs AFTER the local filters: an origin whose entries all
    # get filtered out never costs a robots.txt fetch. For --crawl, crawl()
    # already enforced robots as it walked (same-origin, so one parser is right).
    if respect_robots and robots_cache is not None and not args.crawl:
        entries = filter_by_robots(entries, robots_cache)

    items: List[Item] = []
    for entry in entries:
        if args.limit and len(items) >= args.limit:
            break
        items.append(Item(ref=entry["loc"], title=""))
    return items


def canonical_options(args, kind: str) -> dict:
    """The discovery options that determine a queue's *contents*.

    Reuse is keyed on (kind, query), which says nothing about the filters that
    shaped the item list. Persisting these lets a re-run detect that the scope
    changed instead of silently handing back the old queue.
    """
    options = {"limit": int(args.limit or 0)}
    if kind in {"web_sitemap", "web_crawl"}:
        options.update(
            {
                "include": sorted(args.include or []),
                "exclude": sorted(args.exclude or []),
                "since": args.since or None,
                "ignore_robots": bool(args.ignore_robots),
                "depth": args.depth,
                "max_pages": args.max_pages,
            }
        )
    return options


def describe_option_changes(old: dict, new: dict) -> str:
    """Human-readable diff of two canonical option dicts."""
    keys = sorted(set(old) | set(new))
    changes = [
        f"{k}: {old.get(k)!r} → {new.get(k)!r}" for k in keys if old.get(k) != new.get(k)
    ]
    return "; ".join(changes) or "(no visible differences)"


def determine_query(args) -> Tuple[str, str]:
    picks = [
        bool(args.space),
        bool(args.cql),
        bool(args.jql),
        bool(args.sitemap),
        bool(args.site),
        bool(args.crawl),
    ]
    if sum(picks) != 1:
        raise SystemExit(
            "ERROR: provide exactly one of --space, --cql, --jql, --sitemap, --site, --crawl"
        )
    if args.space:
        return "confluence_space", args.space
    if args.cql:
        return "confluence_cql", args.cql
    if args.jql:
        return "jira_jql", args.jql
    if args.crawl:
        return "web_crawl", args.crawl
    # --sitemap and --site both resolve to a sitemap-driven job; keying the
    # queue on the given URL means a re-run of the same command reuses it.
    return "web_sitemap", (args.sitemap or args.site)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover items for bulk ingest")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--space", help="Confluence space key")
    parser.add_argument("--cql", help="Confluence CQL query")
    parser.add_argument("--jql", help="Jira JQL query")
    parser.add_argument("--sitemap", help="Sitemap URL (sitemap.xml, sitemap index, or .gz)")
    parser.add_argument("--site", help="Site URL — auto-discover its sitemap")
    parser.add_argument("--crawl", help="Site URL — crawl it (requires --depth and --max-pages)")
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help="Crawl only: how many link levels below the start URL to follow",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Crawl only: hard cap on the number of pages enumerated",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Web only: keep URLs matching this regex (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Web only: drop URLs matching this regex (repeatable)",
    )
    parser.add_argument(
        "--since",
        help="Web only: keep sitemap entries whose <lastmod> is >= YYYY-MM-DD",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Web only: do not enforce robots.txt (use only with permission)",
    )
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

    is_web = kind in {"web_sitemap", "web_crawl"}
    if is_web:
        limiter = None
        verify = cfg.web_verify_ssl()
    else:
        apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())
        limiter = get_limiter("atlassian", cfg.atlassian)
        verify = cfg.atlassian_verify_ssl()

    # Detect an existing job for the same (kind, query)
    options = canonical_options(args, kind)
    existing = find_matching(cfg.wiki_root, kind, query)
    if existing and not args.replace:
        try:
            q = load_queue(cfg.wiki_root, existing)
            counts = q.counts()
        except Exception:  # noqa: BLE001
            counts = {}

        # Reuse is only safe when the options that shaped the item list are
        # unchanged — otherwise the new --include/--since/etc. would be silently
        # ignored in favor of the old, differently-scoped queue.
        prior_options = job_options(cfg.wiki_root, existing)
        if prior_options != options:
            print(
                json.dumps(
                    {
                        "status": "options_changed",
                        "job_id": existing,
                        "kind": kind,
                        "query": query,
                        "reused": False,
                        "counts": counts,
                        "prior_options": prior_options,
                        "requested_options": options,
                        "changed": describe_option_changes(prior_options, options),
                        "note": (
                            "An existing queue for this (kind, query) was built with "
                            "different discovery options, so reusing it would silently "
                            "ignore the ones you just passed. Re-run with --replace to "
                            "rebuild the queue with the new options, or use "
                            "`/ingest --resume <job_id>` to continue the existing one "
                            "on its original scope."
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1

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
    if is_web:
        items = enumerate_web(args, cfg)
        if items is None:
            # No sitemap anywhere. Refuse to crawl blind — hand the decision
            # back so the caller can ask the user for explicit bounds.
            from web_discover import crawl_delay_for, load_robots
            from web_url import origin_of

            target = origin_of(args.site or "")
            delay = None
            try:
                robots = load_robots(
                    target, cfg, get_limiter("web", cfg.web), primary_origin=target
                )
                delay = crawl_delay_for(robots, cfg.web_user_agent())
            except Exception:  # noqa: BLE001 — advisory only
                pass
            print(
                json.dumps(
                    {
                        "status": "needs_bounds",
                        "kind": kind,
                        "query": query,
                        "site": target,
                        "robots_crawl_delay": delay,
                        "suggested": {"depth": 2, "max_pages": 100},
                        "note": (
                            f"No sitemap found for {target} (checked robots.txt Sitemap: "
                            "directives and the standard /sitemap.xml locations). Ask the "
                            "user for a crawl depth and a page cap, then re-run with "
                            "--crawl <url> --depth N --max-pages M."
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
    elif kind == "confluence_space":
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
        wiki_root=cfg.wiki_root,
        job_id=job_id,
        kind=kind,
        query=query,
        items=items,
        options=options,
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

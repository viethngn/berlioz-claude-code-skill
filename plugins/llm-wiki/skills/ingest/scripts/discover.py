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
    python3 discover.py --wiki-root PATH --refresh

`--refresh` is the odd one out: instead of one query it builds a single queue
(job id "refresh") covering every source the wiki has ever ingested — one item
per `raw/*.source.json`, plus a fresh re-enumeration of every bulk query in
`raw/.bulk-queries.json` so pages *added* upstream are picked up too. Items
already present in raw/ carry `prior_wiki_status="done"`, so an unchanged
refetch doesn't queue a pointless re-synthesis. An unfinished refresh is
reported as `{"status": "resumable"}` and continued rather than rebuilt; past
`REFRESH_CONFIRM_THRESHOLD` items it reports `{"status": "needs_confirmation"}`
so the caller can check with the user before a long fetch.

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
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from _deps import require

require(["requests"])

import requests

from bulk_queue import (
    BulkSourcesError,
    Item,
    Queue,
    VALID_KINDS,
    find_matching,
    job_options,
    load_bulk_sources,
    load_queue,
    make_job_id,
    record_bulk_query,
)
from config import ConfigError, apply_ssl_env, load_config
from list_sources import build_manifest
from rate_limiter import RateLimitFailure, get_limiter


CONFLUENCE_PAGE_SIZE = 50
JIRA_PAGE_SIZE = 50

# The one queue a refresh writes. Fixed (not timestamped like a bulk job id) so
# a re-run finds and resumes the same refresh instead of piling up queues.
REFRESH_JOB_ID = "refresh"
# Above this many items, refresh discovery stops and asks rather than launching
# what could be hours of API calls. Mirrors the `needs_bounds` handshake.
REFRESH_CONFIRM_THRESHOLD = 200


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
        try:
            resp.raise_for_status()
            data = resp.json()
        except (requests.exceptions.HTTPError, ValueError) as e:
            raise SystemExit(
                f"ERROR: unexpected response from {endpoint} at {base_url} "
                f"(HTTP {resp.status_code}): {e}"
            )
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
        try:
            resp.raise_for_status()
            data = resp.json()
        except (requests.exceptions.HTTPError, ValueError) as e:
            raise SystemExit(
                f"ERROR: unexpected response from Jira search at {base_url} "
                f"(HTTP {resp.status_code}): {e}"
            )
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


def _build_parser() -> argparse.ArgumentParser:
    """The CLI. Also used to re-parse a registry entry's rebuilt flags during a
    refresh, so a replayed query goes through exactly the same validation
    (_validate_web_args, determine_query, canonical_options) as a typed one."""
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
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Build ONE queue covering every source this wiki has ever ingested "
            "(instead of a single query): each raw/*.source.json plus a fresh "
            "re-enumeration of every recorded bulk query"
        ),
    )
    return parser


def _limiter_and_verify(cfg, is_web: bool):
    """Rate limiter + TLS-verify setting for the right backend."""
    if is_web:
        return None, cfg.web_verify_ssl()
    apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())
    return get_limiter("atlassian", cfg.atlassian), cfg.atlassian_verify_ssl()


def enumerate_for(kind: str, query: str, args, cfg, limiter, verify) -> Optional[List[Item]]:
    """Enumerate one bulk query's items.

    Returns None only for the `--site`-with-no-sitemap case, which the caller
    turns into a `needs_bounds` handshake rather than crawling blind.
    """
    if kind in {"web_sitemap", "web_crawl"}:
        return enumerate_web(args, cfg)
    if kind == "confluence_space":
        base, pat = cfg.confluence_base_url(), cfg.confluence_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.confluence_base_url or confluence_pat is empty in .wikirc.json."
            )
        return enumerate_confluence_space(base, pat, query, verify, limiter, args.limit)
    if kind == "confluence_cql":
        base, pat = cfg.confluence_base_url(), cfg.confluence_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.confluence_base_url or confluence_pat is empty in .wikirc.json."
            )
        return enumerate_confluence_cql(base, pat, query, verify, limiter, args.limit)
    if kind == "jira_jql":
        base, pat = cfg.jira_base_url(), cfg.jira_pat()
        if not base or not pat:
            raise SystemExit(
                "ERROR: atlassian.jira_base_url or jira_pat is empty in .wikirc.json."
            )
        return enumerate_jira_jql(base, pat, query, verify, limiter, args.limit)
    raise SystemExit(f"ERROR: unknown bulk kind: {kind}")


def _needs_bounds_payload(kind: str, query: str, args, cfg) -> dict:
    """`--site` found no sitemap: hand the crawl-bounds decision back."""
    from web_discover import crawl_delay_for, load_robots
    from web_url import origin_of

    target = origin_of(args.site or "")
    delay = None
    try:
        robots = load_robots(target, cfg, get_limiter("web", cfg.web), primary_origin=target)
        delay = crawl_delay_for(robots, cfg.web_user_agent())
    except Exception:  # noqa: BLE001 — advisory only
        pass
    return {
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
    }


def _refresh_items_from_sources(manifest: dict) -> List[Item]:
    """One Item per individually-ingested source in the manifest.

    Every one of these already has a raw file, so `prior_wiki_status="done"`:
    if the refetch comes back unchanged there is nothing new to synthesize, and
    prefetch.py restores that instead of queueing the page again.
    """
    items: List[Item] = []
    for src in manifest.get("sources") or []:
        items.append(
            Item(
                ref=src["ref"],
                title=src.get("title") or "",
                source_kind=src["source_kind"],
                thread_ts=src.get("thread_ts"),
                prior_wiki_status="done",
            )
        )
    return items


def _enumerate_registered_queries(
    manifest: dict, args, cfg
) -> Tuple[List[Item], List[dict], List[dict]]:
    """Re-run every recorded bulk query so pages added upstream are caught.

    Returns (items, per_query_report, warnings). Each query's stored flags are
    re-parsed through this script's own parser, so a replayed query is
    validated and canonicalized exactly like a typed one — no second
    implementation of the option handling to drift.

    One failing query is reported and skipped; it never aborts the refresh.
    """
    items: List[Item] = []
    report: List[dict] = []
    warnings: List[dict] = []
    parser = _build_parser()

    for entry in manifest.get("bulk_queries") or []:
        kind = entry["kind"]
        query = entry["query"]
        try:
            sub_args = parser.parse_args(
                ["--wiki-root", str(cfg.wiki_root), *entry["discover_args"]]
            )
        except SystemExit as e:  # argparse rejected the rebuilt flags
            warnings.append({"kind": kind, "query": query, "error": f"unreplayable options: {e}"})
            continue

        is_web = kind in {"web_sitemap", "web_crawl"}
        try:
            limiter, verify = _limiter_and_verify(cfg, is_web)
            found = enumerate_for(kind, query, sub_args, cfg, limiter, verify)
        except (SystemExit, RateLimitFailure, requests.exceptions.RequestException) as e:
            warnings.append({"kind": kind, "query": query, "error": str(e)})
            continue
        if found is None:
            # A --site query whose sitemap has since disappeared. Don't start
            # crawling on the user's behalf during an unattended refresh.
            warnings.append(
                {
                    "kind": kind,
                    "query": query,
                    "error": "no sitemap found anymore — re-ingest it explicitly with --crawl bounds",
                }
            )
            continue

        # web_bulk vs web: these refs were robots-filtered during this very
        # enumeration, so prefetch can skip the per-page robots lookup.
        source_kind = "web_bulk" if is_web else ("jira" if kind == "jira_jql" else "confluence")
        for item in found:
            item.source_kind = source_kind
        items.extend(found)
        report.append({"kind": kind, "query": query, "enumerated": len(found)})

    return items, report, warnings


def _merge_refresh_items(
    from_sources: List[Item], from_queries: List[Item]
) -> Tuple[List[Item], int]:
    """Merge both item lists into one, deduped by (source_kind-ish) ref.

    A page ingested individually AND covered by a space query is one item, not
    two — refs are the same identifiers on both sides (page id / issue key /
    URL), which is what makes that collapse possible.

    Returns (items, brand_new_count). "Brand new" means enumerated upstream
    with no raw file yet: those keep `prior_wiki_status=None` so they always
    reach synthesis.
    """
    merged: dict[Tuple[str, str], Item] = {}
    for item in from_sources:
        merged[(item.ref, item.thread_ts or "")] = item

    brand_new = 0
    for item in from_queries:
        key = (item.ref, "")
        existing = merged.get(key)
        if existing is not None:
            # Already known from raw/: keep the raw-derived item (it carries
            # prior_wiki_status="done") but prefer the freshly enumerated title.
            if item.title and not existing.title:
                existing.title = item.title
            continue
        merged[key] = item
        brand_new += 1
    return list(merged.values()), brand_new


def _disappeared_upstream(
    manifest: dict, enumerated_refs: set, query_report: List[dict]
) -> List[dict]:
    """Sources in raw/ that a re-enumeration no longer returns.

    Only meaningful for kinds a bulk query actually covers, and only when at
    least one query of that kind enumerated successfully — otherwise a failed
    query would look like the whole space had been deleted.

    Reported, never acted on: removing the raw file and re-pointing the wiki
    pages that cite it is /lint's job.
    """
    covered_kinds = set()
    for entry in query_report:
        kind = entry["kind"]
        if kind in {"confluence_space", "confluence_cql"}:
            covered_kinds.add("confluence")
        elif kind == "jira_jql":
            covered_kinds.add("jira")
        elif kind in {"web_sitemap", "web_crawl"}:
            covered_kinds.add("web")
    if not covered_kinds:
        return []

    gone: List[dict] = []
    for src in manifest.get("sources") or []:
        if src["source_kind"] not in covered_kinds:
            continue
        if src["ref"] in enumerated_refs:
            continue
        gone.append(
            {
                "source_kind": src["source_kind"],
                "ref": src["ref"],
                "slug": src.get("slug"),
                "title": src.get("title"),
            }
        )
    return gone


def _apply_confirmation_gate(payload: dict, to_fetch: int) -> None:
    """Flip `payload` to needs_confirmation when the fetch would be large.

    Discovery is cheap and the queue is already on disk, so stopping here costs
    nothing — proceeding is `--resume refresh` (an explicit yes) or `--yes`.
    """
    if to_fetch <= REFRESH_CONFIRM_THRESHOLD:
        return
    payload["status"] = "needs_confirmation"
    payload["note"] = (
        f"{to_fetch} sources would be fetched, which can take a long time and many "
        f"API calls (threshold {REFRESH_CONFIRM_THRESHOLD}). Confirm with the user, "
        "then continue with `/ingest --resume refresh` (or re-run with --yes)."
    )


def _cmd_refresh(args) -> int:
    """Build the single queue that covers every source this wiki knows about."""
    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # An unfinished refresh is continued, not thrown away: re-enumerating would
    # reset every already-fetched item to pending and re-download the lot.
    if not args.replace:
        try:
            existing = load_queue(cfg.wiki_root, REFRESH_JOB_ID)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            existing = None
        if existing is not None:
            counts = existing.counts()
            if counts.get("pending_raw") or counts.get("pending_wiki"):
                payload = {
                    "status": "resumable",
                    "job_id": REFRESH_JOB_ID,
                    "kind": "refresh",
                    "counts": counts,
                    "note": (
                        "An unfinished refresh is already on disk. Continuing it "
                        "(`/ingest --resume refresh`) rather than re-enumerating, "
                        "which would re-fetch everything already done. Pass "
                        "--replace to start a fresh refresh instead."
                    ),
                }
                # The gate applies here too. Otherwise a second bare `/ingest`
                # after a `needs_confirmation` would find the queue "resumable"
                # and start the long sweep the user was never asked about.
                _apply_confirmation_gate(payload, counts.get("pending_raw") or 0)
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0

    try:
        manifest = build_manifest(cfg.wiki_root, cfg.raw_dir)
    except BulkSourcesError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    source_items = _refresh_items_from_sources(manifest)
    query_items, query_report, query_warnings = _enumerate_registered_queries(manifest, args, cfg)
    items, brand_new = _merge_refresh_items(source_items, query_items)
    gone = _disappeared_upstream(
        manifest, {i.ref for i in query_items}, query_report
    )

    if not items:
        print(
            json.dumps(
                {
                    "status": "empty",
                    "kind": "refresh",
                    "items": 0,
                    "skipped": manifest["skipped"],
                    "note": (
                        "No sources found to refresh — this wiki has no "
                        "raw/*.source.json files and no recorded bulk queries."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    queue = Queue(
        wiki_root=cfg.wiki_root,
        job_id=REFRESH_JOB_ID,
        kind="refresh",
        query="",
        items=items,
        options={},
    )
    queue.save()

    payload = {
        "job_id": REFRESH_JOB_ID,
        "kind": "refresh",
        "queue_path": str(queue.path),
        "counts": {
            **queue.counts(),
            "known_sources": len(source_items),
            "brand_new_upstream": brand_new,
            "bulk_queries_enumerated": len(query_report),
        },
        "bulk_queries": query_report,
        "disappeared_upstream": gone,
        "skipped": manifest["skipped"],
    }
    if manifest.get("backfilled_bulk_queries"):
        payload["backfilled_bulk_queries"] = manifest["backfilled_bulk_queries"]
    if manifest.get("registry_error"):
        payload["registry_error"] = manifest["registry_error"]
    if query_warnings:
        payload["query_warnings"] = query_warnings
    payload["status"] = "ready"
    _apply_confirmation_gate(payload, len(items))

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.refresh:
        return _cmd_refresh(args)

    kind, query = determine_query(args)

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Probe the registry before spending any API calls: this run will record
    # itself there at the end, and a malformed file must not be discovered only
    # after a whole space has been enumerated.
    try:
        load_bulk_sources(cfg.raw_dir)
    except BulkSourcesError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    is_web = kind in {"web_sitemap", "web_crawl"}
    limiter, verify = _limiter_and_verify(cfg, is_web)

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

        # Register the query even on a plain reuse. Without this, a query first
        # run before the registry existed (or before this feature landed) would
        # never be recorded — and refresh would silently skip that whole space.
        # touch=False so an already-registered query leaves the committed file
        # byte-identical instead of churning it on every no-op discovery.
        try:
            record_bulk_query(cfg.raw_dir, kind, query, options, existing, touch=False)
        except BulkSourcesError as e:
            print(f"WARNING: could not register this query for refresh: {e}", file=sys.stderr)

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
    items = enumerate_for(kind, query, args, cfg, limiter, verify)
    if items is None:
        # No sitemap anywhere. Refuse to crawl blind — hand the decision
        # back so the caller can ask the user for explicit bounds.
        print(json.dumps(_needs_bounds_payload(kind, query, args, cfg), indent=2, ensure_ascii=False))
        return 0

    if not items:
        print(
            json.dumps(
                {"kind": kind, "query": query, "items": 0, "note": "no items found — nothing to ingest"},
                indent=2,
            )
        )
        return 0

    # Carry over wiki_status="done"/"skipped" from the queue being replaced,
    # keyed by ref (page_id / issue key / URL — stable across re-discovery).
    # prefetch.py consumes this: an item whose refetch comes back
    # raw_status="unchanged" keeps this wiki_status instead of resetting to
    # "pending", so a refresh doesn't force re-synthesis of a whole space
    # that didn't actually change. A genuinely new/changed item is
    # unaffected — it keeps the default wiki_status="pending".
    if existing and args.replace:
        try:
            old_queue = load_queue(cfg.wiki_root, existing)
            prior_wiki_by_ref = {
                i.ref: i.wiki_status
                for i in old_queue.items
                if i.wiki_status in {"done", "skipped"}
            }
        except (FileNotFoundError, ValueError):
            prior_wiki_by_ref = {}
        for item in items:
            prior = prior_wiki_by_ref.get(item.ref)
            if prior:
                item.prior_wiki_status = prior

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
    record_bulk_query(cfg.raw_dir, kind, query, options, job_id)

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

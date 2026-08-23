#!/usr/bin/env python3
"""Enumerate every source ever ingested into a wiki — the input to refresh-all.

Pure enumeration from what's already on disk: the committed
`raw/*.source.json` files plus the committed bulk-query registry
(`raw/.bulk-queries.json`). No network calls (beyond a local `Path.exists()`
per local-file source) and nothing is fetched or synthesized here.

`discover.py --refresh` imports `build_manifest()` to turn this into one
resumable queue, which `prefetch.py` then actually fetches and diff-gates. The
CLI below prints the same manifest for debugging.

Every `raw/*.source.json` lands in exactly one bucket — a refresh target under
`sources`, or an entry under `skipped` — so the manifest can be read as a
coverage report. Nothing is dropped silently.

Usage:
    python3 list_sources.py --wiki-root PATH

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from bulk_queue import BulkSourcesError, backfill_bulk_sources, load_bulk_sources
from config import ConfigError, load_config
from web_url import SITEMAP_URL_RE

# `--space FOO` etc. reconstructed from a registry entry, so refresh discovery
# can re-run the query through discover.py's own argument parser (and therefore
# its own validation) instead of reimplementing enumeration.
# web_sitemap is special-cased in _discover_args_for — see SITEMAP_URL_RE.
BULK_FLAG_BY_KIND = {
    "confluence_space": "--space",
    "confluence_cql": "--cql",
    "jira_jql": "--jql",
    "web_crawl": "--crawl",
}

# raw/<slug>.source.json "type" → the fetcher that refreshes it. Anything not
# listed here is reported under skipped.unhandled_type rather than ignored.
KNOWN_TYPES = {"confluence", "jira", "web", "local", "slack"}


def _load_source_jsons(raw_dir: Path) -> tuple[list[dict], list[dict]]:
    """Read every raw/*.source.json. Returns (entries, unreadable)."""
    entries: list[dict] = []
    unreadable: list[dict] = []
    if not raw_dir.exists():
        return entries, unreadable
    for p in sorted(raw_dir.glob("*.source.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            unreadable.append({"slug": p.name, "reason": str(e)})
            continue
        if not isinstance(data, dict) or not data.get("type"):
            unreadable.append({"slug": p.name, "reason": "missing 'type' field"})
            continue
        data["_slug"] = p.name[: -len(".source.json")]
        entries.append(data)
    return entries, unreadable


def _version_key(entry: dict) -> tuple:
    """Sort key for Confluence duplicates — numeric, not lexicographic.

    `version_number` is an int in the source.json, and comparing it as a
    string would rank version 9 above version 10.
    """
    raw = entry.get("version_number")
    try:
        return (1, int(raw))
    except (TypeError, ValueError):
        return (0, 0)


def _float_key(field: str) -> Callable[[dict], tuple]:
    """Sort key for a numeric field stored as a string (Slack timestamps)."""

    def key(entry: dict) -> tuple:
        try:
            return (1, float(entry.get(field)))
        except (TypeError, ValueError):
            return (0, 0.0)

    return key


def _iso_key(field: str) -> Callable[[dict], tuple]:
    """Sort key for an ISO-8601 field, where string order IS chronological."""

    def key(entry: dict) -> tuple:
        value = entry.get(field)
        return (1, str(value)) if value else (0, "")

    return key


def _dedup_by_stable_id(
    entries: list[dict],
    id_key: str,
    newest_first: Optional[Callable[[dict], tuple]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Collapse entries that describe the same upstream item.

    Returns (kept, dropped_duplicates, unusable). An entry with no `id_key` is
    *unusable*, not silently discarded — there's nothing stable to refresh it
    by, and the user needs to know it exists.

    Duplicates happen because a retitled Confluence page (or a Jira issue whose
    summary changed) leaves its old raw/<slug>.source.json behind — the
    fetchers mint a new slug rather than deleting the superseded file. Dedup
    here so a refresh doesn't fetch the same page twice.
    """
    groups: dict[str, list[dict]] = {}
    unusable: list[dict] = []
    for e in entries:
        key = e.get(id_key)
        if key is None or key == "":
            unusable.append(
                {"slug": e.get("_slug"), "reason": f"no {id_key} — nothing stable to refresh by"}
            )
            continue
        groups.setdefault(str(key), []).append(e)

    kept: list[dict] = []
    dropped: list[dict] = []
    for _key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=newest_first or (lambda e: (0, str(e.get("_slug") or ""))))
        winner = group[-1]
        kept.append(winner)
        for loser in group[:-1]:
            dropped.append(
                {
                    "slug": loser.get("_slug"),
                    "reason": f"duplicate {id_key}, superseded by {winner.get('_slug')}",
                }
            )
    return kept, dropped, unusable


def _source(source_kind: str, ref, entry: dict, **extra) -> dict:
    """One refresh target. `ref` is what the fetcher is invoked with.

    Refs are deliberately the *same* identifiers bulk discovery produces —
    page id, issue key, URL — so a page ingested both individually and via a
    space query collapses to one queue item instead of being fetched twice.
    """
    return {
        "source_kind": source_kind,
        "ref": str(ref),
        "slug": entry.get("_slug"),
        "title": entry.get("title") or "",
        **extra,
    }


# Each _*_sources() returns (targets, {skip_bucket: entries}). Distinct buckets
# because the reasons need distinct advice: a duplicate wants /lint, a missing
# original wants the file back, an excluded search wants a manual re-ingest.
Buckets = dict


def _confluence_sources(entries: list[dict]) -> tuple[list[dict], Buckets]:
    kept, dropped, unusable = _dedup_by_stable_id(entries, "page_id", _version_key)
    return (
        [_source("confluence", e["page_id"], e) for e in kept],
        {"dropped_duplicates": dropped, "unusable_source_json": unusable},
    )


def _jira_sources(entries: list[dict]) -> tuple[list[dict], Buckets]:
    kept, dropped, unusable = _dedup_by_stable_id(entries, "key", _iso_key("updated_at"))
    return (
        [_source("jira", e["key"], e) for e in kept],
        {"dropped_duplicates": dropped, "unusable_source_json": unusable},
    )


def _web_sources(entries: list[dict]) -> tuple[list[dict], Buckets]:
    kept, dropped, unusable = _dedup_by_stable_id(entries, "url", None)
    return (
        [_source("web", e["url"], e) for e in kept],
        {"dropped_duplicates": dropped, "unusable_source_json": unusable},
    )


def _local_sources(entries: list[dict]) -> tuple[list[dict], Buckets]:
    kept, dropped, unusable = _dedup_by_stable_id(entries, "original_path", None)
    ok: list[dict] = []
    missing: list[dict] = []
    for e in kept:
        original_path = e["original_path"]
        if not Path(original_path).exists():
            # Normal on a fresh clone: original_path is absolute and belongs to
            # whichever machine ingested it. Report, never block the run.
            missing.append({"slug": e.get("_slug"), "original_path": original_path})
            continue
        ok.append(_source("local", original_path, e))
    return ok, {
        "dropped_duplicates": dropped,
        "unusable_source_json": unusable,
        "local_missing_original": missing,
    }


def _slack_sources(entries: list[dict]) -> tuple[list[dict], Buckets]:
    """Channels (one item per channel) and threads (one item each).

    Channels carry no date window: fetch_slack.py resumes from its own
    watermark, which is exact to the microsecond, where a `--after` date would
    re-ingest up to a day of messages already in the wiki. Threads refetch by
    (channel_id, thread_ts), which is deterministic and picks up new replies.
    Ad hoc searches are excluded — their result set shifts over time, so
    re-running one would rewrite a raw file with different messages than the
    wiki cited.
    """
    sources: list[dict] = []
    searches: list[dict] = []
    unusable: list[dict] = []
    channels: dict[str, list[dict]] = {}

    for e in entries:
        if e.get("search_query"):
            searches.append(
                {
                    "slug": e.get("_slug"),
                    "reason": "ad hoc search — results shift over time, so re-running it "
                    "would rewrite this raw file with a different message set",
                }
            )
            continue
        channel_id = str(e.get("channel_id") or "")
        if not channel_id:
            unusable.append({"slug": e.get("_slug"), "reason": "no channel_id"})
            continue
        if e.get("thread_ts"):
            sources.append(
                _source("slack_thread", channel_id, e, thread_ts=str(e["thread_ts"]))
            )
            continue
        # Many raw shards can cover one channel (each run's slug encodes the
        # message date range it captured); they collapse to a single
        # incremental refresh, which picks up from the newest watermark.
        channels.setdefault(channel_id, []).append(e)

    for channel_id, shards in channels.items():
        shards.sort(key=_float_key("fetched_until"))
        newest = shards[-1]
        sources.append(
            _source(
                "slack_channel",
                channel_id,
                newest,
                # Every shard this one target stands in for, so the manifest
                # still accounts for each raw file rather than appearing to
                # lose the older shards.
                covers_slugs=sorted(str(s.get("_slug")) for s in shards),
            )
        )
    return sources, {"slack_searches": searches, "unusable_source_json": unusable}


def _discover_args_for(kind: str, query: str, options: dict) -> list[str]:
    """Rebuild the `discover.py` flags that produced a recorded bulk query.

    No `--replace`: refresh merges everything into one queue rather than
    rebuilding each query's own queue, so it never discards a paused job.
    """
    if kind == "web_sitemap":
        # The registry stores the resolved query string, not which flag was
        # originally passed. An exact sitemap/robots.txt URL came from
        # --sitemap; a bare site URL came from --site (auto-discover).
        flag = "--sitemap" if SITEMAP_URL_RE.search(query) else "--site"
    else:
        flag = BULK_FLAG_BY_KIND.get(kind)
    if not flag:
        return []
    args = [flag, query]
    limit = options.get("limit")
    if limit:
        args += ["--limit", str(limit)]
    if kind in {"web_sitemap", "web_crawl"}:
        for inc in options.get("include") or []:
            args += ["--include", inc]
        for exc in options.get("exclude") or []:
            args += ["--exclude", exc]
        if options.get("since"):
            args += ["--since", options["since"]]
        if options.get("ignore_robots"):
            args += ["--ignore-robots"]
        if kind == "web_crawl":
            if options.get("depth") is not None:
                args += ["--depth", str(options["depth"])]
            if options.get("max_pages") is not None:
                args += ["--max-pages", str(options["max_pages"])]
    return args


def _bulk_queries(raw_dir: Path) -> tuple[list[dict], list[dict]]:
    queries: list[dict] = []
    skipped: list[dict] = []
    for entry in load_bulk_sources(raw_dir):
        kind = entry.get("kind")
        query = entry.get("query") or ""
        args = _discover_args_for(kind, query, entry.get("options") or {})
        if not args:
            skipped.append({"kind": kind, "query": query, "reason": "no replayable discovery flags"})
            continue
        queries.append(
            {
                "kind": kind,
                "query": query,
                "options": entry.get("options") or {},
                "discover_args": args,
            }
        )
    return queries, skipped


def build_manifest(wiki_root: Path, raw_dir: Path, backfill: bool = True) -> dict:
    """Every source this wiki knows about, plus everything deliberately skipped.

    `backfill=True` first recovers bulk queries that exist only as a local job
    queue (see backfill_bulk_sources) — a wiki that ingested a space before the
    registry existed would otherwise have its whole space skipped by a refresh.
    """
    entries, unreadable = _load_source_jsons(raw_dir)

    by_type: dict[str, list[dict]] = {}
    unhandled_type: list[dict] = []
    for e in entries:
        source_type = str(e.get("type"))
        if source_type not in KNOWN_TYPES:
            unhandled_type.append({"slug": e.get("_slug"), "type": source_type})
            continue
        by_type.setdefault(source_type, []).append(e)

    sources: list[dict] = []
    skipped: dict[str, list[dict]] = {
        "dropped_duplicates": [],
        "unusable_source_json": [],
        "local_missing_original": [],
        "slack_searches": [],
        "unhandled_type": unhandled_type,
        "unreadable_source_json": unreadable,
        "unreplayable_bulk_queries": [],
    }
    for source_type, fn in (
        ("confluence", _confluence_sources),
        ("jira", _jira_sources),
        ("web", _web_sources),
        ("local", _local_sources),
        ("slack", _slack_sources),
    ):
        got, buckets = fn(by_type.get(source_type, []))
        sources.extend(got)
        for name, entries_ in buckets.items():
            skipped[name].extend(entries_)

    registry_error: Optional[str] = None
    backfilled: list[dict] = []
    bulk_queries: list[dict] = []
    try:
        if backfill:
            backfilled = backfill_bulk_sources(wiki_root, raw_dir)
        bulk_queries, unreplayable = _bulk_queries(raw_dir)
        skipped["unreplayable_bulk_queries"].extend(unreplayable)
    except BulkSourcesError as e:
        # A broken registry costs us the bulk half of the refresh; it must not
        # cost us the individually-ingested sources too.
        registry_error = str(e)

    manifest = {
        "wiki_root": str(wiki_root),
        "counts": {
            "sources_total": len(sources),
            **{
                f"sources_{kind}": sum(1 for s in sources if s["source_kind"] == kind)
                for kind in ("confluence", "jira", "web", "local", "slack_channel", "slack_thread")
            },
            "bulk_queries": len(bulk_queries),
            "source_json_files": len(entries) + len(unreadable),
            "skipped_total": sum(len(v) for v in skipped.values()),
        },
        "sources": sources,
        "bulk_queries": bulk_queries,
        "skipped": skipped,
    }
    if backfilled:
        manifest["backfilled_bulk_queries"] = backfilled
    if registry_error:
        manifest["registry_error"] = registry_error
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enumerate every source ever ingested (individual sources + bulk queries)"
    )
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="Do not register bulk queries found only in local .wiki-state job queues",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    manifest = build_manifest(cfg.wiki_root, cfg.raw_dir, backfill=not args.no_backfill)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

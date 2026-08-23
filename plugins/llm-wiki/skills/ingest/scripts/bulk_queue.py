"""Persisted job queue for bulk ingest.

A queue lives at .wiki-state/bulk-jobs/<job-id>/queue.json. Every mutation
is written back atomically (tempfile + rename). Every reader can load,
mutate, save; concurrent writers are not supported (single-process
prefetch.py is the intended writer).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# Kinds that name a *bulk query* — these are the ones recorded in the
# registry and re-enumerated on refresh.
VALID_QUERY_KINDS = {
    "confluence_space",
    "confluence_cql",
    "jira_jql",
    "web_sitemap",
    "web_crawl",
}
# "refresh" is not a query — it's the single heterogeneous queue built by
# `discover.py --refresh`, whose items each carry their own source_kind.
VALID_KINDS = VALID_QUERY_KINDS | {"refresh"}
VALID_RAW_STATUS = {"pending", "done", "unchanged", "failed"}
VALID_WIKI_STATUS = {"pending", "done", "skipped"}

# Per-item fetcher selection, used by a "refresh" queue where one queue holds
# Confluence pages, Jira issues, web pages, local files and Slack together.
# `web` vs `web_bulk`: a page re-enumerated from a sitemap/crawl was already
# robots-filtered at discovery time, so prefetch skips the per-page robots
# lookup for it; an individually-ingested page was not, so it keeps the check.
VALID_SOURCE_KINDS = {
    "confluence",
    "jira",
    "web",
    "web_bulk",
    "local",
    "slack_channel",
    "slack_thread",
}


def state_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / ".wiki-state" / "bulk-jobs"


# What make_job_id() actually produces — a leading alnum char, else '-' would
# strip and hyphens/alnum after. Validated centrally here (not just at each
# CLI's argument parser) because Path's / operator doesn't normalize '..', so
# an unvalidated job_id joined into a path can escape .wiki-state/bulk-jobs/
# entirely — e.g. queue_admin.py's `delete` subcommand shutil.rmtree()s
# whatever job_dir() returns.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def job_dir(wiki_root: Path, job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise ValueError(
            f"invalid job_id {job_id!r} — expected only letters, digits, and "
            "hyphens (this is what make_job_id() produces; anything else is "
            "refused before it's joined into a filesystem path)"
        )
    return state_dir(wiki_root) / job_id


def queue_path(wiki_root: Path, job_id: str) -> Path:
    return job_dir(wiki_root, job_id) / "queue.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_job_id(kind: str, query: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", query).strip("-").lower()
    slug = slug[:40] or "job"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    prefix = {
        "confluence_space": "conf-space",
        "confluence_cql": "conf-cql",
        "jira_jql": "jira-jql",
        "web_sitemap": "web-sitemap",
        "web_crawl": "web-crawl",
    }.get(kind, "job")
    return f"{prefix}-{slug}-{stamp}"


@dataclass
class Item:
    ref: str
    title: str = ""
    slug: Optional[str] = None
    raw_status: str = "pending"
    wiki_status: str = "pending"
    fetch_attempts: int = 0
    last_error: Optional[str] = None
    images_new: int = 0
    images_changed: int = 0
    # Set when a re-discovery carries this ref's wiki_status forward: by
    # --replace from the queue it replaces, or by --refresh for any ref that
    # already has a raw/<slug>.source.json. prefetch.py restores wiki_status
    # from this when the refetch comes back "unchanged", so a refresh doesn't
    # force re-synthesis of pages that didn't actually change — and clears it
    # the moment a fetch comes back changed.
    prior_wiki_status: Optional[str] = None
    # Which fetcher handles this item, for a heterogeneous "refresh" queue.
    # None means "use queue.kind", which is how every bulk query queue works.
    source_kind: Optional[str] = None
    # Slack threads only: identifies the thread to refetch. A narrow typed
    # field rather than a free-form arg list, because this file is persisted
    # and then fed to a subprocess.
    thread_ts: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "ref": self.ref,
            "title": self.title,
            "slug": self.slug,
            "raw_status": self.raw_status,
            "wiki_status": self.wiki_status,
            "fetch_attempts": self.fetch_attempts,
            "images_new": self.images_new,
            "images_changed": self.images_changed,
        }
        if self.last_error:
            d["last_error"] = self.last_error
        if self.prior_wiki_status:
            d["prior_wiki_status"] = self.prior_wiki_status
        if self.source_kind:
            d["source_kind"] = self.source_kind
        if self.thread_ts:
            d["thread_ts"] = self.thread_ts
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        prior_wiki_status = d.get("prior_wiki_status")
        if prior_wiki_status not in {"done", "skipped"}:
            prior_wiki_status = None
        # Whitelist rather than trust: source_kind picks which script runs and
        # thread_ts is passed to it as an argument.
        source_kind = d.get("source_kind")
        if source_kind not in VALID_SOURCE_KINDS:
            source_kind = None
        thread_ts = d.get("thread_ts")
        thread_ts = str(thread_ts) if thread_ts else None
        return cls(
            ref=str(d.get("ref") or ""),
            title=str(d.get("title") or ""),
            slug=d.get("slug"),
            raw_status=str(d.get("raw_status") or "pending"),
            wiki_status=str(d.get("wiki_status") or "pending"),
            fetch_attempts=int(d.get("fetch_attempts") or 0),
            last_error=d.get("last_error"),
            images_new=int(d.get("images_new") or 0),
            images_changed=int(d.get("images_changed") or 0),
            prior_wiki_status=prior_wiki_status,
            source_kind=source_kind,
            thread_ts=thread_ts,
        )


class Queue:
    def __init__(
        self,
        wiki_root: Path,
        job_id: str,
        kind: str,
        query: str,
        items: List[Item],
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        options: Optional[dict] = None,
    ):
        self.wiki_root = Path(wiki_root)
        self.job_id = job_id
        self.kind = kind
        self.query = query
        self.items = items
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at
        # Canonical discovery options this queue was built with. Queue reuse is
        # keyed on (kind, query), which doesn't capture filters — so without
        # this, re-running with a different --include silently returns the old,
        # differently-scoped queue.
        self.options = dict(options or {})

    @property
    def path(self) -> Path:
        return queue_path(self.wiki_root, self.job_id)

    def counts(self) -> dict:
        total = len(self.items)
        raw_done = sum(1 for i in self.items if i.raw_status in {"done", "unchanged"})
        wiki_done = sum(1 for i in self.items if i.wiki_status == "done")
        failed = sum(1 for i in self.items if i.raw_status == "failed")
        pending_raw = sum(1 for i in self.items if i.raw_status == "pending")
        pending_wiki = sum(
            1
            for i in self.items
            if i.raw_status in {"done", "unchanged"} and i.wiki_status == "pending"
        )
        return {
            "total": total,
            "raw_done": raw_done,
            "wiki_done": wiki_done,
            "failed": failed,
            "pending_raw": pending_raw,
            "pending_wiki": pending_wiki,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.job_id,
            "kind": self.kind,
            "query": self.query,
            "options": self.options,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "counts": self.counts(),
            "items": [i.to_dict() for i in self.items],
        }

    def save(self) -> None:
        self.updated_at = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via tempfile + rename
        fd, tmp_path = tempfile.mkstemp(
            prefix="queue.", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def find(self, ref: str) -> Optional[Item]:
        for item in self.items:
            if item.ref == ref:
                return item
        return None

    def pending_raw(self) -> List[Item]:
        return [i for i in self.items if i.raw_status in {"pending", "failed"}]

    def pending_wiki(self) -> List[Item]:
        return [
            i
            for i in self.items
            if i.raw_status in {"done", "unchanged"} and i.wiki_status == "pending"
        ]


def load_queue(wiki_root: Path, job_id: str) -> Queue:
    p = queue_path(wiki_root, job_id)
    if not p.exists():
        raise FileNotFoundError(f"queue not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return Queue(
        wiki_root=wiki_root,
        job_id=str(data.get("id") or job_id),
        kind=str(data.get("kind") or ""),
        query=str(data.get("query") or ""),
        items=[Item.from_dict(x) for x in (data.get("items") or [])],
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        options=data.get("options") or {},
    )


def list_jobs(wiki_root: Path) -> List[dict]:
    """Return light metadata for every queue found on disk, newest first."""
    base = state_dir(wiki_root)
    if not base.exists():
        return []
    out = []
    for entry in sorted(base.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        p = entry / "queue.json"
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        counts = data.get("counts") or {}
        out.append(
            {
                "id": data.get("id") or entry.name,
                "kind": data.get("kind"),
                "query": data.get("query"),
                "options": data.get("options") or {},
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "counts": counts,
            }
        )
    return out


def find_matching(wiki_root: Path, kind: str, query: str) -> Optional[str]:
    """Return an existing job id whose (kind, query) matches, if any."""
    for meta in list_jobs(wiki_root):
        if meta.get("kind") == kind and meta.get("query") == query:
            return meta.get("id")
    return None


def job_options(wiki_root: Path, job_id: str) -> dict:
    """Return the canonical discovery options a queue was built with."""
    for meta in list_jobs(wiki_root):
        if meta.get("id") == job_id:
            return meta.get("options") or {}
    return {}


# --- Durable, git-committed registry of bulk queries -----------------------
#
# Unlike .wiki-state/bulk-jobs/ (git-ignored, per-machine), this file lives
# under raw/ so ingest.py's existing `git add raw wiki` commits it
# automatically. It's what lets refresh-all rediscover which bulk queries
# (space/CQL/JQL/site/sitemap) were ever run against this wiki, even from a
# fresh clone where .wiki-state/ never existed.
#
# Dot-prefixed on purpose: the wiki's own CLAUDE.md tells Claude never to edit
# anything in raw/, because raw/ holds immutable source documents. This is
# plugin-managed metadata, not a source document, and the leading dot keeps it
# out of the raw/*.md and raw/*.source.json globs that every other script
# walks. `git add raw` still picks up dotfiles, so it is committed normally.

BULK_SOURCES_FILENAME = ".bulk-queries.json"


class BulkSourcesError(Exception):
    """raw/.bulk-queries.json exists but could not be understood.

    Raised instead of quietly returning [] — an empty list would make
    record_bulk_query() overwrite the file with a single entry, silently
    discarding every query the wiki had registered.
    """


def bulk_sources_path(raw_dir: Path) -> Path:
    return Path(raw_dir) / BULK_SOURCES_FILENAME


def load_bulk_sources(raw_dir: Path) -> List[dict]:
    """Return the registered bulk queries. Raises BulkSourcesError if unreadable."""
    p = bulk_sources_path(raw_dir)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise BulkSourcesError(f"{p} could not be read: {e}") from e
    except json.JSONDecodeError as e:
        raise BulkSourcesError(
            f"{p} is not valid JSON ({e}). Fix or delete it — refusing to "
            "overwrite it and lose the queries it holds."
        ) from e
    if not isinstance(data, dict):
        raise BulkSourcesError(
            f"{p} should hold a JSON object with a 'queries' list, found "
            f"{type(data).__name__}. Fix or delete it."
        )
    queries = data.get("queries") or []
    if not isinstance(queries, list):
        raise BulkSourcesError(f"{p}: 'queries' should be a list, found {type(queries).__name__}.")
    out: List[dict] = []
    for entry in queries:
        if not isinstance(entry, dict):
            raise BulkSourcesError(f"{p}: every entry in 'queries' should be an object.")
        if entry.get("kind") not in VALID_QUERY_KINDS:
            raise BulkSourcesError(
                f"{p}: entry {entry.get('query')!r} has unknown kind "
                f"{entry.get('kind')!r} (expected one of {sorted(VALID_QUERY_KINDS)})."
            )
        out.append(entry)
    return out


def _save_bulk_sources(raw_dir: Path, entries: List[dict]) -> None:
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = bulk_sources_path(raw_dir)
    # Dot-prefixed temp name too, so a concurrent raw/ walk never sees a
    # half-written file and a crashed run leaves no visible litter in raw/.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".bulk-queries.", suffix=".json", dir=str(raw_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "queries": entries},
                f,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            f.write("\n")
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_bulk_query(
    raw_dir: Path,
    kind: str,
    query: str,
    options: dict,
    job_id: str,
    touch: bool = True,
) -> None:
    """Upsert one (kind, query) entry into raw/.bulk-queries.json.

    `touch=True` (the enumerate-and-save path) refreshes options and the
    last-run stamps. `touch=False` (the plain-reuse path) registers a query
    that isn't recorded yet but leaves an existing entry byte-identical — so
    re-running a query that's already registered doesn't churn a committed
    file on every no-op discovery.
    """
    entries = load_bulk_sources(raw_dir)
    now = _now()
    for e in entries:
        if e.get("kind") == kind and e.get("query") == query:
            if not touch:
                return  # already registered — leave the file untouched
            e["options"] = options
            e["last_job_id"] = job_id
            e["last_run_at"] = now
            break
    else:
        entries.append(
            {
                "kind": kind,
                "query": query,
                "options": options,
                "first_job_id": job_id,
                "first_seen_at": now,
                "last_job_id": job_id,
                "last_run_at": now,
            }
        )
    _save_bulk_sources(raw_dir, entries)


def backfill_bulk_sources(wiki_root: Path, raw_dir: Path) -> List[dict]:
    """Register any bulk query that only exists as a local job queue.

    The registry was introduced after bulk ingest, and it's only written when
    discovery actually runs — so a wiki that ingested a space before this
    existed has the job queue in (git-ignored) .wiki-state/bulk-jobs/ but no
    registry entry, which would make a refresh silently skip that whole space.
    Recover those here.

    Returns the entries that were added (empty when nothing was missing).
    """
    known = {(e.get("kind"), e.get("query")) for e in load_bulk_sources(raw_dir)}
    added: List[dict] = []
    for meta in list_jobs(wiki_root):
        kind, query = meta.get("kind"), meta.get("query")
        if kind not in VALID_QUERY_KINDS or not query:
            continue  # "refresh" queues and malformed metas are not queries
        if (kind, query) in known:
            continue
        record_bulk_query(
            raw_dir,
            kind,
            query,
            meta.get("options") or {},
            meta.get("id") or "",
            touch=False,
        )
        known.add((kind, query))
        added.append({"kind": kind, "query": query, "from_job_id": meta.get("id")})
    return added

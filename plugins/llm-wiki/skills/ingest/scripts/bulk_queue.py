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


VALID_KINDS = {
    "confluence_space",
    "confluence_cql",
    "jira_jql",
    "web_sitemap",
    "web_crawl",
}
VALID_RAW_STATUS = {"pending", "done", "unchanged", "failed"}
VALID_WIKI_STATUS = {"pending", "done", "skipped"}


def state_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / ".wiki-state" / "bulk-jobs"


def job_dir(wiki_root: Path, job_id: str) -> Path:
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
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
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
    ):
        self.wiki_root = Path(wiki_root)
        self.job_id = job_id
        self.kind = kind
        self.query = query
        self.items = items
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at

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

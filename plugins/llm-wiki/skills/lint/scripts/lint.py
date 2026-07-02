#!/usr/bin/env python3
"""Deterministic wiki linter — stdlib only.

Scans wiki/*.md and emits a JSON report of:
    orphans, broken_links, missing_pages, format_violations,
    stale_pages, unsourced_claims, edges (wiki-link graph)

Usage:
    python3 lint.py --wiki-root PATH [--stale-days DAYS] [--sources]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


WIKI_LINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
H1_RE = re.compile(r"^#\s+.+", re.MULTILINE)

REQUIRED_FIELDS = ("Summary", "Sources", "Last updated")
EXEMPT_ORPHANS = {"index", "log", "README", "readme"}
NEEDS_VERIFICATION_RE = re.compile(r"\(?source:\s*needs verification\)?", re.IGNORECASE)


def load_page(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "text": text}


def wiki_links_in(text: str) -> List[str]:
    return [m.group(1).strip() for m in WIKI_LINK_RE.finditer(text)]


def has_field(text: str, name: str) -> bool:
    pattern = rf"^\*\*{re.escape(name)}\*\*\s*:"
    return bool(re.search(pattern, text, re.MULTILINE))


def parse_last_updated(text: str) -> Optional[datetime]:
    m = re.search(r"\*\*Last updated\*\*\s*:\s*(\S+)", text)
    if not m:
        return None
    raw = m.group(1).strip().rstrip(",")
    md = DATE_RE.search(raw)
    if not md:
        return None
    try:
        return datetime.strptime(md.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_sources(text: str) -> List[str]:
    m = re.search(r"\*\*Sources\*\*\s*:\s*(.+?)(?=\n\*\*|\n---|\n#|\Z)", text, re.S)
    if not m:
        return []
    block = m.group(1)
    entries: List[str] = []
    for line in block.splitlines():
        line = line.strip().lstrip("-").lstrip("*").strip()
        if not line or line.lower() == "(none yet)":
            continue
        entries.append(line)
    return entries


def slug_from_path(path: Path) -> str:
    return path.stem


def collect_pages(wiki_dir: Path) -> List[Path]:
    if not wiki_dir.exists():
        return []
    return sorted(p for p in wiki_dir.glob("*.md") if p.is_file())


def build_edges(pages: List[dict]) -> Dict[str, List[str]]:
    edges: Dict[str, List[str]] = {}
    for page in pages:
        slug = slug_from_path(page["path"])
        targets = sorted(set(wiki_links_in(page["text"])))
        edges[slug] = targets
    return edges


def inbound_counts(edges: Dict[str, List[str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for _, targets in edges.items():
        for t in targets:
            counts[t] = counts.get(t, 0) + 1
    return counts


def find_orphans(pages: List[dict], inbound: Dict[str, int]) -> List[str]:
    orphans = []
    for page in pages:
        slug = slug_from_path(page["path"])
        if slug in EXEMPT_ORPHANS:
            continue
        if inbound.get(slug, 0) == 0:
            orphans.append(slug)
    return sorted(orphans)


def find_broken_links(
    pages: List[dict],
    edges: Dict[str, List[str]],
) -> Tuple[List[dict], Dict[str, List[str]]]:
    existing = {slug_from_path(p["path"]) for p in pages}
    broken: List[dict] = []
    missing: Dict[str, List[str]] = {}
    for slug, targets in edges.items():
        for t in targets:
            if t not in existing:
                broken.append({"from": slug, "to": t})
                missing.setdefault(t, []).append(slug)
    return broken, missing


def find_format_violations(pages: List[dict]) -> List[dict]:
    violations: List[dict] = []
    for page in pages:
        text = page["text"]
        missing = []
        if not H1_RE.search(text):
            missing.append("H1 title (# ...)")
        for field in REQUIRED_FIELDS:
            if not has_field(text, field):
                missing.append(field)
        if missing:
            violations.append(
                {"page": slug_from_path(page["path"]), "missing": missing}
            )
    return violations


def find_stale(
    pages: List[dict],
    raw_dir: Path,
    stale_days: int,
) -> List[dict]:
    now = datetime.now(timezone.utc)
    stale: List[dict] = []
    raw_files: List[dict] = []
    if raw_dir.exists():
        for p in raw_dir.iterdir():
            if p.is_file() and p.suffix.lower() == ".md":
                raw_files.append(
                    {
                        "name": p.name,
                        "stem": p.stem,
                        "mtime": datetime.fromtimestamp(
                            p.stat().st_mtime, tz=timezone.utc
                        ),
                    }
                )

    for page in pages:
        text = page["text"]
        last_updated = parse_last_updated(text)
        if last_updated is None:
            continue
        age_days = (now - last_updated).days
        if age_days < stale_days:
            continue

        slug = slug_from_path(page["path"])
        related_raws: List[str] = []
        for rf in raw_files:
            if rf["mtime"] <= last_updated:
                continue
            if slug in rf["stem"] or any(
                token in rf["stem"] for token in slug.split("-") if len(token) > 3
            ):
                related_raws.append(rf["name"])

        if not related_raws:
            continue

        stale.append(
            {
                "page": slug,
                "last_updated": last_updated.date().isoformat(),
                "age_days": age_days,
                "newer_raw_files": sorted(related_raws),
            }
        )
    return stale


def find_unsourced(pages: List[dict]) -> List[dict]:
    unsourced: List[dict] = []
    for page in pages:
        text = page["text"]
        sources = parse_sources(text)
        needs_marker_count = len(NEEDS_VERIFICATION_RE.findall(text))
        if not sources or needs_marker_count > 0:
            unsourced.append(
                {
                    "page": slug_from_path(page["path"]),
                    "empty_sources": not sources,
                    "needs_verification_markers": needs_marker_count,
                }
            )
    return unsourced


def scan_raw(raw_dir: Path) -> List[dict]:
    if not raw_dir.exists():
        return []
    out: List[dict] = []
    for p in sorted(raw_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".md":
            out.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "stem": p.stem,
                    "mtime": datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic wiki linter")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--stale-days", type=int, default=90)
    parser.add_argument(
        "--sources",
        action="store_true",
        help="Include a scan of raw/ files (path, mtime) in the report",
    )
    parser.add_argument(
        "--wiki-dir",
        default=None,
        help="Override wiki subdir name (defaults to 'wiki' or .wikirc.json value)",
    )
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="Override raw subdir name (defaults to 'raw' or .wikirc.json value)",
    )
    args = parser.parse_args()

    wiki_root = args.wiki_root.resolve()

    wiki_subdir = args.wiki_dir or "wiki"
    raw_subdir = args.raw_dir or "raw"

    config_path = wiki_root / ".wikirc.json"
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            wiki_subdir = args.wiki_dir or cfg.get("wiki_dir") or wiki_subdir
            raw_subdir = args.raw_dir or cfg.get("raw_dir") or raw_subdir
        except (json.JSONDecodeError, OSError):
            pass

    wiki_dir = wiki_root / wiki_subdir
    raw_dir = wiki_root / raw_subdir

    if not wiki_dir.exists():
        print(f"ERROR: wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    page_paths = collect_pages(wiki_dir)
    pages = [load_page(p) for p in page_paths]

    edges = build_edges(pages)
    inbound = inbound_counts(edges)
    orphans = find_orphans(pages, inbound)
    broken, missing = find_broken_links(pages, edges)
    format_violations = find_format_violations(pages)
    stale = find_stale(pages, raw_dir, args.stale_days)
    unsourced = find_unsourced(pages)

    report = {
        "wiki_root": str(wiki_root),
        "wiki_dir": str(wiki_dir),
        "raw_dir": str(raw_dir),
        "page_count": len(pages),
        "edges": edges,
        "inbound_counts": inbound,
        "orphans": orphans,
        "broken_links": broken,
        "missing_pages": dict(sorted(missing.items())),
        "format_violations": format_violations,
        "stale_pages": stale,
        "unsourced_claims": unsourced,
    }

    if args.sources:
        report["raw_sources"] = scan_raw(raw_dir)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

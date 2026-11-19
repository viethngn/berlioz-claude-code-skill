#!/usr/bin/env python3
"""Deterministic wiki bookkeeping — stdlib only. JSON on stdout.

Usage:
    python3 wiki_tools.py locate     --wiki-root PATH TERM [TERM ...]
    python3 wiki_tools.py citing     --wiki-root PATH RAW_SLUG
    python3 wiki_tools.py index-sync --wiki-root PATH
    python3 wiki_tools.py log        --wiki-root PATH --op ingest|lint|log --title TEXT [--bullet TEXT ...]

`index`, `log`, README and journal pages (`YYYY-MM-DD`, `YYYY-MM-index`, owned
by the `log` skill) are never located, cited, or added/removed/moved in
`index.md`; its `## Diary` section is never modified.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lint import (  # noqa: E402
    ARCHIVE_SUBDIR,
    EXEMPT_PAGES,
    WIKI_LINK_RE,
    archived_slugs,
    cited_raw_slugs,
    collect_pages,
    is_journal_slug,
    link_target,
    load_page,
    page_body,
    resolve_dirs,
    slug_from_path,
)

SUMMARY_RE = re.compile(r"^\*\*Summary\*\*\s*:\s*(.*)$", re.MULTILINE)
H1_RE = re.compile(r"^# (.*)$", re.MULTILINE)
LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
LOG_OPS = ("ingest", "lint", "log")
W_TITLE, W_SUMMARY, W_BODY = 3, 2, 1


def _summary(text: str) -> str:
    m = SUMMARY_RE.search(text)
    return m.group(1).strip() if m else ""


def _content_pages(wiki_dir: Path) -> List[dict]:
    """Active pages minus index/log/README and journal pages."""
    pages = []
    for path in collect_pages(wiki_dir):
        slug = slug_from_path(path)
        if slug not in EXEMPT_PAGES and not is_journal_slug(slug):
            pages.append(load_page(path))
    return pages


def locate(wiki_dir: Path, terms: List[str], limit: int = 10) -> List[dict]:
    """Rank pages by case-insensitive substring hits: slug/H1 > Summary > body."""
    terms = [t.lower() for t in terms if t.strip()]
    hits = []
    for page in _content_pages(wiki_dir):
        text = page["text"]
        slug = slug_from_path(page["path"])
        h1 = H1_RE.search(text)
        # Hyphens in slugs read as spaces so "rate limit" matches `api-rate-limit`.
        title = f"{slug} {h1.group(1) if h1 else ''}".lower().replace("-", " ").replace("_", " ")
        summary = _summary(text)
        body = page_body(text).lower()
        score = sum(
            W_TITLE * (t in title or t in slug.lower())
            + W_SUMMARY * (t in summary.lower())
            + W_BODY * (t in body)
            for t in terms
        )
        if score:
            hits.append({"page": slug, "summary": summary, "score": score})
    hits.sort(key=lambda h: (-h["score"], h["page"]))
    return hits[:limit]


def citing(wiki_dir: Path, raw_slug: str, raw_dir: Optional[Path] = None) -> List[str]:
    """Sorted slugs of content pages that cite raw/<raw_slug>."""
    raw_dir = raw_dir or wiki_dir.parent / "raw"
    return sorted(
        slug_from_path(p["path"])
        for p in _content_pages(wiki_dir)
        if raw_slug in cited_raw_slugs([p], raw_dir)
    )


def _insert_in_section(lines: List[str], heading: str, new: List[str], before: Optional[str] = None) -> None:
    """Append `new` at the end of section `heading`; create it (before `before`, else at the end) if absent."""
    if heading in lines:
        end = next((i for i in range(lines.index(heading) + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while lines[end - 1].strip() == "" and end - 1 > lines.index(heading):
            end -= 1
        lines[end:end] = new
        return
    at = lines.index(before) if before in lines else len(lines)
    while at > 0 and lines[at - 1].strip() == "":
        at -= 1
    block = ["", heading, *new]
    if at < len(lines):
        block.append("")
    lines[at:at] = block


def index_sync(wiki_dir: Path) -> dict:
    """Add unlisted pages under `## Uncategorized`, drop list lines to deleted
    pages, and move lines to archived pages under `## Archive`."""
    index_path = wiki_dir / "index.md"
    original = index_path.read_text(encoding="utf-8") if index_path.exists() else "# Wiki Index\n"
    lines = original.splitlines()
    existing = {slug_from_path(p) for p in collect_pages(wiki_dir)}
    archived = archived_slugs(wiki_dir)
    content = {slug_from_path(p["path"]): p["text"] for p in _content_pages(wiki_dir)}

    kept: List[str] = []
    to_archive: List[str] = []
    removed: List[str] = []
    moved: List[str] = []
    in_diary = False
    for line in lines:
        if line.startswith("## "):
            in_diary = line.strip() == "## Diary"
        targets = {t for t in map(link_target, (m.group(1) for m in WIKI_LINK_RE.finditer(line))) if t}
        if in_diary or not LIST_LINE_RE.match(line) or len(targets) != 1:
            kept.append(line)
            continue
        target = targets.pop()
        if target in existing or target.startswith(ARCHIVE_SUBDIR + "/") or is_journal_slug(target):
            kept.append(line)
        elif target in archived:
            moved.append(target)
            to_archive.append(WIKI_LINK_RE.sub(
                lambda m: m.group(0).replace("[[", f"[[{ARCHIVE_SUBDIR}/", 1)
                if link_target(m.group(1)) == target else m.group(0),
                line,
            ))
        else:
            removed.append(target)

    listed = {t for t in map(link_target, (m.group(1) for m in WIKI_LINK_RE.finditer("\n".join(kept)))) if t}
    added = sorted(set(content) - listed)
    if added:
        new = [f"- [[{s}]] — {_summary(content[s])}" if _summary(content[s]) else f"- [[{s}]]" for s in added]
        _insert_in_section(kept, "## Uncategorized", new, before="## Archive")
    if to_archive:
        _insert_in_section(kept, "## Archive", to_archive)

    text = "\n".join(kept) + "\n"
    if text != original:
        index_path.write_text(text, encoding="utf-8")
    return {"added": added, "removed": sorted(set(removed)), "archived": sorted(set(moved))}


def append_log(wiki_dir: Path, op: str, title: str, bullets: List[str], today: Optional[str] = None) -> None:
    """Insert a dated entry directly under log.md's H1 (newest first)."""
    if op not in LOG_OPS:
        raise ValueError(f"unknown op {op!r}; expected one of {', '.join(LOG_OPS)}")
    day = today or date.today().isoformat()
    header = f"## {day}" if op == "ingest" else f"## {day} ({op})"
    log_path = wiki_dir / "log.md"
    lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    h1 = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1 is None:
        lines.insert(0, "# Wiki Log")
        h1 = 0
    block = ["", header, f"- {title}", *(f"- {b}" for b in bullets)]
    if h1 + 1 < len(lines) and lines[h1 + 1].strip():
        block.append("")
    lines[h1 + 1:h1 + 1] = block
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--wiki-root", type=Path, required=True)
    parser = argparse.ArgumentParser(description="Deterministic wiki bookkeeping")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("locate", parents=[common], help="Find existing pages by term")
    p.add_argument("terms", nargs="+")
    p = sub.add_parser("citing", parents=[common], help="Pages citing raw/<RAW_SLUG>")
    p.add_argument("raw_slug")
    sub.add_parser("index-sync", parents=[common], help="Sync wiki/index.md with the pages on disk")
    p = sub.add_parser("log", parents=[common], help="Prepend an entry to wiki/log.md")
    p.add_argument("--op", choices=LOG_OPS, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--bullet", action="append", default=[])
    args = parser.parse_args()

    wiki_dir, raw_dir = resolve_dirs(args.wiki_root.resolve())
    if not wiki_dir.exists():
        print(f"ERROR: wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1
    if args.cmd == "locate":
        result = locate(wiki_dir, args.terms)
    elif args.cmd == "citing":
        result = citing(wiki_dir, args.raw_slug, raw_dir)
    elif args.cmd == "index-sync":
        result = index_sync(wiki_dir)
    else:
        append_log(wiki_dir, args.op, args.title, args.bullet)
        result = {"log": str(wiki_dir / "log.md")}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

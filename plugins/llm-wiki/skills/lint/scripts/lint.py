#!/usr/bin/env python3
"""Deterministic wiki linter — stdlib only.

Scans wiki/*.md and emits a JSON report of:
    orphans, broken_links, missing_pages, archive_links_without_banner,
    format_violations, empty_pages, stale_pages, unsourced_claims,
    status_pages, missing_sources, empty_raw, edges (wiki-link graph)

Pages carrying `**Status**: Archived` or `**Status**: Superseded by [[...]]`
are excluded from the orphan and stale checks (intentionally retired).

Pages under `wiki/archive/` are the primary archival namespace: they are NOT
collected as active pages (so they're never re-flagged as orphan/stale), but
their slugs ARE resolvable link targets so references into the archive don't
read as broken links.

`empty_raw` lists raw source files (`raw/<slug>.md`) whose body is empty/stub
AND which no wiki page cites — safe-to-prune candidates. The linter only
REPORTS them; the /lint skill deletes them after user confirmation.

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
# A raw/ path token inside a Sources entry — stops at backtick, comma, quote,
# whitespace, or closing bracket/paren so multi-path lines split correctly.
PATH_TOKEN_RE = re.compile(r"raw/[^\s`,'\"\)\]]+")

# Body length (non-whitespace chars, after the --- separator) below which a page
# is treated as empty/stub.
EMPTY_BODY_CHARS = 40
# Placeholder phrases that mark a page as an unfilled stub regardless of length.
STUB_MARKERS = (
    "placeholder — add content",
    "placeholder - add content",
    "tbd — mentioned in",
    "tbd - mentioned in",
)
# Status values that retire a page from orphan/stale nagging.
RETIRED_STATUSES = ("archived", "superseded")
# Subdirectory under wiki/ holding archived pages. Files here are NOT linted as
# active pages, but their slugs are valid link targets (references into the
# archive are not broken links).
ARCHIVE_SUBDIR = "archive"


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


def parse_status(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (status, superseded_by_slug) from an optional **Status**: line.

    Absent field → (None, None), treated as Active. Recognizes:
        **Status**: Active
        **Status**: Archived
        **Status**: Superseded by [[current-page]]
    """
    m = re.search(r"\*\*Status\*\*\s*:\s*(.+)", text)
    if not m:
        return None, None
    value = m.group(1).strip()
    lowered = value.lower()
    if lowered.startswith("superseded"):
        link = WIKI_LINK_RE.search(value)
        return "superseded", (link.group(1).strip() if link else None)
    if lowered.startswith("archived"):
        return "archived", None
    if lowered.startswith("active"):
        return "active", None
    # Unknown value — surface it as-is so the semantic pass can inspect it.
    return lowered.split()[0] if lowered else None, None


def page_body(text: str) -> str:
    """Return the page body after the first `---` separator (or whole text)."""
    parts = text.split("\n---", 1)
    return parts[1] if len(parts) == 2 else text


def body_after_h1(text: str) -> str:
    """Return the text after the first H1 line (`# ...`).

    Raw source files have no `---` front-matter separator — they're just an H1
    title followed by the body — so `page_body` (which keys off `---`) would
    count the title. This strips only the first H1 line and returns the rest,
    which is what "empty body" means for a raw container page.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("# "):
            return "\n".join(lines[i + 1:])
    return text


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


def find_status_pages(pages: List[dict]) -> List[dict]:
    """Report pages carrying an explicit **Status** field."""
    out: List[dict] = []
    for page in pages:
        status, superseded_by = parse_status(page["text"])
        if status is None:
            continue
        out.append(
            {
                "page": slug_from_path(page["path"]),
                "status": status,
                "superseded_by": superseded_by,
            }
        )
    return out


def retired_slugs(pages: List[dict]) -> set:
    """Slugs of pages whose Status retires them from orphan/stale nagging."""
    retired = set()
    for page in pages:
        status, _ = parse_status(page["text"])
        if status in RETIRED_STATUSES:
            retired.add(slug_from_path(page["path"]))
    return retired


def find_orphans(
    pages: List[dict], inbound: Dict[str, int], retired: Optional[set] = None
) -> List[str]:
    retired = retired or set()
    orphans = []
    for page in pages:
        slug = slug_from_path(page["path"])
        if slug in EXEMPT_ORPHANS or slug in retired:
            continue
        if inbound.get(slug, 0) == 0:
            orphans.append(slug)
    return sorted(orphans)


def find_broken_links(
    pages: List[dict],
    edges: Dict[str, List[str]],
    archived: Optional[set] = None,
) -> Tuple[List[dict], Dict[str, List[str]]]:
    """Report `[[link]]` targets that resolve to no page.

    Archived pages (under `wiki/archive/`) are not in `pages`, but a link to
    one is legitimate — so their slugs are added to the `existing` set. Both a
    bare slug (`[[old-page]]`) and an `archive/`-prefixed slug
    (`[[archive/old-page]]`) resolve.
    """
    archived = archived or set()
    existing = {slug_from_path(p["path"]) for p in pages}
    existing |= archived
    existing |= {f"{ARCHIVE_SUBDIR}/{s}" for s in archived}
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


def find_empty_pages(pages: List[dict]) -> List[dict]:
    """Flag pages with an empty/stub body (too short, or a placeholder marker)."""
    empty: List[dict] = []
    for page in pages:
        slug = slug_from_path(page["path"])
        if slug in EXEMPT_ORPHANS:
            continue
        body = page_body(page["text"])
        lowered = body.lower()
        marker = next((m for m in STUB_MARKERS if m in lowered), None)
        non_ws = re.sub(r"\s+", "", body)
        if marker:
            empty.append(
                {"page": slug, "reason": "stub placeholder", "char_count": len(non_ws)}
            )
        elif len(non_ws) < EMPTY_BODY_CHARS:
            empty.append(
                {"page": slug, "reason": "body too short", "char_count": len(non_ws)}
            )
    return empty


def find_missing_sources(pages: List[dict], raw_dir: Path) -> List[dict]:
    """Flag Sources entries pointing at raw/ paths that don't exist on disk.

    A single Sources line may list several paths (comma- and/or backtick-
    separated), e.g. `raw/a.md`, `raw/b.md` — extract each path token
    individually and check it, rather than treating the whole line as one path.
    """
    wiki_root = raw_dir.parent
    out: List[dict] = []
    for page in pages:
        missing: List[str] = []
        for entry in parse_sources(page["text"]):
            for candidate in PATH_TOKEN_RE.findall(entry):
                candidate = candidate.strip()
                # Only check entries that look like a raw/ path.
                if not candidate.startswith("raw/"):
                    continue
                if ".." in Path(candidate).parts:
                    # Never resolve a traversal attempt against the real
                    # filesystem — treat it as an invalid Sources entry
                    # rather than a read-oracle for paths outside raw/.
                    missing.append(candidate)
                    continue
                if not (wiki_root / candidate).exists():
                    missing.append(candidate)
        if missing:
            out.append(
                {"page": slug_from_path(page["path"]), "missing": sorted(set(missing))}
            )
    return out


def cited_raw_slugs(pages: List[dict]) -> set:
    """Return the set of raw slugs that ANY wiki page depends on.

    A raw file is "cited" if `raw/<slug>.md` (or an image under
    `raw/images/<slug>/`) is referenced anywhere in a page's text — the Sources
    block, an inline image link, or prose. We scan the whole page text (not just
    Sources) so inline references also protect a raw from pruning.

    Tokens are matched exactly and normalized to the slug: `raw/foo.md` →
    `foo`, `raw/images/foo/0.png` → `foo`. Exact-token extraction avoids
    Jira slug-prefix collisions (`proj-12` vs `proj-123`).
    """
    cited: set = set()
    for page in pages:
        for token in PATH_TOKEN_RE.findall(page["text"]):
            token = token.strip()
            if not token.startswith("raw/"):
                continue
            rest = token[len("raw/"):]
            if rest.startswith("images/"):
                # raw/images/<slug>/<file> → slug is the first path segment
                parts = rest[len("images/"):].split("/", 1)
                slug = parts[0]
            else:
                # raw/<slug>.md (or .source.json) → strip a trailing extension
                first = rest.split("/", 1)[0]
                slug = re.sub(r"\.(md|source\.json|json)$", "", first)
            if slug:
                cited.add(slug)
    return cited


def find_empty_raw(raw_dir: Path, cited: set) -> List[dict]:
    """Report raw/<slug>.md files that are empty/stub AND uncited — prunable.

    Uses the same empty/stub heuristic as `find_empty_pages` (body after the H1
    below `EMPTY_BODY_CHARS` non-whitespace chars, or a `STUB_MARKERS` phrase).
    A raw is only reported if its slug is NOT in `cited`, so no wiki citation is
    ever broken. Each entry lists the companion files to delete atomically:
    the `.md`, its `.source.json`, and (flagged) any `raw/images/<slug>/` dir.

    The linter only reports; the /lint skill deletes after user confirmation.
    """
    if not raw_dir.exists():
        return []
    wiki_root = raw_dir.parent
    raw_name = raw_dir.name
    out: List[dict] = []
    for p in sorted(raw_dir.glob("*.md")):
        if not p.is_file():
            continue
        slug = p.stem
        if slug in cited:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        body = body_after_h1(text)
        lowered = body.lower()
        marker = next((m for m in STUB_MARKERS if m in lowered), None)
        non_ws = re.sub(r"\s+", "", body)
        if marker:
            reason = "stub placeholder"
        elif len(non_ws) < EMPTY_BODY_CHARS:
            reason = "body too short"
        else:
            continue

        source_json = raw_dir / f"{slug}.source.json"
        images_dir = raw_dir / "images" / slug
        companions = [f"{raw_name}/{p.name}"]
        if source_json.exists():
            companions.append(f"{raw_name}/{source_json.name}")
        out.append(
            {
                "slug": slug,
                "reason": reason,
                "char_count": len(non_ws),
                "companions": companions,
                "has_images_dir": images_dir.is_dir(),
            }
        )
    return out


def archived_slugs(wiki_dir: Path) -> set:
    """Slugs of pages living under `wiki/archive/` (the archival namespace).

    These are not linted as active pages, but their slugs remain valid link
    targets so references into the archive don't read as broken links.
    """
    archive_dir = wiki_dir / ARCHIVE_SUBDIR
    if not archive_dir.exists():
        return set()
    return {p.stem for p in archive_dir.glob("*.md") if p.is_file()}


def find_unbannered_archive_links(
    edges: Dict[str, List[str]], wiki_dir: Path, archived: set
) -> List[dict]:
    """Inbound links into wiki/archive/* whose target has no Superseded-by
    banner — informational, not an error.

    find_broken_links() deliberately treats every archived slug as a valid
    target (a bare reference into the archive is a legitimate historical
    citation, not a broken link) — which is the right default, but it means a
    forgotten link-repoint after archiving a page is otherwise invisible
    forever. This surfaces that case without reversing the leniency: it flags
    links whose target lacks a Superseded-by/Archived banner, which is the
    signal that repointing was actually intended.
    """
    archive_dir = wiki_dir / ARCHIVE_SUBDIR
    prefix = f"{ARCHIVE_SUBDIR}/"
    banner_cache: Dict[str, bool] = {}

    def archived_slug_of(target: str) -> Optional[str]:
        if target in archived:
            return target
        if target.startswith(prefix) and target[len(prefix):] in archived:
            return target[len(prefix):]
        return None

    def has_banner(slug: str) -> bool:
        if slug not in banner_cache:
            path = archive_dir / f"{slug}.md"
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            status, _ = parse_status(text)
            banner_cache[slug] = status in RETIRED_STATUSES
        return banner_cache[slug]

    out: List[dict] = []
    for slug, targets in edges.items():
        for t in targets:
            archived_slug = archived_slug_of(t)
            if archived_slug and not has_banner(archived_slug):
                out.append({"from": slug, "to": t, "archived_slug": archived_slug})
    return out


def find_stale(
    pages: List[dict],
    raw_dir: Path,
    stale_days: int,
    retired: Optional[set] = None,
) -> List[dict]:
    retired = retired or set()
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
        slug = slug_from_path(page["path"])
        if slug in retired:
            continue
        last_updated = parse_last_updated(text)
        if last_updated is None:
            continue
        age_days = (now - last_updated).days
        if age_days < stale_days:
            continue

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

    archived = archived_slugs(wiki_dir)
    edges = build_edges(pages)
    inbound = inbound_counts(edges)
    retired = retired_slugs(pages)
    orphans = find_orphans(pages, inbound, retired)
    broken, missing = find_broken_links(pages, edges, archived)
    unbannered_archive_links = find_unbannered_archive_links(edges, wiki_dir, archived)
    format_violations = find_format_violations(pages)
    stale = find_stale(pages, raw_dir, args.stale_days, retired)
    unsourced = find_unsourced(pages)
    empty_pages = find_empty_pages(pages)
    status_pages = find_status_pages(pages)
    missing_sources = find_missing_sources(pages, raw_dir)
    cited = cited_raw_slugs(pages)
    empty_raw = find_empty_raw(raw_dir, cited)

    report = {
        "wiki_root": str(wiki_root),
        "wiki_dir": str(wiki_dir),
        "raw_dir": str(raw_dir),
        "page_count": len(pages),
        "archived_count": len(archived),
        "edges": edges,
        "inbound_counts": inbound,
        "orphans": orphans,
        "broken_links": broken,
        "missing_pages": dict(sorted(missing.items())),
        "archive_links_without_banner": unbannered_archive_links,
        "format_violations": format_violations,
        "empty_pages": empty_pages,
        "stale_pages": stale,
        "unsourced_claims": unsourced,
        "status_pages": status_pages,
        "missing_sources": missing_sources,
        "empty_raw": empty_raw,
    }

    if args.sources:
        report["raw_sources"] = scan_raw(raw_dir)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

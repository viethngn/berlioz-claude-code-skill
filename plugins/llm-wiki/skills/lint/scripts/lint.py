#!/usr/bin/env python3
"""Deterministic wiki linter — stdlib only.

Scans wiki/*.md and emits a JSON report of:
    orphans, broken_links, missing_pages, archive_links_without_banner,
    format_violations, empty_pages, source_drift, unsourced_claims,
    status_pages, missing_sources, empty_raw, overlap_candidates,
    fact_collisions, warnings; plus edges / inbound_counts (wiki-link graph)
    with --graph

`index`, `log` and README pages (EXEMPT_PAGES) are skipped by every per-page
check, and their links do not count as inbound links for orphan detection.
Journal pages (`YYYY-MM-DD`, `YYYY-MM-index`, written by the `log` skill) skip
the same judgment checks, but their links count as inbound and are checked for
broken targets. Link targets drop `#anchor`; cross-wiki `[[label/slug]]` links
are never resolved (see `link_target`).

Pages carrying `**Status**: Archived` or `**Status**: Superseded by [[...]]`
are excluded from the orphan check (intentionally retired).

Pages under `wiki/archive/` are the primary archival namespace: they are NOT
collected as active pages (so they're never re-flagged as orphans), but
their slugs ARE resolvable link targets so references into the archive don't
read as broken links.

`empty_raw` lists raw source files (`raw/<slug>.md`) whose body is empty/stub
AND which no wiki page cites — safe-to-prune candidates. The linter only
REPORTS them; the /lint skill deletes them after user confirmation.

Usage:
    python3 lint.py --wiki-root PATH [--sources] [--graph] [--max-candidates N]
                    [--full | --since-last-lint] [--fix [--allow-dirty]]
    python3 lint.py --wiki-root PATH --mark-reviewed < reviewed.json

`--fix` (see `apply_fixes`) deletes stub-placeholder pages, unlinks links to
them (dropping their index lines) and repairs a missing Sources path by exact
stem, then reports on the fixed wiki under the extra key `fixed`. It refuses
(exit 2) outside a git work tree or with uncommitted wiki changes.

`wiki/.lint-reviewed.json` remembers reviewed overlap pairs, collisions and
acknowledged drift by content hash; a matching item is left out of the report
(counted under `suppressed`) until the page or raw content changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


# Overlap / fact-collision tuning. Score = weighted sum of components in [0, 1];
# a shared source alone (W_SOURCES) must stay below OVERLAP_THRESHOLD.
MAX_CANDIDATES = 20
OVERLAP_THRESHOLD = 0.5
SENTENCE_SIM_THRESHOLD = 0.8
W_SOURCES = 0.2
W_LINKS = 0.1
W_SLUG = 0.2
W_TEXT = 0.5
SLUG_TOKEN_MIN = 4
SHINGLE = 5  # character n-gram size for page-level text_sim
MAX_BUCKET = 50  # index keys shared by more pages generate no pairs (warned)
MAX_COLLISIONS = 50
MAX_EXACT_COLLISIONS = 35  # exact-pass share of MAX_COLLISIONS while fuzzy hits exist
COLLISION_SCAN_LIMIT = 20 * MAX_COLLISIONS  # per pass (exact, fuzzy); past it the warning says "at least"

WIKI_LINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|([^\]]+))?\]\]")  # group 2: alias text
H1_RE = re.compile(r"^#\s+.+", re.MULTILINE)

REQUIRED_FIELDS = ("Summary", "Sources", "Last updated")
EXEMPT_PAGES = {"index", "log", "README", "readme"}
# Append-only journal written by the `log` skill: day-pages and monthly rollups.
JOURNAL_SLUG_RE = re.compile(r"^\d{4}-\d{2}-(?:\d{2}|index)$")
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
# Status values that retire a page from orphan nagging.
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


def is_journal_slug(slug: str) -> bool:
    return bool(JOURNAL_SLUG_RE.fullmatch(slug))


def skip_judgment(slug: str) -> bool:
    """Exempt and journal pages get no per-page judgment checks."""
    return slug in EXEMPT_PAGES or is_journal_slug(slug)


def link_target(raw: str) -> Optional[str]:
    """Resolvable page slug of a wiki-link target: `#anchor` stripped; None for
    a cross-wiki link (`label/slug`, not `archive/...`) or a pure `[[#anchor]]`.
    The `archive` segment matches in any case and is returned lower-case."""
    target = raw.split("#", 1)[0].strip()
    if "/" in target:
        head, rest = target.split("/", 1)
        if head.lower() != ARCHIVE_SUBDIR:
            return None
        target = f"{ARCHIVE_SUBDIR}/{rest}"
    return target or None


def resolved_links(text: str) -> set:
    return {t for t in map(link_target, wiki_links_in(text)) if t}


def has_field(text: str, name: str) -> bool:
    pattern = rf"^\*\*{re.escape(name)}\*\*\s*:"
    return bool(re.search(pattern, text, re.MULTILINE))


SOURCES_BLOCK_RE = re.compile(r"\*\*Sources\*\*\s*:\s*(.+?)(?=\n\*\*|\n---|\n#|\Z)", re.S)


def parse_sources(text: str) -> List[str]:
    m = SOURCES_BLOCK_RE.search(text)
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


# Lines allowed before the header `---`: the H1, blanks, `**Field**:` lines,
# their list-item continuations, and a blockquote banner.
HEADER_LINE_RE = re.compile(r"^(?:#\s.*|\*\*[^*]+\*\*\s*:.*|\s*[-*+]\s.*|\s*>.*|\s*)$")


def page_body(text: str) -> str:
    """Return the page body: after the first `---` when everything before it is
    header material, else everything after the H1 (a `---` in prose is a rule)."""
    head, sep, rest = text.partition("\n---")
    if sep and all(HEADER_LINE_RE.match(line) for line in head.splitlines()):
        return rest
    return body_after_h1(text)


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
        targets = sorted(resolved_links(page["text"]))
        edges[slug] = targets
    return edges


def inbound_counts(edges: Dict[str, List[str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for src, targets in edges.items():
        # Hub pages link everything; a link from them doesn't de-orphan a page.
        if src in EXEMPT_PAGES:
            continue
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
    """Slugs of pages whose Status retires them from orphan nagging."""
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
        if skip_judgment(slug) or slug in retired:
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
        if skip_judgment(slug_from_path(page["path"])):
            continue
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
        if skip_judgment(slug):
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


def is_traversal(path: str) -> bool:
    """True when a path has a `..` segment (would escape raw/)."""
    return ".." in Path(path.strip()).parts


def traversal_source_paths(text: str) -> List[str]:
    """Return the Sources `raw/...` tokens that contain a `..` segment."""
    return [
        t.strip()
        for entry in parse_sources(text)
        for t in PATH_TOKEN_RE.findall(entry)
        if is_traversal(t)
    ]


def resolve_source_paths(text: str, raw_dir: Path) -> List[str]:
    """Return the wiki-root-relative raw paths cited in a page's Sources block.

    Explicit `raw/...` tokens are kept as-is, except ones with a `..` segment,
    which are dropped (see `traversal_source_paths`). A bare filename
    (`api-guide.md`) resolves to `<raw_dir name>/<name>` (normally
    `raw/<name>`) only when that file exists in raw_dir; other bare words are
    dropped.
    """
    out: List[str] = []
    for entry in parse_sources(text):
        out.extend(
            t.strip() for t in PATH_TOKEN_RE.findall(entry) if not is_traversal(t)
        )
        for piece in re.split(r"[\s,]+", entry):
            name = piece.strip("`'\"()[]")
            if name and "/" not in name and (raw_dir / name).is_file():
                out.append(f"{raw_dir.name}/{name}")
    return out


def find_missing_sources(pages: List[dict], raw_dir: Path) -> List[dict]:
    """Flag Sources entries pointing at raw/ paths that don't exist on disk.

    A single Sources line may list several paths (comma- and/or backtick-
    separated), e.g. `raw/a.md`, `raw/b.md` — extract each path token
    individually and check it, rather than treating the whole line as one path.
    """
    wiki_root = raw_dir.parent
    out: List[dict] = []
    for page in pages:
        # Traversal entries are reported, never resolved against the filesystem.
        missing = traversal_source_paths(page["text"]) + [
            c for c in resolve_source_paths(page["text"], raw_dir)
            if not (wiki_root / c).exists()
        ]
        if missing:
            out.append(
                {"page": slug_from_path(page["path"]), "missing": sorted(set(missing))}
            )
    return out


def cited_raw_slugs(pages: List[dict], raw_dir: Path) -> set:
    """Return the set of raw slugs that ANY wiki page depends on.

    A raw file is "cited" if `raw/<slug>.md` (or an image under
    `raw/images/<slug>/`) is referenced anywhere in a page's text — the Sources
    block, an inline image link, or prose. We scan the whole page text (not just
    Sources) so inline references also protect a raw from pruning; bare
    filenames in the Sources block count too (via `resolve_source_paths`).

    Tokens are matched exactly and normalized to the slug: `raw/foo.md` →
    `foo`, `raw/images/foo/0.png` → `foo`. Exact-token extraction avoids
    Jira slug-prefix collisions (`proj-12` vs `proj-123`).
    """
    cited: set = set()
    for page in pages:
        tokens = PATH_TOKEN_RE.findall(page["text"])
        tokens += resolve_source_paths(page["text"], raw_dir)
        for token in tokens:
            token = token.strip()
            head, _, rest = token.partition("/")
            # Bare-name resolution yields the configured raw dir name.
            if head not in ("raw", raw_dir.name) or is_traversal(token):
                continue
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
        if slug in EXEMPT_PAGES:
            continue
        for t in targets:
            archived_slug = archived_slug_of(t)
            if archived_slug and not has_banner(archived_slug):
                out.append({"from": slug, "to": t, "archived_slug": archived_slug})
    return out


_COMMIT_CACHE: Dict[Tuple[str, str, bool], Optional[Tuple[str, int]]] = {}


def _git(wiki_root: Path, *args: str) -> Optional[str]:
    """stdout of `git -C wiki_root <args>`, or None if git is missing or fails."""
    try:
        out = subprocess.run(
            ["git", "-C", str(wiki_root), *args], capture_output=True, text=True
        )
    except OSError:
        return None
    return out.stdout if out.returncode == 0 else None


def _git_paths(wiki_root: Path, *args: str) -> set:
    """Paths printed by a path-listing git command, read with -z so non-ASCII
    names are not C-quoted; empty set if git fails."""
    return set(filter(None, (_git(wiki_root, args[0], "-z", *args[1:]) or "").split("\0")))


def last_commit(
    wiki_root: Path, rel_path: str, skip_lint: bool = False
) -> Optional[Tuple[str, int]]:
    """(sha, committer_unix_time) of the newest commit touching rel_path.

    With skip_lint, commits whose subject starts with `lint:` are ignored, so a
    lint link-fix never counts as the page catching up with its sources.
    """
    key = (str(wiki_root), rel_path, skip_lint)
    if key not in _COMMIT_CACHE:
        found = None
        log = _git(wiki_root, "log", "--format=%H%x09%ct%x09%s", "--", rel_path) or ""
        for line in log.splitlines():
            sha, ct, subject = (line.split("\t", 2) + [""])[:3]
            if skip_lint and subject.startswith("lint:"):
                continue
            found = (sha, int(ct))
            break
        _COMMIT_CACHE[key] = found
    return _COMMIT_CACHE[key]


def find_source_drift(
    pages: List[dict],
    wiki_root: Path,
    wiki_subdir: str,
    raw_dir: Path,
    warnings: List[str],
    retired: Optional[set] = None,
) -> List[dict]:
    """Pages whose cited raws changed in git after the page's last non-lint commit.

    A cited raw with uncommitted changes (modified or untracked) is always drift.
    An untracked page is never drifted; EXEMPT_PAGES and retired pages are
    skipped. No repo / no commits -> [] + warning.
    """
    if _git(wiki_root, "rev-parse", "--verify", "-q", "HEAD") is None:
        warnings.append("source_drift skipped: wiki root is not a git repo with commits")
        return []
    # ls-files prints paths relative to wiki_root (git status --porcelain would
    # print them relative to the repo top, which differs for a nested wiki).
    dirty = _git_paths(wiki_root, "ls-files", "-m", "-o", "--exclude-standard", "--", raw_dir.name)
    out: List[dict] = []
    for page in pages:
        slug = slug_from_path(page["path"])
        if skip_judgment(slug) or slug in (retired or set()):
            continue
        page_c = last_commit(wiki_root, f"{wiki_subdir}/{page['path'].name}", skip_lint=True)
        if page_c is None:
            continue
        drifted = []
        for raw in dict.fromkeys(resolve_source_paths(page["text"], raw_dir)):
            raw_c = last_commit(wiki_root, raw)
            uncommitted = raw in dirty
            if uncommitted or (raw_c and raw_c[0] != page_c[0] and raw_c[1] > page_c[1]):
                drifted.append({
                    "raw": raw,
                    "page_commit": page_c[0],
                    "raw_commit": raw_c[0] if raw_c else None,
                    "uncommitted": uncommitted,
                })
        if drifted:
            out.append({"page": slug, "drifted": drifted})
    return out


def find_unsourced(pages: List[dict]) -> List[dict]:
    unsourced: List[dict] = []
    for page in pages:
        if skip_judgment(slug_from_path(page["path"])):
            continue
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


NUM_TOKEN_RE = re.compile(r"(?:\d[\d.,:/\-]*\d|\d)%?")
CITATION_RE = re.compile(r"\(source:[^)]*\)", re.IGNORECASE)
# `.!?` end a sentence only before whitespace/end, so `1.5` and `raw/a.md` stay whole.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])|\n")


def prose_sentences(body: str) -> List[str]:
    """Sentences from prose only: drops fenced code, table rows, headings and
    the `## Related pages` list."""
    kept: List[str] = []
    in_fence = in_related = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or s.startswith("|"):
            continue
        if s.startswith("#"):
            in_related = s.lstrip("#").strip().lower() == "related pages"
            continue
        if not in_related:
            kept.append(line)
    return [t.strip() for t in SENTENCE_SPLIT_RE.split("\n".join(kept)) if t.strip()]


def _info(page: dict, raw_dir: Path) -> dict:
    """Per-page derived data, computed once and cached on the page dict."""
    if "_info" not in page:
        prose = " ".join(prose_sentences(page_body(page["text"])))
        page["_info"] = {
            "slug": slug_from_path(page["path"]),
            "sources": set(resolve_source_paths(page["text"], raw_dir)),
            "links": resolved_links(page_body(page["text"])),
            "shingles": {prose[i:i + SHINGLE] for i in range(max(len(prose) - SHINGLE + 1, 1))},
        }
    return page["_info"]


def _numeric(page: dict) -> List[Tuple[str, str, List[str]]]:
    """(sentence, normalized number-stripped text, numeric tokens) for each prose
    sentence carrying a numeric token; cached on the page dict."""
    if "_numeric" not in page:
        out = []
        for s in prose_sentences(page_body(page["text"])):
            bare = CITATION_RE.sub("", s)
            tokens = NUM_TOKEN_RE.findall(bare)
            if tokens:
                out.append((s, " ".join(NUM_TOKEN_RE.sub("", bare).lower().split()), tokens))
        page["_numeric"] = out
    return page["_numeric"]


def active_pages(pages: List[dict]) -> List[dict]:
    """Content pages compared by overlap/collision checks: not exempt, journal or retired."""
    retired = retired_slugs(pages)
    return [p for p in pages
            if not skip_judgment(slug_from_path(p["path"])) and slug_from_path(p["path"]) not in retired]


def related_pairs(
    pages: List[dict], raw_dir: Path, warnings: Optional[List[str]] = None
) -> List[Tuple[dict, dict]]:
    """Active content pages sharing a cited raw, a link key (outbound link or
    own slug), or a slug token of >= SLUG_TOKEN_MIN chars, via inverted indexes."""
    active = active_pages(pages)
    index: Dict[str, List[int]] = {}
    for i, page in enumerate(active):
        info = _info(page, raw_dir)
        keys = {f"s:{s}" for s in info["sources"]}
        keys |= {f"l:{l}" for l in info["links"] | {info["slug"]}}
        keys |= {f"t:{t}" for t in re.split(r"[-_/\s]+", info["slug"]) if len(t) >= SLUG_TOKEN_MIN}
        for k in keys:
            index.setdefault(k, []).append(i)
    seen = set()
    for key, bucket in sorted(index.items()):
        # A key shared by very many pages would make pairing O(n^2); the exact
        # collision pass still covers these pages, and the skip is reported.
        if len(bucket) > MAX_BUCKET:
            if warnings is not None:
                warnings.append(f"overlap: skipped hub key {key[2:]} shared by {len(bucket)} pages")
            continue
        seen.update(combinations(bucket, 2))
    return [(active[i], active[j]) for i, j in sorted(seen)]


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def find_overlap(pairs: List[Tuple[dict, dict]], raw_dir: Path, limit: Optional[int]) -> List[dict]:
    out: List[dict] = []
    for pa, pb in pairs:
        a, b = sorted((_info(pa, raw_dir), _info(pb, raw_dir)), key=lambda x: x["slug"])
        slug_sim = SequenceMatcher(None, a["slug"], b["slug"], autojunk=False).ratio()
        # Character-shingle Jaccard: linear, symmetric, order-insensitive, language-agnostic.
        text_sim = _jaccard(a["shingles"], b["shingles"])
        score = (W_SOURCES * _jaccard(a["sources"], b["sources"])
                 + W_LINKS * _jaccard(a["links"], b["links"])
                 + W_SLUG * slug_sim + W_TEXT * text_sim)
        if score < OVERLAP_THRESHOLD:
            continue
        out.append({
            "pages": [a["slug"], b["slug"]],
            "score": round(score, 3),
            "shared_sources": len(a["sources"] & b["sources"]),
            "shared_links": len(a["links"] & b["links"]),
            "slug_sim": round(slug_sim, 3),
            "text_sim": round(text_sim, 3),
        })
    out.sort(key=lambda c: (-c["score"], c["pages"]))
    return out[:limit]


def _differing(ka: List[str], kb: List[str]) -> List[List[str]]:
    """Tokens each side has that the other lacks (multiset); positional
    differences when both hold the same tokens in another order."""
    def minus(xs: List[str], other: List[str]) -> List[str]:
        left, out = Counter(other), []
        for t in xs:
            if left[t]:
                left[t] -= 1
            else:
                out.append(t)
        return out

    da, db = minus(ka, kb), minus(kb, ka)
    if not da and not db:  # same multiset, so same length
        da = [x for x, y in zip(ka, kb) if x != y]
        db = [y for x, y in zip(ka, kb) if x != y]
    return [da, db]


def find_fact_collisions(
    pairs: List[Tuple[dict, dict]],
    pages: Iterable[dict] = (),
    stop_after: int = COLLISION_SCAN_LIMIT,
    stats: Optional[dict] = None,
    accept: Optional[Callable[[str, str, str, str], bool]] = None,
) -> List[dict]:
    """Prose sentences that agree once numbers are removed but whose numbers differ.

    Exact pass (wiki-wide over `pages`, independent of pairing): sentences with
    identical normalized number-stripped text. Fuzzy pass (related `pairs`
    only): SequenceMatcher ratio >= SENTENCE_SIM_THRESHOLD. Exact hits first,
    groups shared by the fewest pages first (a sentence templated across many
    pages is least likely a real contradiction), then fuzzy hits; de-duplicated.
    Each entry carries `kind` ("exact" | "fuzzy"). Each pass stops after
    `stop_after` hits of its own; a `stats` dict, if given, gets
    `truncated = True` when either pass stopped. `accept(pa, pb, sa, sb)`, if
    given, drops a hit before it counts against the budget (scope, review memory).
    """
    found: List[dict] = []
    seen = set()

    def add(kind: str, pa: str, sa: str, ka: List[str], pb: str, sb: str, kb: List[str]) -> None:
        if pa > pb:
            pa, sa, ka, pb, sb, kb = pb, sb, kb, pa, sa, ka
        key = (pa, pb, sa, sb)
        if key not in seen:
            seen.add(key)
            if accept is None or accept(*key):
                found.append({"pages": [pa, pb], "sentences": [sa, sb],
                              "differing": _differing(ka, kb), "kind": kind})

    exact: Dict[str, Dict[Tuple[str, Tuple[str, ...]], str]] = {}
    for page in active_pages(list(pages)):
        slug = slug_from_path(page["path"])
        for s, norm, tokens in _numeric(page):
            exact.setdefault(norm, {}).setdefault((slug, tuple(tokens)), s)

    def exact_pass() -> bool:
        def page_count(norm: str) -> int:
            return len({slug for slug, _ in exact[norm]})
        for norm in sorted(exact, key=lambda n: (page_count(n), n)):
            for ((pa, ka), sa), ((pb, kb), sb) in combinations(sorted(exact[norm].items()), 2):
                if pa != pb and ka != kb:
                    add("exact", pa, sa, list(ka), pb, sb, list(kb))
                    if len(found) >= stop_after:
                        return True
        return False

    def fuzzy_pass(budget_end: int) -> bool:
        sm = SequenceMatcher(autojunk=False)
        for pa, pb in pairs:
            a, b = sorted((pa, pb), key=lambda p: slug_from_path(p["path"]))
            na, nb = _numeric(a), _numeric(b)
            for sb, tb, kb in nb:
                sm.set_seq2(tb)
                for sa, ta, ka in na:
                    if ka == kb or ta == tb:  # identical text is the exact pass's job
                        continue
                    sm.set_seq1(ta)
                    # real_quick_ratio and quick_ratio are upper bounds on ratio().
                    if (sm.real_quick_ratio() >= SENTENCE_SIM_THRESHOLD
                            and sm.quick_ratio() >= SENTENCE_SIM_THRESHOLD
                            and sm.ratio() >= SENTENCE_SIM_THRESHOLD):
                        add("fuzzy", slug_from_path(a["path"]), sa, ka, slug_from_path(b["path"]), sb, kb)
                        if len(found) >= budget_end:
                            return True
        return False

    truncated = exact_pass()
    truncated = fuzzy_pass(len(found) + stop_after) or truncated
    if stats is not None:
        stats["truncated"] = truncated
    return found


REVIEWED_FILE = ".lint-reviewed.json"
REVIEW_KINDS = ("pairs", "collisions", "drift")
VERDICTS = ("fine", "rejected")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pair_hash(text_a: str, text_b: str) -> str:
    """Hash of two page texts, given in slug order."""
    return _sha(f"{text_a}\0{text_b}".encode("utf-8"))


def collision_hash(s1: str, s2: str) -> str:
    return _sha("\0".join(sorted((s1, s2))).encode("utf-8"))


def _review_key(kind: str, entry: dict) -> tuple:
    if kind == "drift":
        return (entry["page"], entry["raw"])
    if kind == "collisions":  # a page pair can hold several reviewed collisions
        return (tuple(entry["pages"]), entry["hash"])
    return tuple(entry["pages"])


def _two_strings(pages: object) -> bool:
    return (isinstance(pages, list) and len(pages) == 2
            and all(isinstance(p, str) for p in pages) and pages[0] != pages[1])


def _valid_entry(kind: str, e: object) -> bool:
    if not isinstance(e, dict):
        return False
    if kind == "drift":
        return all(isinstance(e.get(f), str) for f in ("page", "raw", "hash"))
    return _two_strings(e.get("pages")) and all(isinstance(e.get(f), str) for f in ("hash", "verdict"))


def load_reviewed(wiki_dir: Path, warnings: Optional[List[str]] = None) -> dict:
    """The review memory `wiki/.lint-reviewed.json`; empty lists when absent.
    Malformed entries are dropped (one warning with the count). Raises
    ValueError on a malformed file."""
    path = wiki_dir / REVIEWED_FILE
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict) or not all(isinstance(data.get(k, []), list) for k in REVIEW_KINDS):
        raise ValueError(f"{REVIEWED_FILE} is not an object of lists")
    out = {k: [e for e in data.get(k, []) if _valid_entry(k, e)] for k in REVIEW_KINDS}
    bad = sum(len(data.get(k, [])) - len(out[k]) for k in REVIEW_KINDS)
    if bad and warnings is not None:
        warnings.append(f"{REVIEWED_FILE}: ignored {bad} malformed entr{'y' if bad == 1 else 'ies'}")
    return out


def mark_reviewed(wiki_dir: Path, raw_dir: Path, payload: dict) -> dict:
    """Hash and merge reviewed items into the review memory, prune entries for
    pages that no longer exist, write it sorted. Raises ValueError on a bad
    payload (nothing is written then)."""
    def text(slug: str) -> str:
        path = wiki_dir / f"{slug}.md"
        if "/" in slug or not path.is_file():
            raise ValueError(f"no such page: {slug}")
        return load_page(path)["text"]

    def raw_bytes(raw: str) -> bytes:
        path = raw_dir.parent / raw
        if is_traversal(raw) or not path.is_file():
            raise ValueError(f"no such raw file: {raw}")
        return path.read_bytes()

    def verdict(item: dict) -> str:
        if item["verdict"] not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}: {item['verdict']!r}")
        return item["verdict"]

    def pages_of(item: dict) -> List[str]:
        if not _two_strings(item["pages"]):
            raise ValueError(f"pages must be two different page slugs: {item['pages']!r}")
        return sorted(item["pages"])

    def prose(slug: str) -> set:
        return set(prose_sentences(page_body(text(slug))))

    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    new: Dict[str, List[dict]] = {k: [] for k in REVIEW_KINDS}
    try:
        for item in payload.get("pairs", []):
            a, b = pages_of(item)
            new["pairs"].append({"pages": [a, b], "hash": pair_hash(text(a), text(b)),
                                 "verdict": verdict(item)})
        for item in payload.get("collisions", []):
            a, b = pages_of(item)
            if not _two_strings(item["sentences"]):
                raise ValueError(f"sentences must be two different strings: {item['sentences']!r}")
            s1, s2 = item["sentences"]
            pa, pb = prose(a), prose(b)
            if not ((s1 in pa and s2 in pb) or (s1 in pb and s2 in pa)):
                raise ValueError(f"sentences do not occur in pages {a} and {b}")
            new["collisions"].append({"pages": [a, b], "hash": collision_hash(s1, s2),
                                      "verdict": verdict(item)})
        for item in payload.get("drift", []):
            if not isinstance(item["raw"], str):
                raise ValueError(f"raw must be a path string: {item['raw']!r}")
            text(item["page"])
            new["drift"].append({"page": item["page"], "raw": item["raw"],
                                 "hash": _sha(raw_bytes(item["raw"]))})
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"bad --mark-reviewed entry: {e}") from e

    memory = load_reviewed(wiki_dir)
    pruned = 0
    for kind in REVIEW_KINDS:
        merged = {_review_key(kind, e): e for e in memory[kind] + new[kind]}
        live = [e for e in merged.values()
                if all((wiki_dir / f"{p}.md").is_file()
                       for p in (e["pages"] if "pages" in e else [e["page"]]))]
        pruned += len(merged) - len(live)
        memory[kind] = sorted(live, key=lambda e: _review_key(kind, e))
    (wiki_dir / REVIEWED_FILE).write_text(
        json.dumps(memory, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"recorded": sum(len(v) for v in new.values()), "pruned": pruned}


FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
# shortcut: inline code spans are matched per line; a span crossing a line break is not seen.
CODE_SPAN_RE = re.compile(r"(`+).+?\1")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _unlink_deleted(text: str, gone: set, index: bool) -> Tuple[str, List[str], List[str]]:
    """Unlink `[[links]]` to deleted pages outside code and embeds; in the index,
    drop a list line whose only link is to a deleted page. Returns (text,
    unlinked targets, removed index slugs). Line endings are kept as they are."""
    dropped: List[str] = []
    removed: List[str] = []

    def sub(m: "re.Match") -> str:
        target = link_target(m.group(1))
        if target not in gone or (m.start() and m.string[m.start() - 1] == "!"):
            return m.group(0)
        dropped.append(target)
        return (m.group(2) or m.group(1)).strip()

    lines = text.splitlines(keepends=True)
    out: List[str] = []
    fence: Optional[str] = None
    for i, line in enumerate(lines):
        f = FENCE_RE.match(line)
        if fence is None and f:
            fence = f.group(1)
        elif fence is not None:
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) \
                    and not line[f.end():].strip():
                fence = None
        else:
            links = WIKI_LINK_RE.findall(line)
            if (index and LIST_ITEM_RE.match(line) and "![[" not in line
                    and len(links) == 1 and link_target(links[0][0]) in gone):
                target = link_target(links[0][0])
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                if nxt and _indent(nxt) > _indent(line):
                    print(f"note: index.md line for deleted page {target} kept "
                          "(nested content follows)", file=sys.stderr)
                    out.append(line)
                else:
                    removed.append(target)
                continue
            pos, parts = 0, []
            for span in CODE_SPAN_RE.finditer(line):
                parts += [WIKI_LINK_RE.sub(sub, line[pos:span.start()]), span.group(0)]
                pos = span.end()
            line = "".join(parts) + WIKI_LINK_RE.sub(sub, line[pos:])
        out.append(line)
    return "".join(out), dropped, removed


def apply_fixes(
    pages: List[dict], wiki_dir: Path, raw_dir: Path, warnings: Optional[List[str]] = None
) -> dict:
    """Judgment-free cleanups: delete stub-placeholder pages, unlink links to
    the pages deleted in this run (and drop their index lines), repoint a
    missing Sources path to the one `raw/*.md` with exactly its stem.

    Never writes log.md, journal pages or a page that is not valid UTF-8;
    keeps every byte it does not change (line endings included). Other broken
    links are left for the report.
    """
    texts: Dict[str, str] = {}
    for page in pages:
        try:
            with open(page["path"], encoding="utf-8", newline="") as f:
                texts[slug_from_path(page["path"])] = f.read()
        except UnicodeDecodeError:
            if warnings is not None:
                warnings.append(f"--fix skipped {page['path'].name}: not valid UTF-8")

    retired = retired_slugs(pages)
    deleted = sorted(
        s for s, t in texts.items()
        if not skip_judgment(s) and s not in retired
        and any(m in page_body(t).lower() for m in STUB_MARKERS))
    for slug in deleted:
        (wiki_dir / f"{slug}.md").unlink()

    raws = ({p.stem: p for p in raw_dir.glob("*.md") if p.is_file()}
            if raw_dir.name == "raw" and raw_dir.is_dir() else {})
    wiki_root = raw_dir.parent
    unlinked: List[dict] = []
    repaired: List[dict] = []
    removed: List[str] = []

    for slug, old in texts.items():
        if slug in deleted or slug == "log" or is_journal_slug(slug):
            continue
        text, dropped, rm = _unlink_deleted(old, set(deleted), slug == "index")
        unlinked += [{"page": slug, "target": t} for t in dict.fromkeys(dropped)]
        removed += rm

        done = set()
        for missing in dict.fromkeys(resolve_source_paths(text, raw_dir)):
            raw = raws.get(Path(missing).stem)
            if raw is None or (wiki_root / missing).exists():
                continue
            new = f"raw/{raw.name}"
            m = SOURCES_BLOCK_RE.search(text)
            block = PATH_TOKEN_RE.sub(lambda t: new if t.group(0) == missing else t.group(0), m.group(1))
            text = text[:m.start(1)] + block + text[m.end(1):]
            if new not in done:
                done.add(new)
                repaired.append({"page": slug, "from": missing, "to": new})

        if text != old:
            with open(wiki_dir / f"{slug}.md", "w", encoding="utf-8", newline="") as f:
                f.write(text)
    return {"deleted_pages": deleted, "unlinked": unlinked,
            "repaired_sources": repaired, "removed_index_lines": removed}


def fix_refusal(wiki_root: Path, wiki_subdir: str, allow_dirty: bool) -> Optional[str]:
    """Why `--fix` must not run here, or None: it needs a git work tree and,
    unless allow_dirty, no uncommitted changes under the wiki dir."""
    if (_git(wiki_root, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return f"--fix needs a git work tree; {wiki_root} is not inside one"
    if allow_dirty:
        return None
    dirty = (_git_paths(wiki_root, "ls-files", "-m", "--", wiki_subdir)
             | _git_paths(wiki_root, "diff", "--cached", "--name-only", "--relative", "--", wiki_subdir)
             | {p for p in _git_paths(wiki_root, "ls-files", "-o", "--exclude-standard", "--", wiki_subdir)
                if p.endswith(".md")})
    if dirty:
        shown = ", ".join(sorted(dirty)[:5]) + (", ..." if len(dirty) > 5 else "")
        return (f"--fix refused: uncommitted changes under {wiki_subdir}/ ({shown}); "
                "commit them or pass --allow-dirty")
    return None


def changed_since_last_lint(wiki_root: Path) -> Optional[set]:
    """Wiki-root-relative paths changed (committed or not) since the newest
    commit whose subject starts with `lint:`; None when there is no such commit."""
    log = _git(wiki_root, "log", "--format=%H%x09%s", "--", ".") or ""
    base = next((sha for sha, _, subject in (l.partition("\t") for l in log.splitlines())
                 if subject.startswith("lint:")), None)
    if base is None:
        return None
    # Diffing against the work tree covers base..HEAD plus staged/unstaged edits.
    return (_git_paths(wiki_root, "diff", "--name-only", "--relative", base)
            | _git_paths(wiki_root, "ls-files", "-o", "--exclude-standard"))


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


def resolve_dirs(
    wiki_root: Path,
    wiki_dir_arg: Optional[str] = None,
    raw_dir_arg: Optional[str] = None,
    warnings: Optional[List[str]] = None,
) -> Tuple[Path, Path]:
    """(wiki_dir, raw_dir): CLI override, else `.wikirc.json` value, else wiki/raw.
    An unreadable `.wikirc.json` is ignored with a note in `warnings`."""
    wiki_subdir = wiki_dir_arg or "wiki"
    raw_subdir = raw_dir_arg or "raw"
    config_path = wiki_root / ".wikirc.json"
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            wiki_subdir = wiki_dir_arg or cfg.get("wiki_dir") or wiki_subdir
            raw_subdir = raw_dir_arg or cfg.get("raw_dir") or raw_subdir
        except (json.JSONDecodeError, OSError) as e:
            if warnings is not None:
                warnings.append(f"ignored unreadable {config_path.name}: {e}")
    return wiki_root / wiki_subdir, wiki_root / raw_subdir


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic wiki linter")
    parser.add_argument("--wiki-root", type=Path, required=True)
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
    parser.add_argument(
        "--graph",
        action="store_true",
        help="Include the wiki-link graph (edges, inbound_counts) in the report",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=MAX_CANDIDATES,
        help=f"Cap on overlap_candidates (default {MAX_CANDIDATES})",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--full", action="store_true",
                       help="Compare and check every page (default)")
    scope.add_argument("--since-last-lint", action="store_true",
                       help="Limit overlap, collisions and drift to pages/raws changed "
                            "since the newest `lint:` commit")
    writes = parser.add_mutually_exclusive_group()
    writes.add_argument("--mark-reviewed", action="store_true",
                        help=f"Record reviewed items read from stdin into wiki/{REVIEWED_FILE}; "
                             "runs no checks")
    writes.add_argument("--fix", action="store_true",
                        help="Delete stub-placeholder pages, unlink links to them, repair "
                             "missing Sources paths by exact stem, then report on the fixed wiki")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="With --fix: run despite uncommitted changes under the wiki dir")
    args = parser.parse_args()
    warnings: List[str] = []

    wiki_root = args.wiki_root.resolve()
    wiki_dir, raw_dir = resolve_dirs(wiki_root, args.wiki_dir, args.raw_dir, warnings)
    wiki_subdir = os.path.relpath(wiki_dir, wiki_root)

    claude_md = wiki_root / "CLAUDE.md"
    if claude_md.is_file() and "<!-- llm-wiki:begin -->" not in (
        line.strip() for line in claude_md.read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        warnings.append("CLAUDE.md has no llm-wiki managed block — "
                        f"run bootstrap.py --upgrade --target {wiki_root}")

    if not wiki_dir.exists():
        print(f"ERROR: wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    if args.mark_reviewed:
        try:
            result = mark_reviewed(wiki_dir, raw_dir, json.loads(sys.stdin.read() or "{}"))
        except (ValueError, OSError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0

    pages = [load_page(p) for p in collect_pages(wiki_dir)]
    fixed = None
    if args.fix:
        refusal = fix_refusal(wiki_root, wiki_subdir, args.allow_dirty)
        if refusal:
            print(f"ERROR: {refusal}", file=sys.stderr)
            return 2
        fixed = apply_fixes(pages, wiki_dir, raw_dir, warnings)
        pages = [load_page(p) for p in collect_pages(wiki_dir)]

    archived = archived_slugs(wiki_dir)
    edges = build_edges(pages)
    inbound = inbound_counts(edges)
    retired = retired_slugs(pages)
    orphans = find_orphans(pages, inbound, retired)
    broken, missing = find_broken_links(pages, edges, archived)
    unbannered_archive_links = find_unbannered_archive_links(edges, wiki_dir, archived)
    format_violations = find_format_violations(pages)
    source_drift = find_source_drift(pages, wiki_root, wiki_subdir, raw_dir, warnings, retired)
    unsourced = find_unsourced(pages)
    empty_pages = find_empty_pages(pages)
    status_pages = find_status_pages(pages)
    missing_sources = find_missing_sources(pages, raw_dir)
    cited = cited_raw_slugs(pages, raw_dir)
    empty_raw = find_empty_raw(raw_dir, cited)
    pairs = related_pairs(pages, raw_dir, warnings)

    # Scope and review memory apply before the caps, and inside the collision
    # scan before its budget, so dropped items never take report slots.
    changed = changed_since_last_lint(wiki_root) if args.since_last_lint else None
    if args.since_last_lint and changed is None:
        warnings.append("--since-last-lint: no `lint:` commit found; checked the full wiki")
    texts = {slug_from_path(p["path"]): p["text"] for p in pages}

    def touched(slug: str) -> bool:
        return changed is None or f"{wiki_subdir}/{slug}.md" in changed

    if changed is not None:
        pairs = [(a, b) for a, b in pairs
                 if touched(slug_from_path(a["path"])) or touched(slug_from_path(b["path"]))]
        source_drift = [d for d in source_drift if touched(d["page"])
                        or changed & set(resolve_source_paths(texts[d["page"]], raw_dir))]

    try:
        reviewed = load_reviewed(wiki_dir, warnings)
    except (ValueError, OSError) as e:
        warnings.append(f"ignored unreadable {REVIEWED_FILE}: {e}")
        reviewed = {k: [] for k in REVIEW_KINDS}
    seen_pairs = {(tuple(e["pages"]), e["hash"]) for e in reviewed["pairs"]}
    seen_collisions = {(tuple(e["pages"]), e["hash"]) for e in reviewed["collisions"]}
    acked = {(e["page"], e["raw"]): e["hash"] for e in reviewed["drift"]}
    suppressed = {k: 0 for k in REVIEW_KINDS}

    overlap = []
    for c in find_overlap(pairs, raw_dir, None):
        a, b = c["pages"]
        if seen_pairs and ((a, b), pair_hash(texts[a], texts[b])) in seen_pairs:
            suppressed["pairs"] += 1
        else:
            overlap.append(c)
    overlap = overlap[:args.max_candidates]

    def accept_collision(pa: str, pb: str, sa: str, sb: str) -> bool:
        if not (touched(pa) or touched(pb)):
            return False
        if seen_collisions and ((pa, pb), collision_hash(sa, sb)) in seen_collisions:
            suppressed["collisions"] += 1
            return False
        return True

    scan: dict = {}
    collisions = find_fact_collisions(pairs, pages, stats=scan, accept=accept_collision)

    def drift_acked(page: str, raw: str) -> bool:
        path = wiki_root / raw
        return (page, raw) in acked and path.is_file() and acked[(page, raw)] == _sha(path.read_bytes())

    kept_drift = []
    for d in source_drift:
        raws = [r for r in d["drifted"] if not drift_acked(d["page"], r["raw"])]
        suppressed["drift"] += len(d["drifted"]) - len(raws)
        if raws:
            kept_drift.append({**d, "drifted": raws})
    source_drift = kept_drift

    exact = [c for c in collisions if c["kind"] == "exact"]
    fuzzy = [c for c in collisions if c["kind"] == "fuzzy"]
    for c in collisions:
        del c["kind"]
    # Exact hits get at most MAX_EXACT_COLLISIONS slots while fuzzy hits exist;
    # slots either kind leaves unused go to the other.
    n_exact = min(len(exact), max(MAX_EXACT_COLLISIONS, MAX_COLLISIONS - len(fuzzy)))
    shown = exact[:n_exact] + fuzzy[:MAX_COLLISIONS - n_exact]
    if scan["truncated"] or len(collisions) > MAX_COLLISIONS:
        total = f"at least {len(collisions)}" if scan["truncated"] else len(collisions)
        warnings.append(f"fact_collisions: showing {len(shown)} of {total} found "
                        f"(exact: {len(exact)}, fuzzy: {len(fuzzy)})"
                        + ("; collision scan truncated, more may exist" if scan["truncated"] else ""))
    collisions = shown

    report = {
        "wiki_root": str(wiki_root),
        "wiki_dir": str(wiki_dir),
        "raw_dir": str(raw_dir),
        "page_count": len(pages),
        "archived_count": len(archived),
        "orphans": orphans,
        "broken_links": broken,
        "missing_pages": dict(sorted(missing.items())),
        "archive_links_without_banner": unbannered_archive_links,
        "format_violations": format_violations,
        "empty_pages": empty_pages,
        "source_drift": source_drift,
        "unsourced_claims": unsourced,
        "status_pages": status_pages,
        "missing_sources": missing_sources,
        "empty_raw": empty_raw,
        "overlap_candidates": overlap,
        "fact_collisions": collisions,
        "suppressed": suppressed,
        "warnings": warnings,
    }

    if fixed is not None:
        report["fixed"] = fixed

    if args.graph:
        report["edges"] = edges
        report["inbound_counts"] = inbound

    if args.sources:
        report["raw_sources"] = scan_raw(raw_dir)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

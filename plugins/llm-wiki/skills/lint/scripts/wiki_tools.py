#!/usr/bin/env python3
"""Deterministic wiki bookkeeping — stdlib only. JSON on stdout.

Usage:
    python3 wiki_tools.py locate     --wiki-root PATH TERM [TERM ...]
    python3 wiki_tools.py citing     --wiki-root PATH RAW_SLUG
    python3 wiki_tools.py index-sync --wiki-root PATH
    python3 wiki_tools.py log        --wiki-root PATH --op ingest|lint|log --title TEXT [--bullet TEXT ...]

`index`, `log`, README and journal pages (`YYYY-MM-DD`, `YYYY-MM-index`, owned
by the `log` skill) are never located, cited, or added/removed/moved in
`index.md`; its `## Diary` section is never modified. `index.md` and `log.md`
are edited without newline translation: added lines use the file's own line
ending and untouched lines stay byte-identical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

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


ASCII_MIN_CHARS = 3  # word-bounded, so `sso` / `api` pages are findable
OTHER_MIN_CHARS = 4  # CJK etc.: substring match, so shorter names are too noisy
MENTION_CAP = 5  # occurrences counted per page
TITLE_MIN_CONTAINMENT = 0.6  # share of the source title's trigrams found in the page
TITLE_WEIGHT = 3  # score of a title match (containment scales it)


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").replace("_", " ").split())


def _trigrams(text: str) -> set:
    text = _norm(text)
    return {text[i:i + 3] for i in range(len(text) - 2)} or ({text} if text else set())


def _forms(name: str) -> set:
    """`name` plus its other number: retries<->retry, payments<->payment, process->processes."""
    if name.endswith("ies"):
        return {name, name[:-3] + "y"}
    if name.endswith("ss"):
        return {name, name + "es"}
    if name.endswith("s"):
        return {name, name[:-1]}
    if name.endswith("y") and name[-2:-1] not in "aeiou":
        return {name, name[:-1] + "ies"}
    return {name, name + "s"}


def _count(form: str, text: str) -> int:
    """ASCII names match whole words only (`data` is not in "metadata"); others by substring."""
    if form.isascii():
        if form not in text:  # cheap pre-check; the regex is ~50x slower on large sources
            return 0
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(form)}(?![a-z0-9])", text))
    return text.count(form)


def related(wiki_dir: Path, title: str, text: str, limit: int = 10) -> List[dict]:
    """Pages a source is about: named in its text, or in / like its title.

    `mentioned`: the page's H1 or slug-as-words (singular or plural) occurs in
    `text`. `title`: such a name occurs in `title`, or character-trigram
    containment of `title` in the page's H1 + slug + Summary is at least
    TITLE_MIN_CONTAINMENT. ASCII names match on word boundaries.
    """
    text, norm_title = _norm(text), _norm(title)
    want = _trigrams(title)
    hits = []
    for page in _content_pages(wiki_dir):
        slug = slug_from_path(page["path"])
        h1 = H1_RE.search(page["text"])
        h1 = h1.group(1) if h1 else ""
        summary = _summary(page["text"])
        names = {
            n for n in (_norm(h1), _norm(slug))
            if len(n) >= (ASCII_MIN_CHARS if n.isascii() else OTHER_MIN_CHARS)
        }
        forms = set().union(*(_forms(n) for n in names)) if names else set()
        mentions = min(max((_count(f, text) for f in forms), default=0), MENTION_CAP)
        sim = len(want & _trigrams(f"{h1} {slug} {summary}")) / len(want) if want else 0.0
        if any(_count(f, norm_title) for f in forms):
            title_score = TITLE_WEIGHT
        else:
            title_score = TITLE_WEIGHT * sim if sim >= TITLE_MIN_CONTAINMENT else 0
        if not (mentions or title_score):
            continue
        hits.append({
            "page": slug,
            "summary": summary,
            "score": round(mentions + title_score, 2),
            "why": "both" if mentions and title_score else ("mentioned" if mentions else "title"),
        })
    hits.sort(key=lambda h: (-h["score"], h["page"]))
    return hits[:limit]


# ------------------------- line-preserving file edits -------------------------


def _read_lines(path: Path, default: str = "") -> Tuple[List[str], str]:
    """Lines with their own endings (no newline translation) and the file's
    dominant line ending, used for every added line."""
    text = default
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as f:
            text = f.read()
    crlf = text.count("\r\n")
    eol = "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"
    return re.findall(r"[^\n]*\n|[^\n]+$", text), eol


def _write_lines(path: Path, lines: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def _blank(line: str) -> bool:
    return not line.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _terminate_last(lines: List[str], at: int, eol: str) -> None:
    """Give the last line an ending before something is inserted after it."""
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += eol


def _insert_block(lines: List[str], at: int, new: List[str], eol: str) -> None:
    """Insert `new` (bare lines) at `at`, separated from non-blank neighbours by a blank line."""
    _terminate_last(lines, at, eol)
    block = [n + eol for n in new]
    if at > 0 and not _blank(lines[at - 1]):
        block.insert(0, eol)
    if at < len(lines) and not _blank(lines[at]):
        block.append(eol)
    lines[at:at] = block


def _section_end(lines: List[str], start: int) -> int:
    """Index after the last non-blank line of the `## ` section starting at `start`."""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end - 1 > start and _blank(lines[end - 1]):
        end -= 1
    return end


def _insert_in_section(lines: List[str], heading: str, new: List[str], eol: str,
                       before: Optional[str] = None) -> None:
    """Append `new` at the end of section `heading`; create it (before `before`, else at the end) if absent."""
    bare = [_bare(line).rstrip() for line in lines]
    if heading in bare:
        end = _section_end(lines, bare.index(heading))
        _terminate_last(lines, end, eol)
        lines[end:end] = [n + eol for n in new]
        return
    at = bare.index(before) if before in bare else len(lines)
    while at > 0 and _blank(lines[at - 1]):
        at -= 1
    _insert_block(lines, at, [heading, *new], eol)


def _targets(text: str) -> set:
    return {t for t in map(link_target, (m.group(1) for m in WIKI_LINK_RE.finditer(text))) if t}


def index_sync(wiki_dir: Path) -> dict:
    """Add unlisted pages under `## Uncategorized`, drop list lines to deleted
    pages, and move lines to archived pages under `## Archive`. An entry with
    nested lines under it is left alone and named on stderr."""
    index_path = wiki_dir / "index.md"
    lines, eol = _read_lines(index_path, default="# Wiki Index\n")
    original = "".join(lines)
    existing = {slug_from_path(p).lower() for p in collect_pages(wiki_dir)}
    archived = {s.lower(): s for s in archived_slugs(wiki_dir)}
    content = {slug_from_path(p["path"]): p["text"] for p in _content_pages(wiki_dir)}

    kept: List[str] = []
    to_archive: List[str] = []
    removed: List[str] = []
    moved: List[str] = []
    in_diary = False
    just_removed = False
    for i, line in enumerate(lines):
        if just_removed and _blank(line) and kept and _blank(kept[-1]):
            continue  # no double blank line where an entry was taken out
        just_removed = False
        if line.startswith("## "):
            in_diary = _bare(line).strip() == "## Diary"
        targets = _targets(line)
        if in_diary or not LIST_LINE_RE.match(line) or len(targets) != 1:
            kept.append(line)
            continue
        target = targets.pop()
        # Case-insensitive: `[[Alpha]]` already lists page `alpha`.
        if target.lower() in existing or target.startswith(ARCHIVE_SUBDIR + "/") or is_journal_slug(target):
            kept.append(line)
            continue
        # A child may follow after blank lines (loose list) and is still nested.
        nxt = next((n for n in lines[i + 1:] if not _blank(n)), "")
        if nxt and _indent(nxt) > _indent(line):
            print(f"index-sync: left [[{target}]] at index.md line {i + 1} untouched: "
                  "it has nested lines, review it by hand", file=sys.stderr)
            kept.append(line)
            continue
        just_removed = True
        if target.lower() in archived:
            moved.append(archived[target.lower()])
            to_archive.append(WIKI_LINK_RE.sub(
                lambda m: m.group(0).replace("[[", f"[[{ARCHIVE_SUBDIR}/", 1)
                if link_target(m.group(1)) == target else m.group(0),
                _bare(line),
            ))
        else:
            removed.append(target)

    listed = {t.lower() for t in _targets("".join(kept))}
    added = sorted(s for s in content if s.lower() not in listed)
    if added:
        new = [f"- [[{s}]] — {_summary(content[s])}" if _summary(content[s]) else f"- [[{s}]]" for s in added]
        _insert_in_section(kept, "## Uncategorized", new, eol, before="## Archive")
    if to_archive:
        _insert_in_section(kept, "## Archive", to_archive, eol)

    if "".join(kept) != original or not index_path.exists():
        _write_lines(index_path, kept)
    return {"added": added, "removed": sorted(set(removed)), "archived": sorted(set(moved))}


def _one_line(text: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ", text).strip()


def append_log(wiki_dir: Path, op: str, title: str, bullets: List[str], today: Optional[str] = None) -> None:
    """Insert a dated entry as the newest one: before the first `## ` heading
    after the H1, else after the first `---` line, else at the end. A newest
    entry with the same day and op gets the new lines appended instead."""
    if op not in LOG_OPS:
        raise ValueError(f"unknown op {op!r}; expected one of {', '.join(LOG_OPS)}")
    day = today or date.today().isoformat()
    header = f"## {day}" if op == "ingest" else f"## {day} ({op})"
    items = [f"- {_one_line(title)}", *(f"- {_one_line(b)}" for b in bullets)]
    log_path = wiki_dir / "log.md"
    lines, eol = _read_lines(log_path)
    h1 = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1 is None:
        h1 = 0
        if lines and _bare(lines[0]).strip() == "---":  # YAML front matter: H1 goes after it
            close = next((i for i in range(1, len(lines)) if _bare(lines[i]).strip() == "---"), None)
            h1 = close + 1 if close is not None else 0
        _terminate_last(lines, h1, eol)
        lines.insert(h1, "# Wiki Log" + eol)
    first = next((i for i in range(h1 + 1, len(lines)) if lines[i].startswith("## ")), None)
    if first is not None and _bare(lines[first]).rstrip() == header:
        end = _section_end(lines, first)
        _terminate_last(lines, end, eol)
        lines[end:end] = [item + eol for item in items]
    else:
        if first is None:
            rule = next((i for i in range(h1 + 1, len(lines)) if _bare(lines[i]).strip() == "---"), None)
            first = rule + 1 if rule is not None else len(lines)
        _insert_block(lines, first, [header, *items], eol)
    _write_lines(log_path, lines)


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
    p = sub.add_parser("log", parents=[common], help="Add an entry to wiki/log.md")
    p.add_argument("--op", choices=LOG_OPS, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--bullet", action="append", default=[])
    args = parser.parse_args()

    warnings: List[str] = []
    wiki_dir, raw_dir = resolve_dirs(args.wiki_root.resolve(), warnings=warnings)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
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

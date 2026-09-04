#!/usr/bin/env python3
"""Resolve a Who/What mention against a linked wiki, read-only.

Shells out to the target wiki's own scripts/wiki_search.sh if present (same
ripgrep-ranked search /ingest itself recommends for anti-duplication), else
falls back to a plain ripgrep scan over its wiki/ directory. Never writes
anything, and never touches the linked wiki's raw/ or wiki/ — this is a
lookup for Claude to judge confidence against before linking [[label/slug]]
in /log's own day-pages.

Usage:
    python3 resolve_link.py --wiki-root PATH --label LABEL --query "TEXT"
    python3 resolve_link.py --wiki-root PATH --role who --query "TEXT"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "ingest" / "scripts"))

from config import ConfigError, load_config  # noqa: E402


# Mirrors ingest.py's _looks_like_placeholder — an un-filled-in linked_wikis
# entry (still pointing at the illustrative path from wikirc.example.json)
# should be reported as unconfigured, not walked as if it were real.
_PLACEHOLDER_MARKERS = ("replace_me", "your-", "example.com", "changeme", "todo")


def _looks_like_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


_HEADER_RE = re.compile(r"^##\s+(.+?)\s+\((\d+)\s+hits?\)\s*$")


def _run_wiki_search(script: Path, query: str, top: int) -> list[dict]:
    proc = subprocess.run(
        ["bash", str(script), "--top", str(top), query],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 2:
        return []  # usage error — don't let a malformed query crash /log
    candidates = []
    for line in proc.stdout.splitlines():
        m = _HEADER_RE.match(line.strip())
        if not m:
            continue
        rel_path, hits = m.group(1), int(m.group(2))
        candidates.append({"slug": Path(rel_path).stem, "hits": hits})
    return candidates


def _run_ripgrep_fallback(wiki_dir: Path, query: str, top: int) -> list[dict]:
    if not wiki_dir.exists():
        return []
    proc = subprocess.run(
        ["rg", "--type", "md", "--ignore-case", "--count-matches", "-e", query, str(wiki_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        return []
    rows = []
    for line in proc.stdout.splitlines():
        path_str, _, count_str = line.rpartition(":")
        if not path_str:
            continue
        try:
            count = int(count_str)
        except ValueError:
            continue
        rows.append((count, Path(path_str).stem))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [{"slug": slug, "hits": count} for count, slug in rows[:top]]


def resolve_entries(cfg, label: str | None, role: str | None) -> list[dict]:
    if label:
        entry = cfg.linked_wiki(label)
        return [entry] if entry else []
    return cfg.linked_wikis_by_role(role)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve a Who/What mention against a linked wiki")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--label", default=None)
    parser.add_argument("--role", default=None, choices=["who", "what", "generic"])
    parser.add_argument("--query", required=True)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    if not args.label and not args.role:
        parser.error("pass --label or --role")

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    entries = resolve_entries(cfg, args.label, args.role)
    if not entries:
        print(
            json.dumps(
                {"error": "no matching linked_wikis entry", "label": args.label, "role": args.role},
                indent=2,
            )
        )
        return 1

    results = []
    for entry in entries:
        entry_label = entry.get("label")
        entry_path = entry.get("path") or ""
        if _looks_like_placeholder(entry_path):
            results.append(
                {
                    "label": entry_label,
                    "role": entry.get("role"),
                    "path": entry_path,
                    "configured": False,
                    "note": "linked_wikis entry looks unfilled (placeholder path) — skipped",
                    "candidates": [],
                }
            )
            continue

        target_root = Path(entry_path).expanduser()
        wiki_dir = target_root / "wiki"
        search_script = target_root / "scripts" / "wiki_search.sh"

        if search_script.exists():
            candidates = _run_wiki_search(search_script, args.query, args.top)
            used_wiki_search = True
        else:
            candidates = _run_ripgrep_fallback(wiki_dir, args.query, args.top)
            used_wiki_search = False

        results.append(
            {
                "label": entry_label,
                "role": entry.get("role"),
                "path": str(target_root),
                "configured": True,
                "used_wiki_search": used_wiki_search,
                "candidates": candidates,
            }
        )

    print(json.dumps({"query": args.query, "results": results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

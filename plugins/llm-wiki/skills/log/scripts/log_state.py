#!/usr/bin/env python3
"""Track which raw sources have been synthesized into diary day-pages by /log.

Mirrors raw_store.write_fetch_history's pattern but for a separate concern:
.wiki-state/last-fetched.json records the last *fetch*; .wiki-state/
last-logged.json (this file) records the last *log* (Event-extraction + day-
page synthesis) — a source can be fetched by /ingest long before /log ever
processes it, or reprocessed by /log without being re-fetched, so the two
watermarks are independent.

Usage:
    python3 log_state.py --wiki-root PATH --pending
    python3 log_state.py --wiki-root PATH --mark SLUG --pages "2026-09-04,2026-08-30"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "ingest" / "scripts"))

from config import ConfigError, load_config  # noqa: E402


def _state_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / ".wiki-state" / "last-logged.json"


def read_state(wiki_root: Path) -> dict:
    path = _state_path(wiki_root)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(wiki_root: Path, data: dict) -> Path:
    path = _state_path(wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return path


def mark_logged(wiki_root: Path, slug: str, day_pages: list[str]) -> Path:
    data = read_state(wiki_root)
    data[slug] = {
        "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "day_pages": day_pages,
    }
    return write_state(wiki_root, data)


def pending_sources(wiki_root: Path, raw_dir: Path) -> list[str]:
    logged = set(read_state(wiki_root).keys())
    slugs = set()
    for p in Path(raw_dir).glob("*.source.json"):
        stem = p.stem  # "<slug>.source" (Path.stem only strips the final ".json")
        slug = stem[: -len(".source")] if stem.endswith(".source") else stem
        slugs.add(slug)
    return sorted(slugs - logged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Track /log's raw-source watermark")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--pending", action="store_true", help="List raw sources not yet logged")
    parser.add_argument("--mark", metavar="SLUG", help="Mark a slug as logged")
    parser.add_argument(
        "--pages", default="", help="Comma-separated day-page dates touched (used with --mark)"
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.mark:
        pages = [p.strip() for p in args.pages.split(",") if p.strip()]
        path = mark_logged(args.wiki_root, args.mark, pages)
        print(
            json.dumps(
                {"marked": args.mark, "day_pages": pages, "state_file": str(path)}, indent=2
            )
        )
        return 0

    if args.pending:
        pending = pending_sources(args.wiki_root, cfg.raw_dir)
        print(json.dumps({"pending": pending}, indent=2))
        return 0

    parser.error("pass --pending or --mark SLUG")
    return 1  # unreachable — parser.error exits


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Write pasted/inline text as a raw source for /log.

Used when /log is given meeting notes or a transcript directly in the
conversation rather than as a file path. The file-path case is instead routed
through ingest.py's existing local-file dispatch (fetch_local.py), which this
script does not duplicate.

Usage:
    python3 write_raw_note.py --wiki-root PATH --title TITLE \
        [--slug SLUG] [--source-label TEXT] < note.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# skills/log/scripts/ -> skills/ingest/scripts/ (sibling skill; config.py and
# raw_store.py are the canonical implementations, not duplicated here).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "ingest" / "scripts"))

from config import ConfigError, load_config  # noqa: E402
from raw_store import write_raw_if_changed  # noqa: E402


_slug_re = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    dashed = _slug_re.sub("-", lowered).strip("-")
    return dashed or "untitled"


def main() -> int:
    parser = argparse.ArgumentParser(description="Write pasted text as a raw source for /log")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--slug", default=None, help="Override the slug (default: slugified title)")
    parser.add_argument(
        "--source-label",
        default="",
        help="Free text describing where this note came from, e.g. 'pasted meeting notes, 2026-09-04'",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    text = sys.stdin.read()
    if not text.strip():
        print("ERROR: no text on stdin — pipe the note's body in", file=sys.stderr)
        return 1

    slug = args.slug or slugify(args.title)
    raw_dir = cfg.raw_dir

    markdown = f"# {args.title}\n\n{text.strip()}\n"
    metadata = {
        "type": "local",
        "kind": "pasted",
        # The rendered raw/<slug>.md IS the faithful copy for pasted text —
        # same convention fetch_local.py uses for a .md/.txt source.
        "path": f"{raw_dir.name}/{slug}.md",
        "filename": None,
        "original_filename": None,
        "original_path": None,
        "source_label": args.source_label,
        "image_hints": [],
    }

    result = write_raw_if_changed(raw_dir, slug, markdown, metadata)

    print(
        json.dumps(
            {
                "slug": slug,
                "title": args.title,
                "raw_md": result["raw_md"],
                "source_json": result["source_json"],
                "image_hints": [],
                "saved_images": [],
                "status": result["status"],
                "content_sha256": result["content_sha256"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

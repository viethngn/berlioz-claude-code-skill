"""Diff-gated writes for raw/<slug>.md and raw/<slug>.source.json.

Every fetcher (Confluence/Jira/local) hands `write_raw_if_changed` the
rendered Markdown plus the git-tracked metadata block. This module:

1. Byte-compares the new Markdown against the on-disk `raw/<slug>.md`.
2. If unchanged AND the tracked metadata block (minus `UNCOMPARED_FIELDS`)
   is identical → does nothing, returns status="unchanged".
3. Otherwise → writes both files, returns status="new" or "changed".

The tracked metadata block MUST NOT contain wall-clock timestamps like
`fetched_at`. Those live in `.wiki-state/last-fetched.json`, written by
the orchestrator, and never touch git.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _canonical_json(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# Written to source.json but never compared: adding source_date (or a touch
# of a local file) must not turn an otherwise-unchanged source into "changed".
UNCOMPARED_FIELDS = ("source_date",)


def _compared(src_text: str) -> str:
    try:
        data = json.loads(src_text)
    except ValueError:
        return src_text
    if not isinstance(data, dict):
        return src_text
    return _canonical_json({k: v for k, v in data.items() if k not in UNCOMPARED_FIELDS})


def write_raw_if_changed(
    raw_dir: Path,
    slug: str,
    markdown: str,
    metadata: dict,
    source_date: Optional[str] = None,
) -> dict:
    """Write raw/<slug>.md and raw/<slug>.source.json only if they differ.

    `source_date` (YYYY-MM-DD, when the source last changed upstream) is
    stored in source.json when given, but ignored by the comparison.

    Returns a dict with:
        status: "new" | "changed" | "unchanged"
        raw_md: str        (path to the .md file)
        source_json: str   (path to the .source.json file)
        content_sha256: str

    The metadata dict must not include volatile fields. `image_hints` is fine
    since it's derived from the source content. Callers should include:
        - type, url, title, and source-specific IDs (page_id, key, path)
        - image_hints
        - content_sha256 (computed here, injected by write_raw_if_changed)
    """
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    md_path = raw_dir / f"{slug}.md"
    src_path = raw_dir / f"{slug}.source.json"

    md_normalized = markdown if markdown.endswith("\n") else markdown + "\n"
    content_sha = _sha256_text(md_normalized)

    metadata_with_hash = {**metadata, "content_sha256": content_sha}
    if source_date:
        metadata_with_hash["source_date"] = source_date
    src_normalized = _canonical_json(metadata_with_hash)

    existing_md = md_path.read_text(encoding="utf-8") if md_path.exists() else None
    existing_src = src_path.read_text(encoding="utf-8") if src_path.exists() else None

    if existing_md is None or existing_src is None:
        status = "new"
    elif existing_md == md_normalized and _compared(existing_src) == _compared(src_normalized):
        status = "unchanged"
    else:
        status = "changed"

    if status != "unchanged":
        md_path.write_text(md_normalized, encoding="utf-8")
        src_path.write_text(src_normalized, encoding="utf-8")

    return {
        "status": status,
        "raw_md": str(md_path),
        "source_json": str(src_path),
        "content_sha256": content_sha,
    }


def iso_date(value: Optional[str]) -> Optional[str]:
    """`YYYY-MM-DD` from an ISO-8601 timestamp string, or None."""
    value = (value or "").strip()
    if len(value) >= 10 and value[4] == "-" and value[7] == "-" and value[:4].isdigit():
        return value[:10]
    return None


def read_previous_content_sha(raw_dir: Path, slug: str) -> Optional[str]:
    """Return the content_sha256 in a previous source.json, if any."""
    src_path = Path(raw_dir) / f"{slug}.source.json"
    if not src_path.exists():
        return None
    try:
        with src_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("content_sha256")


def read_previous_source_metadata(raw_dir: Path, slug: str) -> Optional[dict]:
    """Return the full metadata dict from a previous source.json, if any."""
    src_path = Path(raw_dir) / f"{slug}.source.json"
    if not src_path.exists():
        return None
    try:
        with src_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def wiki_state_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / ".wiki-state"


def write_fetch_history(
    wiki_root: Path,
    slug: str,
    status: str,
    source_ref: str,
) -> Path:
    """Record the latest fetch of a source in .wiki-state/last-fetched.json.

    Not git-tracked. Overwrites the entry for `slug`. Every ingest writes here,
    including "unchanged" fetches — so users can always see the last fetch time.
    """
    state_dir = wiki_state_dir(wiki_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "last-fetched.json"

    data: dict = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    data[slug] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "source_ref": source_ref,
    }

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return path

#!/usr/bin/env python3
"""Download remote image references into raw/images/<slug>/ with dedup.

Reads the source metadata file's `image_hints` (URL + filename). For each
hint, this script:

1. Downloads the URL to an in-memory buffer.
2. Hashes the buffer (SHA-256).
3. Looks up the manifest for `raw/images/<slug>/.manifest.json`:
   - **Same source_url**:
     - Same hash → skip write; the file on disk is already correct.
     - Different hash → overwrite the SAME filename (image was edited upstream).
   - **Different source_url but matching hash** → skip; we already have
     this image under another filename. Add the URL as an alias.
   - **No match anywhere** → save under the next available index.

This is what makes re-ingests idempotent: identical image_hints do not
produce duplicate files, and identical bytes at a new URL don't create
copies.

Uses the Confluence PAT when the image host matches
atlassian.confluence_base_url (needed for authenticated attachment
downloads), and similarly for Jira.

Usage:
    python3 extract_images.py --wiki-root /path/to/wiki --source-json /path/to/raw/<slug>.source.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from _deps import require

require(["requests"])

import requests

from config import ConfigError, apply_ssl_env, load_config
from image_manifest import load_manifest


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def sanitize_filename(name: str, fallback: str) -> str:
    name = name or fallback
    name = name.split("?")[0].split("#")[0]
    name = re.sub(r"[^A-Za-z0-9._\-]+", "-", name).strip("-")
    return name or fallback


def _matches_host(url: str, base_url: str) -> bool:
    if not base_url:
        return False
    try:
        u = urlparse(url)
        b = urlparse(base_url)
    except Exception:
        return False
    return bool(u.netloc) and u.netloc == b.netloc


def _headers_for(url: str, cfg) -> dict:
    if _matches_host(url, cfg.confluence_base_url()) and cfg.confluence_pat():
        return {"Authorization": f"Bearer {cfg.confluence_pat()}"}
    if _matches_host(url, cfg.jira_base_url()) and cfg.jira_pat():
        return {"Authorization": f"Bearer {cfg.jira_pat()}"}
    return {}


def download_to_memory(url: str, headers: dict, verify: bool) -> Optional[bytes]:
    resp = requests.get(url, headers=headers, verify=verify, timeout=60)
    if resp.status_code != 200:
        print(
            f"WARN: could not download {url} — HTTP {resp.status_code}",
            file=sys.stderr,
        )
        return None
    return resp.content


def choose_extension(url: str, filename_hint: str) -> str:
    ext = Path(filename_hint or url).suffix.lower()
    return ext if ext in IMAGE_EXTS else ".png"


def next_index(images_dir: Path) -> int:
    if not images_dir.exists():
        return 0
    used = []
    for p in images_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."):
            m = re.match(r"^(\d+)$", p.stem)
            if m:
                used.append(int(m.group(1)))
    return (max(used) + 1) if used else 0


def find_by_url(manifest, url: str) -> Optional[str]:
    for image_name, entry in manifest.images.items():
        if entry.get("source_url") == url:
            return image_name
    return None


def find_by_sha(manifest, sha: str) -> Optional[str]:
    for image_name, entry in manifest.images.items():
        if entry.get("sha256") == sha:
            return image_name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Download image hints into raw/images/")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--source-json", type=Path, required=True)
    parser.add_argument(
        "--slug",
        default=None,
        help="Override slug (default: derived from source_json filename)",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    source_json = args.source_json.resolve()
    if not source_json.exists():
        print(f"ERROR: source metadata not found: {source_json}", file=sys.stderr)
        return 1
    with source_json.open("r", encoding="utf-8") as f:
        metadata = json.load(f)

    slug = args.slug or source_json.stem.replace(".source", "")
    image_hints = metadata.get("image_hints") or []
    images_dir = cfg.raw_dir / "images" / slug
    manifest = load_manifest(cfg.raw_dir, slug)

    apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())
    verify = cfg.atlassian_verify_ssl()

    counts = {"downloaded_new": 0, "overwritten": 0, "skipped_unchanged": 0, "skipped_alias": 0, "failed": 0}
    results: list[dict] = []
    manifest_dirty = False

    for hint in image_hints:
        url = hint.get("url") if isinstance(hint, dict) else str(hint)
        if not url:
            continue
        filename_hint = hint.get("filename", "") if isinstance(hint, dict) else ""
        ext = choose_extension(url, filename_hint)

        headers = _headers_for(url, cfg)
        payload = download_to_memory(url, headers, verify)
        if payload is None:
            counts["failed"] += 1
            results.append({"url": url, "status": "failed"})
            continue

        sha = hashlib.sha256(payload).hexdigest()
        images_dir.mkdir(parents=True, exist_ok=True)

        by_url = find_by_url(manifest, url)
        if by_url:
            existing_entry = manifest.entry(by_url) or {}
            existing_sha = existing_entry.get("sha256")
            dest = images_dir / by_url
            if existing_sha == sha and dest.exists():
                counts["skipped_unchanged"] += 1
                results.append(
                    {"url": url, "status": "unchanged", "image_name": by_url, "sha256": sha}
                )
                continue
            dest.write_bytes(payload)
            manifest.set_entry(
                by_url,
                sha256=sha,
                description_file=existing_entry.get("description_file"),
                source_url=url,
            )
            manifest_dirty = True
            counts["overwritten"] += 1
            results.append(
                {"url": url, "status": "overwritten", "image_name": by_url, "sha256": sha}
            )
            continue

        by_sha = find_by_sha(manifest, sha)
        if by_sha:
            existing_entry = manifest.entry(by_sha) or {}
            if not existing_entry.get("source_url"):
                manifest.set_entry(
                    by_sha,
                    sha256=sha,
                    description_file=existing_entry.get("description_file"),
                    source_url=url,
                )
                manifest_dirty = True
            counts["skipped_alias"] += 1
            results.append(
                {"url": url, "status": "alias", "image_name": by_sha, "sha256": sha}
            )
            continue

        idx = next_index(images_dir)
        dest = images_dir / f"{idx}{ext}"
        dest.write_bytes(payload)
        manifest.set_entry(
            dest.name,
            sha256=sha,
            description_file=None,
            source_url=url,
        )
        manifest_dirty = True
        counts["downloaded_new"] += 1
        results.append(
            {"url": url, "status": "new", "image_name": dest.name, "sha256": sha}
        )

    if manifest_dirty:
        manifest.save()

    print(
        json.dumps(
            {
                "slug": slug,
                "images_dir": str(images_dir),
                "counts": counts,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

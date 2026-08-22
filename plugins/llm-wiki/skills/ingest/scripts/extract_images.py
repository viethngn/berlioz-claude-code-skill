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
downloads), and similarly for Jira. For `type: "web"` sources it uses the
`web` rate limiter and always sends `web.user_agent`, but `web.extra_headers`
(Cookie, Authorization, …) is sent **only** when the image's origin
(scheme+host+port) matches the page's own — an image embedded from a
third-party CDN must never receive the credentials configured for the site
being ingested, and a configured secret must not cross an http/https boundary
on the same hostname either. It also drops anything smaller than
`web.min_image_bytes` — that floor is what filters out the icons and spacers
whose dimensions weren't in the markup.

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

from _deps import require

require(["requests"])

import requests

from config import ConfigError, apply_ssl_env, load_config
from image_manifest import load_manifest
from rate_limiter import RateLimitFailure, get_limiter
from web_url import same_origin


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# A single oversized attachment/image (malicious or just large) shouldn't be
# able to buffer unbounded bytes in memory before we even hash it. Streamed,
# so the cap is enforced during download, not after it's already too late.
MAX_IMAGE_BYTES = 50 * 1024 * 1024


def sanitize_filename(name: str, fallback: str) -> str:
    name = name or fallback
    name = name.split("?")[0].split("#")[0]
    name = re.sub(r"[^A-Za-z0-9._\-]+", "-", name).strip("-")
    return name or fallback


def _headers_for(url: str, cfg, source_type: str = "", page_url: str = "") -> dict:
    # same_origin() compares scheme+host+port, not just host, so a PAT never
    # goes out over a plaintext http:// URL on an https://-configured host.
    # Gated by source_type too: on a shared-host Atlassian Data Center install
    # (Confluence and Jira on the same domain, different context paths — a
    # common real topology), an image URL's host alone could match either
    # configured base_url regardless of which tool is actually fetching it —
    # gating means a Jira fetch can never pick up the Confluence PAT or vice
    # versa.
    if source_type == "confluence" and same_origin(url, cfg.confluence_base_url()) and cfg.confluence_pat():
        return {"Authorization": f"Bearer {cfg.confluence_pat()}"}
    if source_type == "jira" and same_origin(url, cfg.jira_base_url()) and cfg.jira_pat():
        return {"Authorization": f"Bearer {cfg.jira_pat()}"}
    if source_type == "web":
        # User-Agent is always sent — a bare python-requests UA is 403'd by
        # many CDNs, and it isn't a secret. web.extra_headers (Cookie,
        # Authorization, …) is scoped to the page's own *origin*: an image
        # embedded from a third-party CDN must never receive the credentials
        # configured for the site being ingested. same_origin() compares
        # scheme+host+port, not just host, so a configured secret never crosses
        # an http/https boundary on the same hostname either.
        headers = {"User-Agent": cfg.web_user_agent()}
        if same_origin(url, page_url):
            headers.update(cfg.web_extra_headers())
        return headers
    return {}


def download_to_memory(url: str, headers: dict, verify: bool, limiter=None) -> Optional[bytes]:
    if limiter is None:
        limiter = get_limiter("atlassian", None)
    try:
        resp = limiter.request(
            "GET",
            url,
            headers=headers,
            verify=verify,
            timeout=60,
            stream=True,
            # No-op unless a Cookie is configured; keeps it across same-origin
            # redirects, which requests would otherwise strip.
            follow_redirects_preserving_cookie=True,
        )
    except RateLimitFailure as e:
        print(f"WARN: could not download {url} — {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(
            f"WARN: could not download {url} — HTTP {resp.status_code}",
            file=sys.stderr,
        )
        return None

    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            print(
                f"WARN: {url} exceeds the {MAX_IMAGE_BYTES} byte limit — skipping.",
                file=sys.stderr,
            )
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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
    source_type = str(metadata.get("type") or "")
    page_url = str(metadata.get("url") or "")
    image_hints = metadata.get("image_hints") or []
    images_dir = cfg.raw_dir / "images" / slug
    manifest = load_manifest(cfg.raw_dir, slug)

    # Web images come from arbitrary hosts/CDNs, so they get their own limiter,
    # SSL setting, and User-Agent rather than the Atlassian ones.
    if source_type == "web":
        apply_ssl_env("web", cfg.web_verify_ssl())
        verify = cfg.web_verify_ssl()
        limiter = get_limiter("web", cfg.web)
        min_bytes = cfg.web_min_image_bytes()
    else:
        apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())
        verify = cfg.atlassian_verify_ssl()
        limiter = get_limiter("atlassian", cfg.atlassian)
        min_bytes = 0

    counts = {
        "downloaded_new": 0,
        "overwritten": 0,
        "skipped_unchanged": 0,
        "skipped_alias": 0,
        "skipped_small": 0,
        "failed": 0,
    }
    results: list[dict] = []
    manifest_dirty = False

    for hint in image_hints:
        url = hint.get("url") if isinstance(hint, dict) else str(hint)
        if not url:
            continue
        filename_hint = hint.get("filename", "") if isinstance(hint, dict) else ""
        ext = choose_extension(url, filename_hint)

        headers = _headers_for(url, cfg, source_type, page_url)
        payload = download_to_memory(url, headers, verify, limiter=limiter)
        if payload is None:
            counts["failed"] += 1
            results.append({"url": url, "status": "failed"})
            continue

        # Byte-size floor for web images: markup rarely carries dimensions, so
        # this is where the remaining icons and spacers get dropped. No manifest
        # entry is written, so the description loop never sees them.
        if min_bytes and len(payload) < min_bytes:
            counts["skipped_small"] += 1
            results.append(
                {"url": url, "status": "skipped_small", "bytes": len(payload)}
            )
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

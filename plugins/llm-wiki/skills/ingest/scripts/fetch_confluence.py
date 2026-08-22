#!/usr/bin/env python3
"""Fetch one Confluence page → raw Markdown + source metadata.

Usage:
    python3 fetch_confluence.py --wiki-root /path/to/wiki --url <URL>
    python3 fetch_confluence.py --wiki-root /path/to/wiki --page-id 12345678

Writes:
    raw/<slug>.md              - Markdown-converted body
    raw/<slug>.source.json     - source metadata (url, pageId, fetched_at, etc.)
    raw/images/<slug>/<n>.<ext> - attachment images referenced in the body (via extract_images.py)

Emits a JSON summary to stdout on success:
    {"slug": "...", "title": "...", "raw_md": "...", "source_json": "...", "image_hints": [...]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from _deps import require

require(["requests", "markdownify", "bs4"])

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from config import ConfigError, apply_ssl_env, load_config
from rate_limiter import RateLimitFailure, get_limiter
from raw_store import read_previous_source_metadata, write_raw_if_changed


_slug_re = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(title: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    dashed = _slug_re.sub("-", lowered).strip("-")
    return dashed or "untitled"


def resolve_slug_collision(raw_dir: Path, slug: str, page_id: str) -> str:
    """Return a slug that isn't already owned by a different page.

    slugify(title) collides whenever two pages share a title — common across
    spaces ("Overview", "FAQ", "Home"). Without this, the second page's fetch
    silently overwrites the first's raw/<slug>.md via write_raw_if_changed's
    plain byte-diff, which has no identity check of its own.
    """
    prior = read_previous_source_metadata(raw_dir, slug)
    if not prior:
        return slug
    prior_page_id = str(prior.get("page_id") or "")
    if not prior_page_id or prior_page_id == str(page_id):
        return slug  # same page — keep the slug

    candidate = f"{slug}-{page_id}"
    prior2 = read_previous_source_metadata(raw_dir, candidate)
    if prior2:
        prior2_page_id = str(prior2.get("page_id") or "")
        if prior2_page_id and prior2_page_id != str(page_id):
            raise SystemExit(
                f"ERROR: slug collision could not be resolved for page {page_id}: "
                f"both {slug!r} and {candidate!r} are already owned by other pages "
                f"({prior_page_id}, {prior2_page_id}). Refusing to overwrite."
            )
    print(
        f"WARNING: slug {slug!r} is already used by page {prior_page_id} — a "
        f"different page with the same title. Using {candidate!r} for page "
        f"{page_id} instead.",
        file=sys.stderr,
    )
    return candidate


def extract_page_id(url_or_id: str) -> Optional[str]:
    if url_or_id.isdigit():
        return url_or_id

    parsed = urlparse(url_or_id)
    m = re.search(r"/pages/(\d+)(?:/|$)", parsed.path)
    if m:
        return m.group(1)

    qs = parse_qs(parsed.query)
    if "pageId" in qs and qs["pageId"]:
        return qs["pageId"][0]

    return None


def _api_get(base_url: str, pat: str, page_id: str, expand: str, verify: bool, limiter) -> dict:
    url = f"{base_url}/rest/api/content/{page_id}"
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    try:
        resp = limiter.request(
            "GET", url, headers=headers, params={"expand": expand}, verify=verify, timeout=60
        )
    except RateLimitFailure as e:
        raise SystemExit(f"ERROR: {e}")
    if resp.status_code == 401:
        raise SystemExit(
            "ERROR: 401 Unauthorized. Check atlassian.confluence_pat in .wikirc.json."
        )
    if resp.status_code == 403:
        raise SystemExit(
            f"ERROR: 403 Forbidden — the PAT has no permission for page {page_id}."
        )
    if resp.status_code == 404:
        raise SystemExit(
            f"ERROR: 404 Not Found — page {page_id} does not exist at {base_url}."
        )
    try:
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.HTTPError, ValueError) as e:
        # An unhandled status (5xx, a proxy's 502/504) or a 200 that isn't
        # actually JSON — most plausibly an SSO-fronted instance returning an
        # HTML login page once the PAT has expired — would otherwise surface
        # as a raw traceback instead of a clean error.
        raise SystemExit(
            f"ERROR: unexpected response fetching page {page_id} from {base_url} "
            f"(HTTP {resp.status_code}): {e}"
        )


def fetch_page_version(base_url: str, pat: str, page_id: str, verify: bool, limiter) -> dict:
    """Cheap pre-check: fetch only title + version (no body)."""
    return _api_get(base_url, pat, page_id, "version", verify, limiter)


def fetch_page(base_url: str, pat: str, page_id: str, verify: bool, limiter=None) -> dict:
    if limiter is None:
        limiter = get_limiter("atlassian", None)
    return _api_get(base_url, pat, page_id, "body.storage,version,space,ancestors", verify, limiter)


def normalize_storage(
    body_html: str, base_url: str, page_id: str
) -> tuple[str, list[dict]]:
    """Convert Confluence storage-format XHTML to plain HTML we can markdownify.

    Returns (cleaned_html, image_hints).
    """
    soup = BeautifulSoup(body_html, "html.parser")

    # Drop macro parameters (metadata that isn't user-visible text)
    for tag in soup.find_all(re.compile(r"^ac:parameter$", re.I)):
        tag.decompose()

    # Confluence internal page links → plain [text]
    for link in soup.find_all(re.compile(r"^ac:link$", re.I)):
        ri = link.find(re.compile(r"^ri:page$", re.I))
        title = None
        if ri is not None:
            title = ri.get("ri:content-title") or ri.get("content-title")
        plain_text = link.get_text(strip=True) or title
        if plain_text:
            link.replace_with(f"[{plain_text}]")
        else:
            link.decompose()

    # Attachments → <img src=".../download/attachments/<pageId>/<filename>">
    image_hints: list[dict] = []
    for image in soup.find_all(re.compile(r"^ac:image$", re.I)):
        attach = image.find(re.compile(r"^ri:attachment$", re.I))
        url_tag = image.find(re.compile(r"^ri:url$", re.I))
        if attach is not None:
            filename = attach.get("ri:filename") or attach.get("filename") or ""
            src = f"{base_url}/download/attachments/{page_id}/{filename}"
            image_hints.append({"url": src, "filename": filename, "kind": "attachment"})
        elif url_tag is not None:
            src = url_tag.get("ri:value") or url_tag.get("value") or ""
            image_hints.append({"url": src, "filename": Path(src).name, "kind": "url"})
        else:
            continue
        new_img = soup.new_tag("img", src=src, alt=image.get("ac:alt", "") or "")
        image.replace_with(new_img)

    # Unwrap the remaining ac:* macros — keep inner text
    for tag in list(soup.find_all(re.compile(r"^ac:.*", re.I))):
        tag.unwrap()

    # Also unwrap ri:* leftovers if any
    for tag in list(soup.find_all(re.compile(r"^ri:.*", re.I))):
        tag.unwrap()

    return str(soup), image_hints


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Confluence page into raw/")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--url", help="Confluence page URL")
    parser.add_argument("--page-id", help="Confluence page ID (numeric)")
    parser.add_argument("--force", action="store_true", help="Skip version pre-check and re-fetch")
    args = parser.parse_args()

    if not args.url and not args.page_id:
        parser.error("provide --url or --page-id")

    if args.url:
        parsed_url = urlparse(args.url)
        if parsed_url.username or parsed_url.password:
            print(
                f"ERROR: {args.url!r} contains embedded credentials (user:pass@host). "
                "Strip them from the URL — it gets written verbatim into the committed "
                "raw/<slug>.source.json. Use atlassian.confluence_pat in .wikirc.json instead.",
                file=sys.stderr,
            )
            return 1

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    base_url = cfg.confluence_base_url()
    pat = cfg.confluence_pat()
    if not base_url:
        print(
            "ERROR: atlassian.confluence_base_url is empty in .wikirc.json.",
            file=sys.stderr,
        )
        return 1
    if not pat:
        print(
            "ERROR: atlassian.confluence_pat is empty in .wikirc.json.",
            file=sys.stderr,
        )
        return 1

    apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())

    page_id = args.page_id or extract_page_id(args.url or "")
    if not page_id:
        print(
            f"ERROR: could not extract a numeric page ID from {args.url!r}.",
            file=sys.stderr,
        )
        return 1

    verify = cfg.atlassian_verify_ssl()
    limiter = get_limiter("atlassian", cfg.atlassian)

    # --- Version pre-check: one cheap API call before fetching the full body ---
    if not args.force:
        version_data = fetch_page_version(base_url, pat, page_id, verify, limiter)
        current_version = (version_data.get("version") or {}).get("number")
        # Determine what slug would be used to find any prior source.json.
        # We need at least the title for slugification; it's available here.
        tentative_title = version_data.get("title") or f"Confluence Page {page_id}"
        tentative_slug = slugify(tentative_title)
        prior_meta = read_previous_source_metadata(cfg.raw_dir, tentative_slug)
        # Two guards before trusting the cache: the cached entry must actually
        # BE this page (a same-titled different page at the same version number
        # is the common case — freshly created pages both sitting at version 1),
        # and the raw file it refers to must still exist on disk.
        is_same_page = (
            prior_meta is not None
            and str(prior_meta.get("page_id") or "") == str(page_id)
        )
        raw_file_exists = (cfg.raw_dir / f"{tentative_slug}.md").exists()
        if prior_meta is not None and current_version is not None and is_same_page and raw_file_exists:
            stored_version = prior_meta.get("version_number")
            if stored_version is not None and int(current_version) == int(stored_version):
                summary = {
                    "slug": tentative_slug,
                    "title": tentative_title,
                    "raw_md": str(cfg.raw_dir / f"{tentative_slug}.md"),
                    "source_json": str(cfg.raw_dir / f"{tentative_slug}.source.json"),
                    "image_hints": prior_meta.get("image_hints", []),
                    "status": "unchanged",
                    "content_sha256": prior_meta.get("content_sha256", ""),
                    "version_number": current_version,
                    "note": f"Version {current_version} unchanged since last ingest. Pass --force to re-fetch.",
                }
                print(json.dumps(summary, indent=2, ensure_ascii=False))
                return 0

    page = fetch_page(base_url, pat, page_id, verify, limiter=limiter)

    title = page.get("title") or f"Confluence Page {page_id}"
    body_html = (page.get("body", {}).get("storage") or {}).get("value", "") or ""
    if not body_html.strip():
        print(
            f"ERROR: page {page_id} has an empty body — skipping.",
            file=sys.stderr,
        )
        return 1

    cleaned_html, image_hints = normalize_storage(body_html, base_url, page_id)
    markdown = markdownify(
        cleaned_html,
        heading_style="ATX",
        bullets="-",
        code_language="",
    )

    slug = slugify(title)
    slug = resolve_slug_collision(cfg.raw_dir, slug, page_id)
    version = page.get("version", {})
    metadata = {
        "type": "confluence",
        "page_id": page_id,
        "url": (args.url or f"{base_url}/pages/{page_id}/{slug}"),
        "title": title,
        "space_key": (page.get("space") or {}).get("key"),
        "version_number": version.get("number"),
        "version_when": version.get("when"),
        "image_hints": image_hints,
    }

    full_markdown = f"# {title}\n\n{markdown.strip()}\n"
    result = write_raw_if_changed(cfg.raw_dir, slug, full_markdown, metadata)

    summary = {
        "slug": slug,
        "title": title,
        "raw_md": result["raw_md"],
        "source_json": result["source_json"],
        "image_hints": image_hints,
        "status": result["status"],
        "content_sha256": result["content_sha256"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

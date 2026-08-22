#!/usr/bin/env python3
"""Fetch one web page → raw Markdown + source metadata.

Usage:
    python3 fetch_web.py --wiki-root /path/to/wiki --url https://example.com/docs/intro

Writes:
    raw/<slug>.md              - Markdown-converted page content
    raw/<slug>.source.json     - source metadata (url, title, extractor, image_hints)

Emits a JSON summary to stdout on success, in the same shape as the other
fetchers so ingest.py / prefetch.py need no special handling:
    {"slug", "title", "raw_md", "source_json", "image_hints", "status", "content_sha256"}

Extraction is trafilatura first (best boilerplate removal), with a
BeautifulSoup + markdownify fallback when trafilatura yields nothing. The
extractor used is recorded in source.json so a switch shows up in the diff
rather than silently changing the page.

Re-fetches are cheap: the ETag / Last-Modified from the previous fetch are
replayed as If-None-Match / If-Modified-Since, so an unchanged page costs one
304 and no parsing. Those validators live in .wiki-state/ (volatile, not git)
because some servers rotate ETags per request, which would otherwise churn
source.json and defeat the content-diff gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from _deps import require

require(["requests", "markdownify", "bs4"])

from bs4 import BeautifulSoup
from markdownify import markdownify

from config import ConfigError, apply_ssl_env, load_config
from rate_limiter import RateLimitFailure, get_limiter
from raw_store import wiki_state_dir, write_raw_if_changed
from web_url import disambiguate_slug, normalize_url, slug_identity, web_slug

# Tags that never carry page content.
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
)

# Selectors that identify the main content region, best first.
_MAIN_SELECTORS = ("main", "article", "[role=main]", "#content", ".content", "#main")

# Images that are chrome, not content.
_CHROME_IMAGE_RE = re.compile(
    r"logo|icon|avatar|sprite|badge|pixel|tracking|spacer|favicon|thumb(nail)?[-_]?small",
    re.I,
)

_SKIP_IMAGE_EXT_RE = re.compile(r"\.(svg|ico)(\?|#|$)", re.I)

# Below this, an <img> with explicit dimensions is decoration.
_MIN_IMAGE_DIMENSION = 100

# Beyond this many images on one page we stop collecting and warn.
_MAX_IMAGE_HINTS = 20


# --------------------------------------------------------- volatile validators


def _validator_key(slug: str) -> str:
    """Key for .wiki-state/last-fetched.json.

    Deliberately NOT the bare slug: write_fetch_history() in raw_store.py
    overwrites data[<slug>] wholesale on every ingest, which would wipe the
    validators we store here. fetch_slack.py uses a prefixed key for the same
    reason.
    """
    return f"web:{slug}"


def resolve_slug_collision(raw_dir: Path, slug: str, url: str) -> str:
    """Return a slug that isn't already owned by a *different* page.

    web_slug() flattens `/` and `-` identically, so `/a/b` and `/a-b` produce
    the same slug. Without this check the second page silently overwrites the
    first's raw/<slug>.md — worst of all during an unattended bulk sitemap run.

    Same-page re-ingests (including http→https of the same page) compare equal
    via slug_identity() and keep their existing slug, so the diff gate and the
    conditional-GET cache still work.
    """
    from raw_store import read_previous_source_metadata

    prior = read_previous_source_metadata(raw_dir, slug)
    if not prior:
        return slug
    prior_url = str(prior.get("url") or "")
    if not prior_url or slug_identity(prior_url) == slug_identity(url):
        return slug  # same page — keep the slug

    candidate = disambiguate_slug(slug, url)
    prior2 = read_previous_source_metadata(raw_dir, candidate)
    if prior2:
        prior2_url = str(prior2.get("url") or "")
        if prior2_url and slug_identity(prior2_url) != slug_identity(url):
            raise SystemExit(
                f"ERROR: slug collision could not be resolved: both {slug!r} and "
                f"{candidate!r} are already owned by other pages ({prior_url}, "
                f"{prior2_url}). Refusing to overwrite. Report this — it should be "
                "practically impossible."
            )
    print(
        f"WARNING: slug {slug!r} is already used by {prior_url} — a different page "
        f"that flattens to the same slug. Using {candidate!r} for {url} instead.",
        file=sys.stderr,
    )
    return candidate


def _raw_files_exist(raw_dir: Path, slug: str) -> bool:
    """True only when both raw files for a slug are present.

    Guards the conditional-GET path: if either file was deleted (by hand, or
    lost from disk some other way), a server-side 304 must not be trusted —
    there's nothing on disk for it to mean "unchanged" relative to.
    """
    return (raw_dir / f"{slug}.md").exists() and (raw_dir / f"{slug}.source.json").exists()


def _read_validators(wiki_root: Path, req_slug: str) -> dict:
    """Look up cached validators by the *requested* URL's slug.

    Keying by the request (not the post-redirect content slug) is what makes
    a redirected URL cacheable at all: the request slug is known before any
    HTTP call happens, while the content slug is only known after following
    redirects. See _write_validators for how the two are reconciled.
    """
    path = wiki_state_dir(wiki_root) / "last-fetched.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    entry = data.get(_validator_key(req_slug))
    return entry if isinstance(entry, dict) else {}


def _write_validators(
    wiki_root: Path,
    req_slug: str,
    req_url: str,
    resolved_slug: str,
    resolved_url: str,
    etag: str,
    last_modified: str,
) -> None:
    """Record validators under the requested slug, and the resolved slug too.

    Writing under both means a later `--url <old-redirected-url>` and a later
    `--url <the-redirect-target>` both hit the cache, instead of the second
    one silently missing it because it was written under a different key.

    `identity` records which *requested* URL each key belongs to — so it differs
    between the two entries when a redirect is involved. Two different pages can
    share a req_slug (that's the collision resolve_slug_collision() handles), and
    the identity is what stops the second page's ETag from being replayed against
    the first page's URL. It must therefore be keyed to what the caller asks for,
    not to where that request landed.
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

    def build(identity_url: str) -> dict:
        # Rebuild rather than merge: a stale etag from a *different* page that
        # shared this key must not survive.
        entry = {"slug": resolved_slug, "identity": slug_identity(identity_url)}
        if etag:
            entry["etag"] = etag
        if last_modified:
            entry["last_modified"] = last_modified
        return entry

    data[_validator_key(req_slug)] = build(req_url)
    if resolved_slug != req_slug:
        data[_validator_key(resolved_slug)] = build(resolved_url)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


# ----------------------------------------------------------------- extraction


def _pick_main(soup: BeautifulSoup):
    """Return the content subtree: <main>/<article>/… if present, else <body>."""
    for selector in _MAIN_SELECTORS:
        try:
            node = soup.select_one(selector)
        except Exception:  # noqa: BLE001 — malformed selector support varies
            node = None
        if node is not None and node.get_text(strip=True):
            return node
    return soup.body or soup


def extract_with_bs4(html: str) -> tuple[str, Optional[BeautifulSoup]]:
    """Fallback extraction: strip chrome, markdownify the content subtree.

    Mirrors fetch_local.parse_html so a saved page and a fetched page render
    the same way. Returns (markdown, content_node).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    node = _pick_main(soup)
    markdown = markdownify(str(node), heading_style="ATX", bullets="-").strip()
    return markdown, node


def extract_with_trafilatura(html: str, url: str) -> str:
    """Primary extraction. Returns "" when trafilatura finds no main content."""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        result = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            include_images=False,
            favor_precision=True,
        )
    except Exception as e:  # noqa: BLE001 — never let extraction crash the fetch
        print(f"WARNING: trafilatura failed on {url} ({e}); falling back to bs4.", file=sys.stderr)
        return ""
    return (result or "").strip()


def extract_title(soup: Optional[BeautifulSoup], url: str) -> str:
    if soup is not None:
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            if title:
                return re.sub(r"\s+", " ", title)
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
            if title:
                return re.sub(r"\s+", " ", title)
    path = (urlparse(url).path or "").rstrip("/")
    if path:
        stem = path.rsplit("/", 1)[-1]
        stem = re.sub(r"\.(html?|php|asp|aspx|jsp)$", "", stem, flags=re.I)
        if stem:
            return stem.replace("-", " ").replace("_", " ").strip().title()
    return urlparse(url).netloc or url


# --------------------------------------------------------------- image hints


def _largest_srcset(srcset: str) -> str:
    """Pick the highest-width candidate from a srcset attribute."""
    best, best_width = "", -1.0
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0]
        width = 0.0
        if len(parts) > 1:
            m = re.match(r"^(\d+(?:\.\d+)?)([wx])$", parts[1])
            if m:
                width = float(m.group(1))
        if width > best_width:
            best, best_width = url, width
    return best


def _is_decoration(img) -> bool:
    haystack = " ".join(
        str(img.get(attr) or "")
        for attr in ("src", "class", "id", "alt", "role")
    )
    if _CHROME_IMAGE_RE.search(haystack):
        return True
    for attr in ("width", "height"):
        raw = str(img.get(attr) or "").strip().rstrip("px")
        if raw.isdigit() and int(raw) < _MIN_IMAGE_DIMENSION:
            return True
    return False


def collect_image_hints(node, page_url: str) -> list[dict]:
    """Collect content-image URLs from the extracted subtree only.

    Restricting to the content subtree removes most nav/footer chrome before
    any pattern matching happens; the filters below catch the rest. A final
    byte-size floor is applied at download time in extract_images.py, since
    dimensions often aren't in the markup.
    """
    if node is None:
        return []

    hints: list[dict] = []
    seen: set[str] = set()
    truncated = False

    for img in node.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            srcset = (img.get("srcset") or "").strip()
            src = _largest_srcset(srcset) if srcset else ""
        if not src or src.startswith("data:"):
            continue
        if _SKIP_IMAGE_EXT_RE.search(src):
            continue
        if _is_decoration(img):
            continue

        absolute = urljoin(page_url, src)
        if urlparse(absolute).scheme not in {"http", "https"}:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)

        if len(hints) >= _MAX_IMAGE_HINTS:
            truncated = True
            break

        hints.append(
            {
                "url": absolute,
                "filename": Path(urlparse(absolute).path).name,
                "kind": "web",
                "alt": (img.get("alt") or "").strip(),
            }
        )

    if truncated:
        print(
            f"WARNING: {page_url} has more than {_MAX_IMAGE_HINTS} content images; "
            "only the first were kept.",
            file=sys.stderr,
        )
    return hints


# ----------------------------------------------------------------------- main


def _check_robots(url: str, cfg, limiter) -> None:
    """Warn (never block) when robots.txt disallows an explicitly-named page.

    The user asked for this one URL, so we surface the signal and continue.
    The automated bulk paths (sitemap / crawl) enforce robots instead.
    """
    if not cfg.web_respect_robots():
        return
    try:
        from web_discover import load_robots
        from web_url import origin_of

        # Same origin as the page the user named, so extra_headers apply.
        robots = load_robots(url, cfg, limiter, primary_origin=origin_of(url))
        if not robots.can_fetch(cfg.web_user_agent(), url):
            print(
                f"WARNING: robots.txt at {urlparse(url).netloc} disallows {url}. "
                "Ingesting anyway because you named this page explicitly.",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — robots is advisory here
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a web page into raw/")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--url", required=True, help="Page URL (http/https)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the conditional-GET pre-check and re-fetch the page body",
    )
    parser.add_argument(
        "--no-robots-check",
        action="store_true",
        help="Skip the advisory robots.txt lookup (saves one request)",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    url = normalize_url(args.url)
    parsed = urlparse(url)
    # netloc matters as much as the scheme: 'https:///path' passes a
    # scheme-only check and then tracebacks inside requests as InvalidURL.
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print(
            f"ERROR: {args.url!r} is not a valid http(s) URL (missing scheme or host).",
            file=sys.stderr,
        )
        return 1
    # Embedded credentials would end up in the slug (and so in filenames) and
    # in the committed raw/<slug>.source.json. Refuse them outright.
    if parsed.username or parsed.password:
        print(
            f"ERROR: {args.url!r} contains embedded credentials (user:pass@host). "
            "Strip them from the URL and put the credentials in "
            "`web.extra_headers` in .wikirc.json instead (e.g. a Cookie or "
            "Authorization header) — those are redacted when the config is printed "
            "and never written to raw/.",
            file=sys.stderr,
        )
        return 1

    apply_ssl_env("web", cfg.web_verify_ssl())
    limiter = get_limiter("web", cfg.web)
    # req_slug identifies the URL as requested — the only thing known before
    # any HTTP call. If the server redirects, the eventual content slug (used
    # for raw/ files) may differ; see the write-back below.
    req_slug = web_slug(url)
    req_url = url  # preserved: `url` is reassigned to the final URL after redirects
    slug = req_slug

    if not args.no_robots_check:
        _check_robots(url, cfg, limiter)

    # --- Conditional GET: one cheap request when the page hasn't changed ---
    headers = {**cfg.web_headers(), "Accept": "text/html,application/xhtml+xml,*/*"}
    validators: dict = {}
    if not args.force:
        validators = _read_validators(cfg.wiki_root, req_slug)
        cached_slug = validators.get("slug") or req_slug
        cached_identity = validators.get("identity")
        # An entry written by a *different* page that shares this req_slug must
        # not have its ETag replayed against this URL. Entries predating the
        # identity field have no identity — treat those as usable.
        identity_ok = cached_identity is None or cached_identity == slug_identity(url)
        if validators and identity_ok and _raw_files_exist(cfg.raw_dir, cached_slug):
            if validators.get("etag"):
                headers["If-None-Match"] = validators["etag"]
            if validators.get("last_modified"):
                headers["If-Modified-Since"] = validators["last_modified"]
        else:
            # No cached raw files to call "unchanged" relative to (first
            # ingest, or the raw files were deleted), or the cached entry
            # belongs to another page — send a plain GET so a stale
            # server-side ETag can't produce a false 304.
            validators = {}

    try:
        resp = limiter.request(
            "GET",
            url,
            headers=headers,
            verify=cfg.web_verify_ssl(),
            timeout=cfg.web_timeout(),
            allow_redirects=True,
            # requests drops a header-supplied Cookie on every redirect hop;
            # keep it across same-origin ones (e.g. an http→https upgrade).
            follow_redirects_preserving_cookie=True,
        )
    except RateLimitFailure as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if resp.status_code == 304:
        from raw_store import read_previous_source_metadata

        # validators is non-empty here only when we actually sent conditional
        # headers, which only happens when _raw_files_exist() already passed
        # for this slug — so trusting the 304 below is safe.
        slug = validators.get("slug") or req_slug
        prior = read_previous_source_metadata(cfg.raw_dir, slug) or {}
        summary = {
            "slug": slug,
            "title": prior.get("title") or slug,
            "raw_md": str(cfg.raw_dir / f"{slug}.md"),
            "source_json": str(cfg.raw_dir / f"{slug}.source.json"),
            "image_hints": prior.get("image_hints", []),
            "status": "unchanged",
            "content_sha256": prior.get("content_sha256", ""),
            "note": "HTTP 304 Not Modified since the last ingest. Pass --force to re-fetch.",
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if resp.status_code in (401, 403):
        print(
            f"ERROR: HTTP {resp.status_code} fetching {url}. The page needs "
            "authentication or blocks this client. Add a Cookie/Authorization header "
            "or a browser User-Agent under `web.extra_headers` / `web.user_agent` in "
            ".wikirc.json.",
            file=sys.stderr,
        )
        return 1
    if resp.status_code == 404:
        print(f"ERROR: HTTP 404 — {url} does not exist.", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(f"ERROR: HTTP {resp.status_code} fetching {url}.", file=sys.stderr)
        return 1

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        print(
            f"ERROR: {url} returned Content-Type {content_type!r}, not HTML. "
            "Download it and ingest the file instead: /ingest <path-to-file> "
            "(PDF, DOCX, XLSX, PPTX, CSV and images are all supported).",
            file=sys.stderr,
        )
        return 1

    # requests guesses latin-1 for text/* without a charset; let it sniff instead.
    if resp.encoding is None or "charset" not in (resp.headers.get("Content-Type") or "").lower():
        resp.encoding = resp.apparent_encoding or resp.encoding
    html = resp.text

    # Final URL after redirects — that's what we actually ingested.
    final_url = normalize_url(str(resp.url) or url)
    if final_url != url:
        slug = web_slug(final_url)
        url = final_url

    # Guard against two different pages flattening to one slug. Must run before
    # anything writes using `slug` (metadata, raw files, validators).
    slug = resolve_slug_collision(cfg.raw_dir, slug, url)

    markdown = extract_with_trafilatura(html, url)
    extractor = "trafilatura"
    fallback_markdown, node = extract_with_bs4(html)
    if not markdown:
        markdown = fallback_markdown
        extractor = "bs4"

    title = extract_title(BeautifulSoup(html, "html.parser"), url)

    if not markdown.strip():
        print(
            f"ERROR: no readable content extracted from {url}. The page is most likely "
            "rendered client-side by JavaScript, which this fetcher does not execute. "
            "Save the rendered page from your browser (Save As → Web Page, Complete) "
            "and ingest the local .html file instead.",
            file=sys.stderr,
        )
        return 1

    image_hints = collect_image_hints(node, url)

    metadata = {
        "type": "web",
        "url": url,
        "title": title,
        "site": urlparse(url).netloc,
        "extractor": extractor,
        "image_hints": image_hints,
    }

    body = markdown.strip()
    # Avoid a duplicate H1 when the extractor already emitted the title.
    if body.startswith("# "):
        full_markdown = f"{body}\n"
    else:
        full_markdown = f"# {title}\n\n{body}\n"

    result = write_raw_if_changed(cfg.raw_dir, slug, full_markdown, metadata)

    _write_validators(
        cfg.wiki_root,
        req_slug,
        req_url,
        slug,
        url,
        resp.headers.get("ETag", "") or "",
        resp.headers.get("Last-Modified", "") or "",
    )

    summary = {
        "slug": slug,
        "title": title,
        "raw_md": result["raw_md"],
        "source_json": result["source_json"],
        "image_hints": image_hints,
        "status": result["status"],
        "content_sha256": result["content_sha256"],
        "extractor": extractor,
        "url": url,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Enumerate the pages of a website — via sitemap, or via a bounded crawl.

Used by `discover.py` to build a bulk job queue for `web_sitemap` /
`web_crawl` jobs. Every HTTP call goes through the shared `web` rate limiter,
so a whole-site enumeration stays polite and honors Retry-After.

Discovery order for a bare site URL:

1. `Sitemap:` directives in /robots.txt (authoritative — a site can point
   anywhere, including another host or a non-standard filename).
2. /sitemap.xml, then /sitemap_index.xml, then /sitemap.xml.gz.
3. Nothing found → the caller asks the user for explicit crawl bounds.

Requires `requests`.
"""

from __future__ import annotations

import gzip
import io
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional, Sequence, Set
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from rate_limiter import RateLimitFailure

from web_url import (
    join_base,
    looks_non_html,
    normalize_url,
    origin_of,
    parse_lastmod,
    same_origin,
)


# Standard locations to probe when robots.txt names no sitemap.
SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap.xml.gz",
)

# How deep a <sitemapindex> may nest before we stop following.
MAX_SITEMAP_DEPTH = 3

# Hard ceiling so a malformed or hostile sitemap can't exhaust memory.
MAX_SITEMAP_URLS = 50000

# Byte ceilings for sitemap fetches. MAX_SITEMAP_URLS only caps *parsed entries*,
# which is far too late to help: the response body is already fully in memory by
# then. These bound the transfer itself and the gzip expansion (a .gz sitemap is
# a compression-bomb vector otherwise).
MAX_SITEMAP_RAW_BYTES = 50 * 1024 * 1024
MAX_SITEMAP_DECOMPRESSED_BYTES = 200 * 1024 * 1024


class WebDiscoveryError(Exception):
    """Discovery could not proceed (network, or an unusable response)."""


def _local_name(tag: str) -> str:
    """Strip an XML namespace: '{ns}urlset' → 'urlset'."""
    return tag.rsplit("}", 1)[-1].lower()


def _scoped_headers(url: str, cfg, primary_origin: Optional[str], accept: str) -> dict:
    """Headers for a discovery request, with credentials scoped to one origin.

    `web.extra_headers` (Cookie, Authorization, …) is configured for the site the
    user asked to ingest. Discovery legitimately reaches other hosts — a sitemap
    can list URLs on another origin, and we then fetch *that* origin's robots.txt
    and possibly its nested sitemap files. Those hosts must never receive the
    entry-point site's credentials. User-Agent is always sent; it isn't a secret.

    `primary_origin=None` means "unscoped" (send extra_headers), preserving
    behavior for any caller that hasn't been threaded yet.
    """
    headers = {"User-Agent": cfg.web_user_agent(), "Accept": accept}
    if primary_origin is None or origin_of(url) == primary_origin:
        headers.update(cfg.web_extra_headers())
    return headers


def _get(
    url: str,
    cfg,
    limiter,
    *,
    accept: str = "*/*",
    timeout: Optional[int] = None,
    primary_origin: Optional[str] = None,
    stream: bool = False,
):
    headers = _scoped_headers(url, cfg, primary_origin, accept)
    try:
        return limiter.request(
            "GET",
            url,
            headers=headers,
            verify=cfg.web_verify_ssl(),
            timeout=timeout or cfg.web_timeout(),
            allow_redirects=True,
            stream=stream,
            follow_redirects_preserving_cookie=True,
        )
    except RateLimitFailure as e:
        raise WebDiscoveryError(str(e)) from e


# ---------------------------------------------------------------- robots.txt


def load_robots(
    site_url: str, cfg, limiter, primary_origin: Optional[str] = None
) -> RobotFileParser:
    """Fetch and parse /robots.txt for a site's origin.

    We fetch it ourselves rather than using RobotFileParser.read(), which
    would use urllib directly and bypass our rate limiter, User-Agent, and
    verify_ssl setting. Status handling mirrors urllib's: 401/403 means
    "assume everything is disallowed", other 4xx means "no restrictions".
    """
    parser = RobotFileParser()
    robots_url = join_base(site_url, "/robots.txt")
    parser.set_url(robots_url)

    try:
        resp = _get(
            robots_url, cfg, limiter, accept="text/plain", primary_origin=primary_origin
        )
    except WebDiscoveryError as e:
        print(f"WARNING: could not fetch {robots_url} — {e}", file=sys.stderr)
        parser.parse([])
        parser.allow_all = True
        return parser

    if resp.status_code in (401, 403):
        parser.parse([])
        parser.disallow_all = True
        print(
            f"WARNING: {robots_url} returned HTTP {resp.status_code} — per the robots "
            "standard this means 'disallow everything'. Pass --ignore-robots to override.",
            file=sys.stderr,
        )
        return parser

    if resp.status_code >= 400:
        parser.parse([])
        parser.allow_all = True
        return parser

    parser.parse(resp.text.splitlines())
    return parser


def robots_sitemaps(parser: RobotFileParser) -> List[str]:
    try:
        maps = parser.site_maps()
    except AttributeError:  # pragma: no cover — Python < 3.8
        maps = None
    return list(maps or [])


def crawl_delay_for(parser: RobotFileParser, user_agent: str) -> Optional[float]:
    try:
        delay = parser.crawl_delay(user_agent) or parser.crawl_delay("*")
    except Exception:  # noqa: BLE001 — robotparser can raise on odd input
        return None
    try:
        return float(delay) if delay is not None else None
    except (TypeError, ValueError):
        return None


class RobotsCache:
    """Lazily loads and caches one RobotFileParser per origin.

    A sitemap can legitimately list URLs on a different host than the one it
    was fetched from (a docs subdomain listed from the main site's sitemap,
    for example). Checking every entry against a single, entry-point robots
    parser would apply the wrong site's policy to those URLs — each entry
    must be checked against its *own* origin's robots.txt.
    """

    def __init__(
        self,
        cfg,
        limiter,
        seed: Optional[tuple] = None,
        primary_origin: Optional[str] = None,
    ):
        self._cfg = cfg
        self._limiter = limiter
        self._cache: dict = {}
        # Foreign origins reached via a sitemap must not receive the entry-point
        # site's configured Cookie/Authorization when we fetch their robots.txt.
        self._primary_origin = primary_origin
        if seed is not None:
            origin, parser = seed
            self._cache[origin] = parser

    def for_url(self, url: str) -> RobotFileParser:
        origin = origin_of(url)
        if origin not in self._cache:
            self._cache[origin] = load_robots(
                url, self._cfg, self._limiter, primary_origin=self._primary_origin
            )
        return self._cache[origin]

    def can_fetch(self, url: str) -> bool:
        return self.for_url(url).can_fetch(self._cfg.web_user_agent(), url)


# ------------------------------------------------------------------ sitemaps


def _looks_like_sitemap(payload: bytes) -> bool:
    head = payload[:2048].lstrip()
    if head.startswith(b"<"):
        lowered = head.lower()
        return b"<urlset" in lowered or b"<sitemapindex" in lowered
    # Plain-text sitemaps are one URL per line.
    return head.startswith(b"http://") or head.startswith(b"https://")


def _read_capped(resp, max_bytes: int, what: str) -> bytes:
    """Read a streamed response body, aborting past `max_bytes`.

    Streaming rather than touching resp.content is the point: it bounds memory
    even against a server that never stops sending.
    """
    chunks: list = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise WebDiscoveryError(
                f"{what} exceeds the {max_bytes} byte limit — refusing to load it. "
                "Point --sitemap at a smaller sitemap, or narrow the scope."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decompress(url: str, payload: bytes) -> bytes:
    """Gunzip if needed, with a cap so a compression bomb can't exhaust memory."""
    if payload[:2] == b"\x1f\x8b" or url.lower().endswith(".gz"):
        limit = MAX_SITEMAP_DECOMPRESSED_BYTES
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gz:
                # Read one byte past the limit: if we get it, the real payload is
                # over the cap. Truncating silently would hand a half-document to
                # the XML parser instead.
                out = gz.read(limit + 1)
        except (OSError, EOFError) as e:
            raise WebDiscoveryError(f"could not gunzip {url}: {e}")
        if len(out) > limit:
            raise WebDiscoveryError(
                f"{url} expands to more than {limit} bytes when decompressed "
                "(possible compression bomb) — refusing to load it."
            )
        return out
    return payload


def find_sitemaps(
    site_url: str,
    cfg,
    limiter,
    robots: Optional[RobotFileParser] = None,
    primary_origin: Optional[str] = None,
) -> List[str]:
    """Return every sitemap URL discoverable for a site, best source first."""
    found: List[str] = []
    seen: Set[str] = set()

    def add(candidate: str) -> None:
        normalized = normalize_url(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(candidate)

    if robots is not None:
        for sm in robots_sitemaps(robots):
            add(sm)

    if found:
        return found

    for path in SITEMAP_CANDIDATES:
        candidate = join_base(site_url, path)
        try:
            resp = _get(
                candidate,
                cfg,
                limiter,
                accept="application/xml,text/xml,text/plain",
                primary_origin=primary_origin,
                stream=True,
            )
            if resp.status_code != 200:
                continue
            payload = _decompress(
                candidate,
                _read_capped(resp, MAX_SITEMAP_RAW_BYTES, f"sitemap {candidate}"),
            )
        except WebDiscoveryError:
            continue
        if _looks_like_sitemap(payload):
            add(candidate)
            break

    return found


def parse_sitemap(
    url: str,
    cfg,
    limiter,
    *,
    depth: int = 0,
    visited: Optional[Set[str]] = None,
    robots_cache: Optional[RobotsCache] = None,
    primary_origin: Optional[str] = None,
) -> List[dict]:
    """Fetch one sitemap and return [{loc, lastmod}], recursing into indexes.

    `robots_cache`, if given, gates fetching each *nested* sitemap against its
    own origin's robots.txt — a <sitemapindex> entry can point at a different
    host than the one this sitemap was fetched from.
    """
    visited = visited if visited is not None else set()
    normalized_self = normalize_url(url)
    if normalized_self in visited or depth > MAX_SITEMAP_DEPTH:
        return []
    visited.add(normalized_self)

    resp = _get(
        url,
        cfg,
        limiter,
        accept="application/xml,text/xml,text/plain",
        primary_origin=primary_origin,
        stream=True,
    )
    if resp.status_code != 200:
        raise WebDiscoveryError(f"HTTP {resp.status_code} fetching sitemap {url}")

    payload = _decompress(
        url, _read_capped(resp, MAX_SITEMAP_RAW_BYTES, f"sitemap {url}")
    )
    stripped = payload.lstrip()

    # Plain-text sitemap: one URL per line.
    if not stripped.startswith(b"<"):
        entries = []
        for line in payload.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(("http://", "https://")):
                entries.append({"loc": line, "lastmod": None})
        return entries

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as e:
        raise WebDiscoveryError(f"could not parse sitemap XML at {url}: {e}")

    root_name = _local_name(root.tag)
    entries: List[dict] = []

    if root_name == "sitemapindex":
        for child in root:
            if _local_name(child.tag) != "sitemap":
                continue
            loc = _child_text(child, "loc")
            if not loc:
                continue
            if robots_cache is not None and not robots_cache.can_fetch(loc):
                print(
                    f"WARNING: robots.txt disallows fetching nested sitemap {loc} — skipping",
                    file=sys.stderr,
                )
                continue
            try:
                entries.extend(
                    parse_sitemap(
                        loc,
                        cfg,
                        limiter,
                        depth=depth + 1,
                        visited=visited,
                        robots_cache=robots_cache,
                        primary_origin=primary_origin,
                    )
                )
            except WebDiscoveryError as e:
                print(f"WARNING: skipping nested sitemap {loc} — {e}", file=sys.stderr)
            if len(entries) >= MAX_SITEMAP_URLS:
                print(
                    f"WARNING: stopping sitemap expansion at {MAX_SITEMAP_URLS} URLs "
                    "— narrow the scope with --include / --since / --limit.",
                    file=sys.stderr,
                )
                break
        return entries

    if root_name == "urlset":
        for child in root:
            if _local_name(child.tag) != "url":
                continue
            loc = _child_text(child, "loc")
            if not loc:
                continue
            entries.append({"loc": loc, "lastmod": parse_lastmod(_child_text(child, "lastmod"))})
            if len(entries) >= MAX_SITEMAP_URLS:
                print(
                    f"WARNING: stopping sitemap expansion at {MAX_SITEMAP_URLS} URLs "
                    "— narrow the scope with --include / --since / --limit.",
                    file=sys.stderr,
                )
                break
        return entries

    raise WebDiscoveryError(
        f"{url} is not a sitemap (root element <{root_name}>)"
    )


def _child_text(element, name: str) -> Optional[str]:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def collect_sitemap_urls(
    sitemaps: Sequence[str],
    cfg,
    limiter,
    *,
    robots_cache: Optional[RobotsCache] = None,
    primary_origin: Optional[str] = None,
) -> List[dict]:
    """Expand every sitemap into a deduplicated, normalized entry list.

    `robots_cache` is used only to gate fetching *nested sitemap files* here.
    The per-page robots check is a separate pass (`filter_by_robots`) that the
    caller runs after `apply_filters`, so a URL that a filter would drop anyway
    never costs a robots.txt fetch for its origin.
    """
    entries: List[dict] = []
    seen: Set[str] = set()
    visited: Set[str] = set()
    dropped_non_http = 0

    for sitemap in sitemaps:
        try:
            raw_entries = parse_sitemap(
                sitemap,
                cfg,
                limiter,
                visited=visited,
                robots_cache=robots_cache,
                primary_origin=primary_origin,
            )
        except WebDiscoveryError as e:
            print(f"WARNING: skipping sitemap {sitemap} — {e}", file=sys.stderr)
            continue
        for entry in raw_entries:
            raw_loc = entry.get("loc") or ""
            # A sitemap <loc> is only ever supposed to be a page URL, but some
            # sitemaps carry mailto:/ftp:/etc. entries by mistake. Reject them
            # here rather than letting them reach the queue and fail late in
            # fetch_web.py.
            if urlparse(raw_loc).scheme not in {"http", "https"}:
                dropped_non_http += 1
                continue
            loc = normalize_url(raw_loc)
            if not loc or loc in seen or looks_non_html(loc):
                continue
            seen.add(loc)
            entries.append({"loc": loc, "lastmod": entry.get("lastmod")})

    if dropped_non_http:
        print(
            f"WARNING: dropped {dropped_non_http} sitemap entries with a non-http(s) "
            "scheme (mailto:, ftp:, …).",
            file=sys.stderr,
        )
    return entries


def filter_by_robots(entries: Iterable[dict], robots_cache: RobotsCache) -> List[dict]:
    """Drop entries their own origin's robots.txt disallows.

    Deliberately a separate pass from collect_sitemap_urls so callers can run
    the cheap local filters (--include/--exclude/--since) first: an origin whose
    every entry gets filtered out then never costs a robots.txt request at all.
    """
    kept: List[dict] = []
    dropped_by_origin: dict = {}
    for entry in entries:
        loc = entry.get("loc") or ""
        if robots_cache.can_fetch(loc):
            kept.append(entry)
        else:
            origin = origin_of(loc)
            dropped_by_origin[origin] = dropped_by_origin.get(origin, 0) + 1

    if dropped_by_origin:
        total = sum(dropped_by_origin.values())
        breakdown = ", ".join(
            f"{origin}: {n}" for origin, n in sorted(dropped_by_origin.items())
        )
        print(
            f"WARNING: robots.txt disallowed {total} sitemap URLs ({breakdown}). "
            "Pass --ignore-robots to include them.",
            file=sys.stderr,
        )
    return kept


# ------------------------------------------------------------------- filters


def apply_filters(
    entries: Iterable[dict],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    since: Optional[str] = None,
) -> List[dict]:
    """Filter entries by URL regex and <lastmod> date.

    `include` patterns are OR'd: a URL must match at least one. `exclude`
    patterns are also OR'd: matching any one drops the URL. `since` keeps
    entries whose lastmod is >= the given YYYY-MM-DD; entries with no lastmod
    are kept (we can't prove they're old, and the SHA gate will catch them).
    """
    def compile_all(patterns: Sequence[str], flag: str):
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error as e:
                raise WebDiscoveryError(f"invalid {flag} regex {pattern!r}: {e}") from e
        return compiled

    include_res = compile_all(include, "--include")
    exclude_res = compile_all(exclude, "--exclude")

    out = []
    for entry in entries:
        loc = entry.get("loc") or ""
        if include_res and not any(r.search(loc) for r in include_res):
            continue
        if exclude_res and any(r.search(loc) for r in exclude_res):
            continue
        if since:
            lastmod = entry.get("lastmod")
            if lastmod and lastmod < since:
                continue
        out.append(entry)
    return out


# --------------------------------------------------------------------- crawl

_HREF_RE = re.compile(rb"""<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)


def _extract_links(base_url: str, payload: bytes) -> List[str]:
    """Pull same-origin page links out of an HTML document.

    Regex rather than bs4 on purpose: this module only needs hrefs, and
    staying stdlib-only keeps discovery importable without the parser stack.
    """
    links = []
    for _, raw in _HREF_RE.findall(payload):
        href = raw.decode("utf-8", errors="replace").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if absolute and same_origin(absolute, base_url) and not looks_non_html(absolute):
            links.append(absolute)
    return links


def crawl(
    start_url: str,
    cfg,
    limiter,
    *,
    depth: int,
    max_pages: int,
    robots: Optional[RobotFileParser] = None,
    respect_robots: bool = True,
) -> List[dict]:
    """Breadth-first same-origin crawl, bounded by `depth` and `max_pages`.

    Both bounds are required — there is no default. Callers get them from the
    user, because an unbounded crawl of an unknown site is exactly the mistake
    this signature exists to prevent.
    """
    if depth < 0:
        raise ValueError("crawl depth must be >= 0")
    if max_pages <= 0:
        raise ValueError("crawl max_pages must be > 0")

    user_agent = cfg.web_user_agent()
    delay = crawl_delay_for(robots, user_agent) if (robots and respect_robots) else None

    start = normalize_url(start_url)
    # The crawl is same-origin by construction, so every page is the primary
    # origin — but scope explicitly rather than relying on that invariant.
    primary_origin = origin_of(start)
    frontier = [(start, 0)]
    seen: Set[str] = {start}
    found: List[dict] = []

    while frontier and len(found) < max_pages:
        url, level = frontier.pop(0)

        if respect_robots and robots is not None and not robots.can_fetch(user_agent, url):
            print(f"WARNING: robots.txt disallows {url} — skipping", file=sys.stderr)
            continue

        try:
            resp = _get(
                url,
                cfg,
                limiter,
                accept="text/html,application/xhtml+xml",
                primary_origin=primary_origin,
            )
        except WebDiscoveryError as e:
            print(f"WARNING: could not fetch {url} during crawl — {e}", file=sys.stderr)
            continue

        if delay:
            time.sleep(delay)

        if resp.status_code != 200:
            continue
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            continue

        found.append({"loc": url, "lastmod": None})

        if level >= depth:
            continue
        for link in _extract_links(url, resp.content):
            if link not in seen:
                seen.add(link)
                frontier.append((link, level + 1))

    truncated = bool(frontier) and len(found) >= max_pages
    if truncated:
        print(
            f"WARNING: crawl stopped at the --max-pages cap of {max_pages}; "
            f"{len(frontier)} discovered URLs were not enumerated.",
            file=sys.stderr,
        )
    return found

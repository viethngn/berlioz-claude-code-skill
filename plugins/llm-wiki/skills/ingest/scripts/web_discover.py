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


class WebDiscoveryError(Exception):
    """Discovery could not proceed (network, or an unusable response)."""


def _local_name(tag: str) -> str:
    """Strip an XML namespace: '{ns}urlset' → 'urlset'."""
    return tag.rsplit("}", 1)[-1].lower()


def _get(
    url: str, cfg, limiter, *, accept: str = "*/*", timeout: Optional[int] = None
):
    headers = {**cfg.web_headers(), "Accept": accept}
    try:
        return limiter.request(
            "GET",
            url,
            headers=headers,
            verify=cfg.web_verify_ssl(),
            timeout=timeout or cfg.web_timeout(),
            allow_redirects=True,
        )
    except RateLimitFailure as e:
        raise WebDiscoveryError(str(e)) from e


# ---------------------------------------------------------------- robots.txt


def load_robots(site_url: str, cfg, limiter) -> RobotFileParser:
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
        resp = _get(robots_url, cfg, limiter, accept="text/plain")
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

    def __init__(self, cfg, limiter, seed: Optional[tuple] = None):
        self._cfg = cfg
        self._limiter = limiter
        self._cache: dict = {}
        if seed is not None:
            origin, parser = seed
            self._cache[origin] = parser

    def for_url(self, url: str) -> RobotFileParser:
        origin = origin_of(url)
        if origin not in self._cache:
            self._cache[origin] = load_robots(url, self._cfg, self._limiter)
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


def _decompress(url: str, payload: bytes) -> bytes:
    if payload[:2] == b"\x1f\x8b" or url.lower().endswith(".gz"):
        try:
            return gzip.decompress(payload)
        except (OSError, EOFError) as e:
            raise WebDiscoveryError(f"could not gunzip {url}: {e}")
    return payload


def find_sitemaps(site_url: str, cfg, limiter, robots: Optional[RobotFileParser] = None) -> List[str]:
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
            resp = _get(candidate, cfg, limiter, accept="application/xml,text/xml,text/plain")
        except WebDiscoveryError:
            continue
        if resp.status_code != 200:
            continue
        try:
            payload = _decompress(candidate, resp.content)
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

    resp = _get(url, cfg, limiter, accept="application/xml,text/xml,text/plain")
    if resp.status_code != 200:
        raise WebDiscoveryError(f"HTTP {resp.status_code} fetching sitemap {url}")

    payload = _decompress(url, resp.content)
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
    sitemaps: Sequence[str], cfg, limiter, *, robots_cache: Optional[RobotsCache] = None
) -> List[dict]:
    """Expand every sitemap into a deduplicated, normalized entry list.

    When `robots_cache` is given, each entry is checked against its *own*
    origin's robots.txt (not the origin of the sitemap that listed it) —
    a sitemap can legitimately list URLs on another host.
    """
    entries: List[dict] = []
    seen: Set[str] = set()
    visited: Set[str] = set()
    dropped_non_http = 0
    dropped_by_robots_origin: dict = {}

    for sitemap in sitemaps:
        try:
            raw_entries = parse_sitemap(
                sitemap, cfg, limiter, visited=visited, robots_cache=robots_cache
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
            if robots_cache is not None and not robots_cache.can_fetch(loc):
                host = origin_of(loc)
                dropped_by_robots_origin[host] = dropped_by_robots_origin.get(host, 0) + 1
                continue
            seen.add(loc)
            entries.append({"loc": loc, "lastmod": entry.get("lastmod")})

    if dropped_non_http:
        print(
            f"WARNING: dropped {dropped_non_http} sitemap entries with a non-http(s) "
            "scheme (mailto:, ftp:, …).",
            file=sys.stderr,
        )
    if dropped_by_robots_origin:
        total = sum(dropped_by_robots_origin.values())
        breakdown = ", ".join(
            f"{origin}: {n}" for origin, n in sorted(dropped_by_robots_origin.items())
        )
        print(
            f"WARNING: robots.txt disallowed {total} sitemap URLs ({breakdown}). "
            "Pass --ignore-robots to include them.",
            file=sys.stderr,
        )
    return entries


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
    frontier = [(start, 0)]
    seen: Set[str] = {start}
    found: List[dict] = []

    while frontier and len(found) < max_pages:
        url, level = frontier.pop(0)

        if respect_robots and robots is not None and not robots.can_fetch(user_agent, url):
            print(f"WARNING: robots.txt disallows {url} — skipping", file=sys.stderr)
            continue

        try:
            resp = _get(url, cfg, limiter, accept="text/html,application/xhtml+xml")
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

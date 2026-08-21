"""URL normalization and slug derivation for web ingest.

Shared by `fetch_web.py` (slug stability across re-ingests) and
`web_discover.py` (dedup during sitemap/crawl enumeration). Both must agree
on what "the same URL" means, or a re-ingest would write a second raw file
instead of hitting the content-diff gate.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# Analytics / ad params that never change the content served.
TRACKING_PARAM_RE = re.compile(
    r"^(utm_[a-z_]+|gclid|fbclid|mc_cid|mc_eid|ref|ref_src|igshid|_ga|yclid|msclkid)$",
    re.I,
)

# Trailing path noise that names the same resource as the bare directory.
INDEX_SUFFIX_RE = re.compile(r"/(index|default)\.(html?|php|asp|aspx|jsp)$", re.I)

# Extensions we can render as text. Anything else is skipped by the crawler
# and rejected by the fetcher.
NON_HTML_EXT_RE = re.compile(
    r"\.(pdf|zip|gz|tgz|bz2|xz|7z|rar|docx?|xlsx?|pptx?|csv|tsv|rtf|odt|ods|odp"
    r"|png|jpe?g|gif|webp|bmp|svg|ico|tiff?|avif"
    r"|mp[34g]|m4[av]|mov|avi|mkv|webm|wav|flac|ogg"
    r"|css|js|mjs|json|xml|rss|atom|txt|exe|dmg|pkg|deb|rpm|apk|woff2?|ttf|eot)$",
    re.I,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

SLUG_MAX_LEN = 80
_SLUG_HASH_LEN = 8
# Leave room for "-" + the hash so a truncated slug still fits SLUG_MAX_LEN.
_SLUG_TRUNCATE_TO = SLUG_MAX_LEN - _SLUG_HASH_LEN - 1


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup and slug derivation.

    Lowercases scheme and host, drops the fragment, drops tracking params,
    sorts the remaining query, and strips a trailing slash and index.html.
    Returns the input unchanged if it isn't an http(s) URL.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Drop the default port — :80/:443 name the same origin as no port.
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]

    path = INDEX_SUFFIX_RE.sub("/", parsed.path or "/")
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not TRACKING_PARAM_RE.match(k)
    ]
    query = urlencode(sorted(kept))

    return urlunparse((scheme, netloc, path, "", query, ""))


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def same_origin(a: str, b: str) -> bool:
    return origin_of(a) == origin_of(b) and bool(host_of(a))


def looks_non_html(url: str) -> bool:
    """True when the URL path ends in an extension we can't render as a page."""
    return bool(NON_HTML_EXT_RE.search(urlparse(url).path or ""))


def _slug_part(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")


def web_slug(url: str) -> str:
    """Derive a stable raw/ slug from a URL.

    `web-<host>-<path>`, with `www.` dropped and dots/slashes flattened to
    dashes. An empty path becomes `home`. Non-tracking query params are
    appended so query-keyed pages stay distinct. Slugs longer than
    SLUG_MAX_LEN are truncated and given a short hash of the full normalized
    URL, so uniqueness survives truncation and the result stays deterministic.
    """
    normalized = normalize_url(url)
    parsed = urlparse(normalized)

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path or "/"
    path = re.sub(r"\.(html?|php|asp|aspx|jsp)$", "", path, flags=re.I)

    parts = ["web", _slug_part(host), _slug_part(path) or "home"]
    if parsed.query:
        parts.append(_slug_part(parsed.query))

    slug = "-".join(p for p in parts if p)
    if len(slug) > SLUG_MAX_LEN:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
        slug = f"{slug[:_SLUG_TRUNCATE_TO].rstrip('-')}-{digest}"
    return slug


def join_base(base_url: str, path: str) -> str:
    """Join a site root and an absolute path (e.g. '/robots.txt')."""
    return f"{origin_of(base_url)}{path}"


def parse_lastmod(value: Optional[str]) -> Optional[str]:
    """Return the YYYY-MM-DD prefix of a sitemap <lastmod>, if parseable."""
    if not value:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
    return m.group(1) if m else None

#!/usr/bin/env python3
"""Smoke test for website + sitemap ingest.

Spins up a local HTTP server that mimics a small documentation site and:

1. Verifies URL normalization and slug derivation in `web_url`.
2. Verifies `ingest.detect_bulk_from_url` routes sitemap-shaped URLs to
   `web_sitemap` while leaving a bare site URL as a single page.
3. Fetches one page with `fetch_web.py` — checks the Markdown, the title, the
   `extractor` field, and that decoration images were filtered out of
   `image_hints` while the content image survived.
4. Re-fetches the same page: the server answers `304 Not Modified` to the
   conditional GET, so the fetcher must report `unchanged` without re-parsing.
5. Runs `discover.py --site` against a host whose /robots.txt names a sitemap
   index; the index nests a plain sitemap and a gzipped one. Checks that every
   URL is enumerated, robots-disallowed paths are dropped, and `--include` /
   `--exclude` / `--since` / `--limit` filter as documented.
6. Runs `prefetch.py` over the resulting queue and checks every item lands as
   `done` with a raw file on disk; then re-runs to confirm `unchanged`.
7. Runs `extract_images.py` and checks the byte-size floor drops the tiny
   image while keeping the large one.
8. Verifies the sitemap-less path returns `status="needs_bounds"`, and that a
   bounded `--crawl` respects `--depth` and `--max-pages`.
9. Deletes a raw file on disk and re-fetches: a stale server-side ETag must
   not produce a false `unchanged` for content that no longer exists locally.
10. Fetches a 301-redirected URL twice: the redirect target's slug is used,
    and the *second* fetch of the original URL still hits the 304 cache.
11. Ingests a page with a cross-origin image while `web.extra_headers` is set:
    the same-origin image request carries the header, the cross-origin one
    does not.
12. Enumerates a sitemap that lists a second origin's URLs: entries are
    checked against *that origin's own* robots.txt, and non-http(s) `<loc>`
    entries (mailto:, ftp:) are dropped before they reach the queue.
13. Confirms malformed `--since` / `--site` / `--depth` / `--max-pages` /
    `--include` all fail with a clean `ERROR:` message and never a traceback.

Run:
    python3 plugins/llm-wiki/tests/smoke_web_ingest.py

Exits 0 on success, non-zero on failure. Driver is stdlib only; the scripts
under test need `requests`, `markdownify`, `beautifulsoup4` and (optionally)
`trafilatura`.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "ingest" / "scripts"


# ------------------------------- Fixture site -------------------------------

# A page with: a real content image, a logo (filtered by name), a tiny
# spacer with explicit dimensions (filtered by dimensions), an inline SVG
# reference (filtered by extension), a data: URI (filtered), plus nav/footer
# chrome that must not reach the Markdown.
PAGE_TEMPLATE = """<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body>
  <nav><a href="/">Home</a> <a href="/guide/setup">Setup</a> <a href="/guide/deploy">Deploy</a>
       <a href="/private/secret">Secret</a> <a href="/assets/manual.pdf">PDF</a></nav>
  <header><img src="/img/logo.png" alt="Acme logo"></header>
  <main>
    <h1>{title}</h1>
    <p>{body}</p>
    <h2>Details</h2>
    <ul><li>First point about {title}.</li><li>Second point worth recording.</li></ul>
    <p>Some more prose so the extractor has enough text to consider this a real
    article rather than boilerplate. It needs a few sentences to clear the
    precision threshold, so here they are, plainly written and unremarkable.</p>
    <figure><img src="/img/diagram-{n}.png" alt="Architecture diagram"></figure>
    <img src="/img/spacer.png" width="8" height="8" alt="">
    <img src="/img/chart.svg" alt="vector chart">
    <img src="data:image/png;base64,iVBORw0KGgo=" alt="inline">
    <img src="/img/tiny-{n}.png" alt="small but unnamed">
  </main>
  <footer>Copyright Acme. <a href="/legal">Legal</a></footer>
</body></html>
"""

PAGES = {
    "/": ("Acme Docs Home", "Welcome to the Acme documentation site.", 0),
    "/guide/setup": ("Setup Guide", "How to install and configure Acme.", 1),
    "/guide/deploy": ("Deploy Guide", "How to ship Acme to production.", 2),
    "/reference/api": ("API Reference", "Every endpoint Acme exposes.", 3),
    "/blog/release-notes": ("Release Notes", "What changed in the latest version.", 4),
    # Reachable by link but robots-disallowed, and absent from the sitemap.
    "/private/secret": ("Secret Page", "Should never be ingested.", 5),
    # Two DIFFERENT pages whose slugs collide: web_slug() flattens both `/` and
    # `-` to `-`, so `/collide/x` and `/collide-x` produce the same base slug.
    "/collide/x": ("Collide Nested", "This is the nested page under collide.", 6),
    "/collide-x": ("Collide Flat", "This is the flat hyphenated page.", 7),
}

ROBOTS = """User-agent: *
Disallow: /private/
Sitemap: {base}/sitemap_index.xml
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>{base}/sitemap-blog.xml.gz</loc></sitemap>
</sitemapindex>
"""

SITEMAP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>{base}/guide/setup</loc><lastmod>2026-06-15T10:00:00+00:00</lastmod></url>
  <url><loc>{base}/guide/deploy</loc><lastmod>2026-06-20</lastmod></url>
  <url><loc>{base}/reference/api</loc><lastmod>2026-02-02</lastmod></url>
  <url><loc>{base}/private/secret</loc><lastmod>2026-06-20</lastmod></url>
  <url><loc>{base}/assets/manual.pdf</loc><lastmod>2026-06-20</lastmod></url>
</urlset>
"""

SITEMAP_BLOG = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/blog/release-notes</loc><lastmod>2026-07-01</lastmod></url>
</urlset>
"""

# URLs the sitemap advertises and robots allows.
SITEMAP_ALLOWED = {"/", "/guide/setup", "/guide/deploy", "/reference/api", "/blog/release-notes"}


def _png(size_bytes: int) -> bytes:
    """A valid-enough PNG padded to an approximate byte length."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_body = b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_body) - 4) + ihdr_body
    ihdr += struct.pack(">I", zlib.crc32(ihdr_body) & 0xFFFFFFFF)
    pad_len = max(0, size_bytes - len(header) - len(ihdr) - 12)
    pad_body = b"tEXt" + b"p" * pad_len
    pad = struct.pack(">I", len(pad_body) - 4) + pad_body
    pad += struct.pack(">I", zlib.crc32(pad_body) & 0xFFFFFFFF)
    return header + ihdr + pad + b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"


BIG_IMAGE = _png(30000)
SMALL_IMAGE = _png(500)


# ------------------------------- Mock server --------------------------------


class MockSiteHandler(http.server.BaseHTTPRequestHandler):
    """Serves the fixture site, plus a second 'no sitemap' host behavior.

    Query the port with `?nositemap=1` on /robots.txt to get a robots file
    with no Sitemap: directive; /sitemap*.xml then 404s, which is what drives
    the `needs_bounds` path.

    `last_headers` records the request headers most recently seen for each
    path, used by the credential-isolation check. `cross_origin_sitemap`, when
    set, serves a test-supplied sitemap body at /sitemap-crossorigin.xml.
    """

    base = ""
    no_sitemap = False
    hits: dict = {}
    last_headers: dict = {}
    cross_origin_sitemap: bytes = b""

    def log_message(self, *args):  # noqa: D102 — silence the test server
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        MockSiteHandler.hits[path] = MockSiteHandler.hits.get(path, 0) + 1
        MockSiteHandler.last_headers[path] = dict(self.headers)
        base = MockSiteHandler.base

        if path == "/robots.txt":
            robots = ROBOTS.format(base=base)
            if MockSiteHandler.no_sitemap:
                robots = "User-agent: *\nDisallow: /private/\n"
            return self._send(200, robots.encode(), "text/plain")

        if MockSiteHandler.no_sitemap and path.startswith("/sitemap"):
            return self._send(404, b"not found", "text/plain")

        if path == "/sitemap_index.xml":
            return self._send(200, SITEMAP_INDEX.format(base=base).encode(), "application/xml")
        if path == "/sitemap-pages.xml":
            return self._send(200, SITEMAP_PAGES.format(base=base).encode(), "application/xml")
        if path == "/sitemap-blog.xml.gz":
            payload = gzip.compress(SITEMAP_BLOG.format(base=base).encode())
            return self._send(200, payload, "application/gzip")
        if path == "/sitemap.xml":
            return self._send(404, b"not found", "text/plain")
        if path == "/sitemap-crossorigin.xml":
            if MockSiteHandler.cross_origin_sitemap:
                return self._send(200, MockSiteHandler.cross_origin_sitemap, "application/xml")
            return self._send(404, b"not found", "text/plain")

        if path == "/old/setup":
            # A redirect never evaluates conditional headers itself — only the
            # final destination does. Same-origin, so `requests` forwards
            # If-None-Match/If-Modified-Since through to /guide/setup.
            return self._send(301, b"", "text/plain", {"Location": f"{base}/guide/setup"})

        if path.startswith("/img/"):
            name = path.rsplit("/", 1)[-1]
            payload = SMALL_IMAGE if name.startswith(("tiny", "spacer")) else BIG_IMAGE
            return self._send(200, payload, "image/png")

        if path == "/assets/manual.pdf":
            return self._send(200, b"%PDF-1.4 fake", "application/pdf")

        if path in PAGES:
            title, body, n = PAGES[path]
            html = PAGE_TEMPLATE.format(title=title, body=body, n=n)
            etag = '"%s"' % hashlib.sha256(html.encode()).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, b"", "text/html", {"ETag": etag})
            return self._send(
                200,
                html.encode(),
                "text/html; charset=utf-8",
                {"ETag": etag, "Last-Modified": "Mon, 15 Jun 2026 10:00:00 GMT"},
            )

        return self._send(404, b"not found", "text/plain")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server() -> tuple[http.server.HTTPServer, int]:
    port = _find_free_port()
    MockSiteHandler.base = f"http://127.0.0.1:{port}"
    MockSiteHandler.no_sitemap = False
    MockSiteHandler.hits = {}
    MockSiteHandler.last_headers = {}
    MockSiteHandler.cross_origin_sitemap = b""
    srv = http.server.HTTPServer(("127.0.0.1", port), MockSiteHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class _SecondOriginHandler(http.server.BaseHTTPRequestHandler):
    """A second, independent origin for behavior that must be scoped
    per-origin — credential isolation and per-sitemap-entry robots.txt.

    Deliberately a separate class (not MockSiteHandler): MockSiteHandler.base
    is class-level state read by every dynamically-generated response on the
    primary server, so running a second MockSiteHandler-based server would
    stomp on it.
    """

    robots_body: bytes = b"User-agent: *\n"
    last_headers: dict = {}
    nested_sitemap: bytes = b""

    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        _SecondOriginHandler.last_headers[path] = dict(self.headers)
        if path == "/robots.txt":
            return self._send(200, self.robots_body, "text/plain")
        if path == "/nested-sitemap.xml":
            if self.nested_sitemap:
                return self._send(200, self.nested_sitemap, "application/xml")
            return self._send(404, b"not found", "text/plain")
        if path.startswith("/img/"):
            return self._send(200, BIG_IMAGE, "image/png")
        return self._send(200, b"<html><body>ok</body></html>", "text/html")


def _start_second_origin(
    robots_body: bytes = b"User-agent: *\n", nested_sitemap: bytes = b""
) -> tuple[http.server.HTTPServer, int]:
    port = _find_free_port()
    _SecondOriginHandler.robots_body = robots_body
    _SecondOriginHandler.last_headers = {}
    _SecondOriginHandler.nested_sitemap = nested_sitemap
    srv = http.server.HTTPServer(("127.0.0.1", port), _SecondOriginHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ------------------------------- Helpers ------------------------------------


def _run_script(script: str, args: list) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _write_wikirc(wiki_root: Path, extra_headers: dict | None = None) -> None:
    (wiki_root / ".wikirc.json").write_text(
        json.dumps(
            {
                "wiki_root": ".",
                "raw_dir": "raw",
                "wiki_dir": "wiki",
                "auto_commit": False,
                "atlassian": {"confluence_base_url": "", "jira_base_url": ""},
                "nano_banana": {"base_url": "", "api_key": ""},
                "web": {
                    "user_agent": "llm-wiki-smoke-test/1.0",
                    "rate_limit_rps": 50,
                    "burst": 10,
                    "max_retries": 2,
                    "retry_base_delay_seconds": 0.1,
                    "timeout_seconds": 10,
                    "respect_robots": True,
                    "min_image_bytes": 8192,
                    "extra_headers": extra_headers or {},
                },
            },
            indent=2,
        )
    )
    (wiki_root / "raw").mkdir(exist_ok=True)
    (wiki_root / "wiki").mkdir(exist_ok=True)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _json_tail(stdout: str) -> dict:
    """Parse the last JSON object printed by a script."""
    text = stdout.strip()
    start = text.rfind("\n{")
    candidate = text[start + 1 :] if start >= 0 else text
    return json.loads(candidate)


# ------------------------------- Test steps ---------------------------------


def test_url_helpers() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from web_url import looks_non_html, normalize_url, same_origin, web_slug

    _assert(
        normalize_url("https://WWW.Example.com/A/?utm_source=x&b=2#frag")
        == "https://www.example.com/A?b=2",
        f"normalize_url mismatch: {normalize_url('https://WWW.Example.com/A/?utm_source=x&b=2#frag')}",
    )
    _assert(
        normalize_url("https://example.com:443/a/b/") == "https://example.com/a/b",
        "default port / trailing slash not normalized",
    )
    _assert(
        normalize_url("https://example.com/docs/index.html") == "https://example.com/docs",
        "index.html not normalized away",
    )
    _assert(
        web_slug("https://docs.python.org/3/library/json.html")
        == "web-docs-python-org-3-library-json",
        f"unexpected slug: {web_slug('https://docs.python.org/3/library/json.html')}",
    )
    _assert(web_slug("https://www.example.com/") == "web-example-com-home", "root slug wrong")

    long_url = "https://example.com/" + "/".join(f"segment-number-{i}" for i in range(12))
    slug = web_slug(long_url)
    _assert(len(slug) <= 80, f"long slug not truncated: {len(slug)}")
    _assert(slug == web_slug(long_url), "slug is not deterministic")

    _assert(looks_non_html("https://e.com/a/manual.pdf"), "pdf not flagged non-html")
    _assert(not looks_non_html("https://e.com/a/page"), "extensionless path flagged non-html")
    _assert(same_origin("https://e.com/a", "https://e.com/b"), "same_origin false negative")
    _assert(not same_origin("https://e.com/a", "http://e.com/b"), "scheme ignored by same_origin")
    print("[OK] web_url normalization + slug derivation")


def test_detect_bulk_from_url() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from ingest import detect_bulk_from_url

    for url in (
        "https://example.com/sitemap.xml",
        "https://example.com/sitemap_index.xml",
        "https://example.com/sitemap-1.xml.gz",
        "https://example.com/robots.txt",
    ):
        result = detect_bulk_from_url(url)
        _assert(result == ("web_sitemap", url), f"{url} → {result}, expected web_sitemap")

    for url in (
        "https://example.com/",
        "https://example.com/guide/setup",
        "https://example.com/blog/post.html",
    ):
        _assert(
            detect_bulk_from_url(url) is None,
            f"bare page URL {url} must stay single-item, got {detect_bulk_from_url(url)}",
        )

    _assert(
        detect_bulk_from_url("https://wiki.example.com/wiki/spaces/FOO")
        == ("confluence_space", "FOO"),
        "Confluence space detection regressed",
    )
    print("[OK] detect_bulk_from_url routes sitemaps to bulk, pages to single")


def test_fetch_single_page(wiki_root: Path, base: str) -> str:
    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/guide/setup"])
    _assert(r.returncode == 0, f"fetch_web.py failed: {r.stderr}\n{r.stdout}")
    summary = _json_tail(r.stdout)

    sys.path.insert(0, str(SCRIPTS_DIR))
    from web_url import web_slug

    slug = summary["slug"]
    _assert(slug == web_slug(f"{base}/guide/setup"), f"unexpected slug {slug}")
    _assert(slug.startswith("web-127-0-0-1-") and slug.endswith("-guide-setup"), f"unexpected slug {slug}")
    _assert(summary["title"] == "Setup Guide", f"unexpected title {summary['title']!r}")
    _assert(summary["status"] == "new", f"expected status=new, got {summary['status']}")
    _assert(
        summary["extractor"] in {"trafilatura", "bs4"},
        f"unexpected extractor {summary['extractor']!r}",
    )

    md = (wiki_root / "raw" / f"{slug}.md").read_text(encoding="utf-8")
    _assert("Setup Guide" in md, "title missing from raw markdown")
    _assert("How to install and configure Acme" in md, "body text missing from raw markdown")
    _assert("Copyright Acme" not in md, "footer chrome leaked into raw markdown")
    _assert("Secret" not in md, "nav chrome leaked into raw markdown")

    hints = [h["url"] for h in summary["image_hints"]]
    _assert(any("diagram-1.png" in h for h in hints), f"content image missing from hints: {hints}")
    _assert(not any("logo" in h for h in hints), f"logo not filtered: {hints}")
    _assert(not any("spacer" in h for h in hints), f"spacer not filtered: {hints}")
    _assert(not any(h.endswith(".svg") for h in hints), f"svg not filtered: {hints}")
    _assert(not any(h.startswith("data:") for h in hints), f"data URI not filtered: {hints}")

    meta = json.loads((wiki_root / "raw" / f"{slug}.source.json").read_text(encoding="utf-8"))
    _assert(meta["type"] == "web", "source.json type is not 'web'")
    _assert(meta["url"] == f"{base}/guide/setup", f"source.json url wrong: {meta['url']}")
    _assert("extractor" in meta, "source.json is missing the extractor field")

    state = json.loads((wiki_root / ".wiki-state" / "last-fetched.json").read_text())
    _assert(f"web:{slug}" in state, "validators were not recorded under the web: key")
    _assert(state[f"web:{slug}"].get("etag"), "ETag was not recorded")
    print(f"[OK] single page fetch ({summary['extractor']}) + image filtering")
    return slug


def test_conditional_get(wiki_root: Path, base: str, slug: str) -> None:
    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/guide/setup"])
    _assert(r.returncode == 0, f"re-fetch failed: {r.stderr}")
    summary = _json_tail(r.stdout)
    _assert(
        summary["status"] == "unchanged",
        f"expected unchanged on re-fetch, got {summary['status']}",
    )
    _assert("304" in (summary.get("note") or ""), "re-fetch did not go through the 304 path")

    # --force must re-fetch and re-render identical content (so status is
    # 'unchanged' from the SHA gate, not from a 304).
    r = _run_script(
        "fetch_web.py",
        ["--wiki-root", str(wiki_root), "--url", f"{base}/guide/setup", "--force"],
    )
    _assert(r.returncode == 0, f"forced re-fetch failed: {r.stderr}")
    forced = _json_tail(r.stdout)
    _assert(
        forced["status"] == "unchanged" and "304" not in (forced.get("note") or ""),
        f"--force should bypass the conditional GET, got {forced}",
    )
    print("[OK] conditional GET (304) + --force bypass")


def test_missing_file_recovery(wiki_root: Path, base: str, slug: str) -> None:
    md_path = wiki_root / "raw" / f"{slug}.md"
    _assert(md_path.exists(), "precondition: raw md should exist before deleting it")
    md_path.unlink()

    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/guide/setup"])
    _assert(r.returncode == 0, f"recovery fetch failed: {r.stderr}\n{r.stdout}")
    summary = _json_tail(r.stdout)
    _assert(
        summary["status"] != "unchanged",
        f"a deleted raw file must not be reported unchanged (stale ETag trusted): {summary}",
    )
    _assert(md_path.exists(), "deleted raw file was not restored by the recovery fetch")
    print("[OK] deleted raw file triggers a real re-fetch instead of a false 304")


def test_redirect_caching(wiki_root: Path, base: str) -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from web_url import web_slug

    old_url = f"{base}/old/setup"
    target_slug = web_slug(f"{base}/guide/setup")

    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", old_url])
    _assert(r.returncode == 0, f"redirected fetch failed: {r.stderr}\n{r.stdout}")
    summary = _json_tail(r.stdout)
    _assert(
        summary["slug"] == target_slug,
        f"redirect did not resolve to the target's slug: {summary['slug']!r} != {target_slug!r}",
    )

    # Second ingest of the *original* (pre-redirect) URL: validators were
    # stored under its own request-slug, pointing at the target's content —
    # this must still hit the 304 cache, not silently miss and re-fetch.
    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", old_url])
    _assert(r.returncode == 0, f"second redirected fetch failed: {r.stderr}")
    summary2 = _json_tail(r.stdout)
    _assert(
        summary2["status"] == "unchanged" and "304" in (summary2.get("note") or ""),
        f"redirected URL did not hit the conditional-GET cache on re-fetch: {summary2}",
    )
    print("[OK] redirect caching — keyed by the requested URL, resolved to the target's slug")


def test_credential_isolation(base: str) -> None:
    cdn_srv, cdn_port = _start_second_origin()
    cdn_base = f"http://127.0.0.1:{cdn_port}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wiki_root = Path(tmp) / "wiki-root"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, extra_headers={"Cookie": "secret=abc123"})

            r = _run_script(
                "fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/guide/deploy"]
            )
            _assert(r.returncode == 0, f"fetch failed: {r.stderr}\n{r.stdout}")
            summary = _json_tail(r.stdout)
            slug = summary["slug"]

            # Splice in a cross-origin image hint alongside the real same-origin
            # one already extracted from /guide/deploy (diagram-2.png).
            source_json = wiki_root / "raw" / f"{slug}.source.json"
            meta = json.loads(source_json.read_text(encoding="utf-8"))
            meta["image_hints"].append(
                {"url": f"{cdn_base}/img/cdn-only.png", "filename": "cdn-only.png", "kind": "web", "alt": ""}
            )
            source_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            r = _run_script(
                "extract_images.py",
                ["--wiki-root", str(wiki_root), "--source-json", str(source_json), "--slug", slug],
            )
            _assert(r.returncode == 0, f"extract_images.py failed: {r.stderr}\n{r.stdout}")

            same_origin_headers = MockSiteHandler.last_headers.get("/img/diagram-2.png") or {}
            cross_origin_headers = _SecondOriginHandler.last_headers.get("/img/cdn-only.png") or {}

            _assert(
                same_origin_headers.get("Cookie") == "secret=abc123",
                f"same-origin image request should carry the configured Cookie: {same_origin_headers}",
            )
            _assert(
                "Cookie" not in cross_origin_headers,
                f"cross-origin image request must NOT receive the site's Cookie: {cross_origin_headers}",
            )
            _assert(
                cross_origin_headers.get("User-Agent") == "llm-wiki-smoke-test/1.0",
                f"cross-origin image request should still carry the User-Agent: {cross_origin_headers}",
            )

            # Scheme must count too: an https image on the same *host* as an
            # http page is a different origin, so it must not get the Cookie.
            sys.path.insert(0, str(SCRIPTS_DIR))
            import extract_images
            from config import load_config

            cfg = load_config(wiki_root)
            page = "http://example.com/page"
            same_scheme = extract_images._headers_for(
                "http://example.com/a.png", cfg, "web", page
            )
            other_scheme = extract_images._headers_for(
                "https://example.com/a.png", cfg, "web", page
            )
            _assert(
                same_scheme.get("Cookie") == "secret=abc123",
                f"same-origin image should get the Cookie: {same_scheme}",
            )
            _assert(
                "Cookie" not in other_scheme,
                "an https image must not receive a Cookie configured for an http "
                f"page on the same host: {other_scheme}",
            )
    finally:
        cdn_srv.shutdown()
    print("[OK] web.extra_headers scoped to same-origin images (scheme+host, not host)")


def test_cross_origin_robots_and_non_http(wiki_root: Path, base: str) -> None:
    host_b_srv, host_b_port = _start_second_origin(
        robots_body=b"User-agent: *\nDisallow: /blocked\n"
    )
    host_b_base = f"http://127.0.0.1:{host_b_port}"
    try:
        cross_origin_sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{host_b_base}/allowed</loc></url>
  <url><loc>{host_b_base}/blocked</loc></url>
  <url><loc>mailto:test@example.com</loc></url>
  <url><loc>ftp://example.com/file.txt</loc></url>
</urlset>
"""
        MockSiteHandler.cross_origin_sitemap = cross_origin_sitemap.encode()

        r = _run_script(
            "discover.py",
            [
                "--wiki-root", str(wiki_root),
                "--sitemap", f"{base}/sitemap-crossorigin.xml",
                "--replace",
            ],
        )
        _assert(r.returncode == 0, f"cross-origin sitemap discover failed: {r.stderr}\n{r.stdout}")
        result = _json_tail(r.stdout)
        _assert(
            result["counts"]["total"] == 1,
            f"expected only the robots-allowed host-B URL, got {result['counts']}",
        )
        _assert(
            "non-http" in r.stderr,
            f"no warning about dropped mailto:/ftp: entries: {r.stderr}",
        )
        _assert(
            "robots.txt disallowed" in r.stderr and host_b_base in r.stderr,
            f"no per-origin robots warning for host B: {r.stderr}",
        )

        sys.path.insert(0, str(SCRIPTS_DIR))
        from bulk_queue import load_queue

        queue = load_queue(wiki_root, result["job_id"])
        refs = {i.ref for i in queue.items}
        _assert(
            refs == {f"{host_b_base}/allowed"},
            f"unexpected refs after cross-origin robots + scheme filtering: {refs}",
        )
    finally:
        MockSiteHandler.cross_origin_sitemap = b""
        host_b_srv.shutdown()
    print("[OK] per-origin robots.txt for sitemap entries + non-http(s) <loc> rejection")


def test_validation_errors(wiki_root: Path, base: str) -> None:
    cases = [
        (["--site", base, "--since", "2026-6-1", "--replace"], "--since"),
        (["--site", "example.com", "--replace"], "--site"),
        (["--crawl", base, "--depth", "1", "--max-pages", "0", "--replace"], "--max-pages"),
        (["--crawl", base, "--depth", "-1", "--max-pages", "5", "--replace"], "--depth"),
        (["--site", base, "--include", "[unclosed", "--replace"], "--include"),
    ]
    for extra_args, needle in cases:
        r = _run_script("discover.py", ["--wiki-root", str(wiki_root), *extra_args])
        _assert(r.returncode != 0, f"expected failure for {extra_args}, got exit 0: {r.stdout}")
        _assert(
            "Traceback" not in r.stderr,
            f"a raw traceback leaked for {extra_args}:\n{r.stderr}",
        )
        _assert("ERROR:" in r.stderr, f"no friendly ERROR: prefix for {extra_args}: {r.stderr}")
        _assert(needle in r.stderr, f"error for {extra_args} doesn't mention {needle}: {r.stderr}")
    print("[OK] friendly validation for --since / --site / --depth / --max-pages / --include")


def test_no_userinfo_in_urls(wiki_root: Path) -> None:
    """Embedded credentials must never reach a slug or committed metadata."""
    before = set(p.name for p in (wiki_root / "raw").iterdir())

    r = _run_script(
        "fetch_web.py",
        ["--wiki-root", str(wiki_root), "--url", "https://alice:secret@example.com/docs"],
    )
    _assert(r.returncode != 0, "a URL with embedded credentials must be rejected")
    _assert("credentials" in r.stderr, f"unhelpful error: {r.stderr}")
    _assert("Traceback" not in r.stderr, f"traceback leaked: {r.stderr}")

    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", "https://alice:secret@example.com"],
    )
    _assert(r.returncode != 0, "--site with embedded credentials must be rejected")
    _assert("credentials" in r.stderr, f"unhelpful error: {r.stderr}")

    after = set(p.name for p in (wiki_root / "raw").iterdir())
    _assert(before == after, f"rejected URL still wrote files: {after - before}")
    # And nothing with the secret in its name anywhere under the wiki.
    leaked = [p for p in wiki_root.rglob("*secret*")]
    _assert(not leaked, f"credential leaked into a filename: {leaked}")
    print("[OK] URLs with embedded credentials rejected, nothing written")


def test_hostless_url(wiki_root: Path) -> None:
    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", "https:///path"])
    _assert(r.returncode != 0, "a hostless URL should fail")
    _assert("Traceback" not in r.stderr, f"hostless URL produced a traceback: {r.stderr}")
    _assert("ERROR:" in r.stderr, f"no friendly error: {r.stderr}")
    print("[OK] hostless http(s) URL fails cleanly instead of tracebacking")


def test_slug_collision(wiki_root: Path, base: str) -> None:
    """Two different pages that flatten to one slug must not overwrite."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from web_url import web_slug

    nested_url = f"{base}/collide/x"
    flat_url = f"{base}/collide-x"
    _assert(
        web_slug(nested_url) == web_slug(flat_url),
        "precondition: these URLs are supposed to collide under web_slug()",
    )

    r1 = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", nested_url])
    _assert(r1.returncode == 0, f"first collide fetch failed: {r1.stderr}")
    s1 = _json_tail(r1.stdout)

    r2 = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", flat_url])
    _assert(r2.returncode == 0, f"second collide fetch failed: {r2.stderr}\n{r2.stdout}")
    s2 = _json_tail(r2.stdout)

    _assert(
        s1["slug"] != s2["slug"],
        f"colliding pages got the same slug ({s1['slug']}) — one overwrote the other",
    )
    md1 = (wiki_root / "raw" / f"{s1['slug']}.md").read_text(encoding="utf-8")
    md2 = (wiki_root / "raw" / f"{s2['slug']}.md").read_text(encoding="utf-8")
    _assert("nested page under collide" in md1, f"page 1 content wrong:\n{md1[:300]}")
    _assert("flat hyphenated page" in md2, f"page 2 content wrong:\n{md2[:300]}")

    # Re-ingesting either must be stable: same slug, and the diff gate holds.
    r3 = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", flat_url])
    _assert(r3.returncode == 0, f"re-fetch of collided page failed: {r3.stderr}")
    s3 = _json_tail(r3.stdout)
    _assert(
        s3["slug"] == s2["slug"],
        f"collided slug is not stable across re-ingest: {s2['slug']} → {s3['slug']}",
    )
    _assert(
        s3["status"] == "unchanged",
        f"re-fetch of collided page should be unchanged, got {s3['status']}",
    )
    r4 = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", nested_url])
    s4 = _json_tail(r4.stdout)
    _assert(
        s4["slug"] == s1["slug"] and s4["status"] == "unchanged",
        f"original page destabilized by the collision: {s4}",
    )
    print(f"[OK] slug collision resolved ({s1['slug']} / {s2['slug']}), both stable")


def test_cookie_survives_same_origin_redirect(base: str) -> None:
    """requests strips Cookie on every redirect hop; we must re-attach it."""
    with tempfile.TemporaryDirectory() as tmp:
        wiki_root = Path(tmp) / "wiki-root"
        wiki_root.mkdir()
        _write_wikirc(wiki_root, extra_headers={"Cookie": "session=redirect-me"})

        MockSiteHandler.last_headers.pop("/guide/deploy", None)
        r = _run_script(
            "fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/old/setup"]
        )
        _assert(r.returncode == 0, f"redirected fetch failed: {r.stderr}\n{r.stdout}")

        final_headers = MockSiteHandler.last_headers.get("/guide/setup") or {}
        _assert(
            final_headers.get("Cookie") == "session=redirect-me",
            "Cookie was lost across a same-origin redirect — requests strips it on "
            f"every hop and it must be re-attached. Final-hop headers: {final_headers}",
        )
    print("[OK] Cookie survives a same-origin redirect (requests would have dropped it)")


def test_discovery_credentials_not_leaked_cross_origin(wiki_root: Path, base: str) -> None:
    """A foreign sitemap/robots host must not receive our configured secrets."""
    nested = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>PLACEHOLDER/page-a</loc></url>
</urlset>
"""
    host_b_srv, host_b_port = _start_second_origin()
    host_b_base = f"http://127.0.0.1:{host_b_port}"
    _SecondOriginHandler.nested_sitemap = nested.replace("PLACEHOLDER", host_b_base).encode()

    # An index on host A pointing at a nested sitemap FILE on host B.
    index = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{host_b_base}/nested-sitemap.xml</loc></sitemap>
</sitemapindex>
"""
    MockSiteHandler.cross_origin_sitemap = index.encode()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            wr = Path(tmp) / "wiki-root"
            wr.mkdir()
            _write_wikirc(wr, extra_headers={"Cookie": "session=do-not-leak"})

            r = _run_script(
                "discover.py",
                [
                    "--wiki-root", str(wr),
                    "--sitemap", f"{base}/sitemap-crossorigin.xml",
                    "--replace",
                ],
            )
            _assert(r.returncode == 0, f"cross-origin discover failed: {r.stderr}\n{r.stdout}")

            b_headers = _SecondOriginHandler.last_headers
            for path in ("/robots.txt", "/nested-sitemap.xml"):
                got = b_headers.get(path)
                _assert(got is not None, f"host B never received a request for {path}")
                _assert(
                    "Cookie" not in got,
                    f"configured Cookie leaked to a foreign origin on {path}: {got}",
                )
                _assert(
                    got.get("User-Agent") == "llm-wiki-smoke-test/1.0",
                    f"User-Agent should still be sent to {path}: {got}",
                )

            # The entry-point host DOES legitimately get the credentials.
            a_headers = MockSiteHandler.last_headers.get("/sitemap-crossorigin.xml") or {}
            _assert(
                a_headers.get("Cookie") == "session=do-not-leak",
                f"entry-point origin should receive the configured Cookie: {a_headers}",
            )
    finally:
        MockSiteHandler.cross_origin_sitemap = b""
        _SecondOriginHandler.nested_sitemap = b""
        host_b_srv.shutdown()
    print("[OK] discovery credentials scoped to the entry-point origin only")


def test_queue_options_identity(wiki_root: Path, base: str) -> None:
    """Changing a filter must not silently return the old, differently-scoped queue."""
    r = _run_script(
        "discover.py", ["--wiki-root", str(wiki_root), "--site", base, "--replace"]
    )
    _assert(r.returncode == 0, f"baseline discover failed: {r.stderr}")
    baseline = _json_tail(r.stdout)
    _assert(baseline["counts"]["total"] == len(SITEMAP_ALLOWED), f"unexpected: {baseline}")

    # Same options, no --replace → reuse is correct and expected.
    r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--site", base])
    _assert(r.returncode == 0, f"same-options rerun should succeed: {r.stderr}")
    _assert(_json_tail(r.stdout).get("reused") is True, "same options should reuse")

    # Different --include, no --replace → must refuse, not silently reuse.
    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--include", "/guide/"],
    )
    _assert(r.returncode != 0, "changed --include must not silently reuse the old queue")
    payload = _json_tail(r.stdout)
    _assert(
        payload.get("status") == "options_changed",
        f"expected options_changed, got {payload}",
    )
    _assert("include" in payload.get("changed", ""), f"diff should name include: {payload}")
    _assert("--replace" in payload.get("note", ""), "note should tell the user what to do")

    # With --replace it applies.
    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--include", "/guide/", "--replace"],
    )
    _assert(r.returncode == 0, f"--replace with new options failed: {r.stderr}")
    _assert(_json_tail(r.stdout)["counts"]["total"] == 2, "new --include should apply")

    # ingest.py surfaces it too rather than crashing opaquely.
    r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--site", base])
    _assert(r.returncode != 0, "ingest.py should propagate options_changed as a failure")
    _assert(
        '"options_changed"' in r.stdout,
        f"ingest.py did not surface the options_changed payload: {r.stdout}\n{r.stderr}",
    )
    print("[OK] queue reuse rejects changed discovery options instead of ignoring them")


def test_sitemap_size_bounds() -> None:
    """The gzip and raw-transfer caps must fire, in-process (no 200 MB anywhere)."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    import web_discover

    # --- gzip bomb: highly compressible payload, cap lowered for the test ---
    bomb = gzip.compress(b"A" * (2 * 1024 * 1024))  # 2 MB → a few KB
    original = web_discover.MAX_SITEMAP_DECOMPRESSED_BYTES
    try:
        web_discover.MAX_SITEMAP_DECOMPRESSED_BYTES = 64 * 1024  # 64 KB
        try:
            web_discover._decompress("http://x/sitemap.xml.gz", bomb)
            raise AssertionError("decompression cap did not fire on a compression bomb")
        except web_discover.WebDiscoveryError as e:
            _assert(
                "compression bomb" in str(e) or "decompressed" in str(e),
                f"unexpected error text: {e}",
            )
        # Under the cap still works.
        web_discover.MAX_SITEMAP_DECOMPRESSED_BYTES = original
        small = gzip.compress(b"<urlset></urlset>")
        _assert(
            web_discover._decompress("http://x/s.xml.gz", small) == b"<urlset></urlset>",
            "a normal gzipped sitemap must still decompress",
        )
    finally:
        web_discover.MAX_SITEMAP_DECOMPRESSED_BYTES = original

    # --- raw transfer cap: _read_capped aborts past the limit ---
    class _FakeResp:
        def __init__(self, chunks):
            self._chunks = chunks

        def iter_content(self, chunk_size=65536):  # noqa: ARG002
            return iter(self._chunks)

    ok = web_discover._read_capped(_FakeResp([b"a" * 10, b"b" * 10]), 100, "test")
    _assert(ok == b"a" * 10 + b"b" * 10, "under-cap read should return the full body")
    try:
        web_discover._read_capped(_FakeResp([b"x" * 60, b"y" * 60]), 100, "test")
        raise AssertionError("raw byte cap did not fire")
    except web_discover.WebDiscoveryError as e:
        _assert("limit" in str(e), f"unexpected error text: {e}")
    print("[OK] sitemap raw-transfer and gzip-decompression caps both fire")


def test_discover_site(wiki_root: Path, base: str) -> str:
    r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--site", base])
    _assert(r.returncode == 0, f"discover --site failed: {r.stderr}\n{r.stdout}")
    result = _json_tail(r.stdout)
    job_id = result.get("job_id")
    _assert(bool(job_id), f"no job_id in discover output: {result}")
    _assert(
        result["counts"]["total"] == len(SITEMAP_ALLOWED),
        f"expected {len(SITEMAP_ALLOWED)} items, got {result['counts']}",
    )

    sys.path.insert(0, str(SCRIPTS_DIR))
    from bulk_queue import load_queue

    queue = load_queue(wiki_root, job_id)
    refs = {i.ref for i in queue.items}
    expected = {f"{base}{p}".rstrip("/") if p != "/" else f"{base}/" for p in SITEMAP_ALLOWED}
    _assert(refs == expected, f"queue refs mismatch:\n  got      {sorted(refs)}\n  expected {sorted(expected)}")
    _assert(
        not any("/private/" in ref for ref in refs),
        "robots-disallowed URL was not dropped from the sitemap",
    )
    _assert(
        not any(ref.endswith(".pdf") for ref in refs),
        "non-HTML sitemap entry was not dropped",
    )
    _assert(queue.kind == "web_sitemap", f"unexpected queue kind {queue.kind}")
    print(f"[OK] --site → robots Sitemap: → nested index (plain + .gz) = {len(refs)} URLs")
    return job_id


def test_discover_filters(wiki_root: Path, base: str) -> None:
    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--include", "/guide/", "--replace"],
    )
    _assert(r.returncode == 0, f"discover --include failed: {r.stderr}")
    result = _json_tail(r.stdout)
    _assert(result["counts"]["total"] == 2, f"--include should keep 2 guide pages, got {result['counts']}")

    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--exclude", "/blog/", "--replace"],
    )
    _assert(r.returncode == 0, f"discover --exclude failed: {r.stderr}")
    result = _json_tail(r.stdout)
    _assert(
        result["counts"]["total"] == len(SITEMAP_ALLOWED) - 1,
        f"--exclude should drop the blog page, got {result['counts']}",
    )

    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--since", "2026-06-01", "--replace"],
    )
    _assert(r.returncode == 0, f"discover --since failed: {r.stderr}")
    result = _json_tail(r.stdout)
    # setup (2026-06-15), deploy (2026-06-20), release-notes (2026-07-01)
    _assert(result["counts"]["total"] == 3, f"--since should keep 3 entries, got {result['counts']}")

    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--site", base, "--limit", "2", "--replace"],
    )
    _assert(r.returncode == 0, f"discover --limit failed: {r.stderr}")
    result = _json_tail(r.stdout)
    _assert(result["counts"]["total"] == 2, f"--limit 2 not honored: {result['counts']}")
    print("[OK] --include / --exclude / --since / --limit filters")


def test_prefetch(wiki_root: Path, base: str, job_id: str) -> None:
    r = _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", job_id])
    _assert(r.returncode == 0, f"prefetch failed: {r.stderr}\n{r.stdout}")

    sys.path.insert(0, str(SCRIPTS_DIR))
    from bulk_queue import load_queue

    queue = load_queue(wiki_root, job_id)
    counts = queue.counts()
    _assert(counts["failed"] == 0, f"prefetch had failures: {[i.last_error for i in queue.items]}")
    _assert(
        counts["raw_done"] == counts["total"],
        f"not every item completed: {counts}",
    )
    for item in queue.items:
        _assert(bool(item.slug), f"item {item.ref} has no slug")
        _assert(item.slug.startswith("web-"), f"unexpected slug {item.slug}")
        _assert(
            (wiki_root / "raw" / f"{item.slug}.md").exists(),
            f"missing raw file for {item.ref}",
        )
        _assert(bool(item.title), f"title not backfilled for {item.ref}")

    # Second pass: every page answers 304 / hashes identically → unchanged.
    r = _run_script(
        "prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", job_id, "--retry-failed"]
    )
    _assert(r.returncode == 0, f"prefetch re-run failed: {r.stderr}")
    print(f"[OK] prefetch fetched {counts['total']} pages, all resumable-clean")


def test_extract_images(wiki_root: Path, base: str, slug: str) -> None:
    source_json = wiki_root / "raw" / f"{slug}.source.json"
    r = _run_script(
        "extract_images.py",
        ["--wiki-root", str(wiki_root), "--source-json", str(source_json), "--slug", slug],
    )
    _assert(r.returncode == 0, f"extract_images.py failed: {r.stderr}\n{r.stdout}")
    result = _json_tail(r.stdout)
    counts = result["counts"]
    _assert(counts["downloaded_new"] >= 1, f"no content image downloaded: {counts}")
    _assert(counts["skipped_small"] >= 1, f"byte-size floor did not fire: {counts}")
    _assert(counts["failed"] == 0, f"image download failures: {result['results']}")

    manifest_path = wiki_root / "raw" / "images" / slug / ".manifest.json"
    _assert(manifest_path.exists(), "image manifest was not written")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("images") or {}
    _assert(len(entries) == counts["downloaded_new"], f"manifest/count mismatch: {entries}")
    print(f"[OK] image download: {counts['downloaded_new']} kept, {counts['skipped_small']} under the size floor")


def test_needs_bounds_and_crawl(wiki_root: Path, base: str) -> None:
    MockSiteHandler.no_sitemap = True
    try:
        # --replace so the queue built earlier in this run doesn't short-circuit
        # discovery via the reuse path.
        r = _run_script(
            "discover.py", ["--wiki-root", str(wiki_root), "--site", base, "--replace"]
        )
        _assert(r.returncode == 0, f"discover --site (no sitemap) should exit 0: {r.stderr}")
        result = _json_tail(r.stdout)
        _assert(
            result.get("status") == "needs_bounds",
            f"expected needs_bounds, got {result}",
        )
        _assert("suggested" in result, "needs_bounds payload has no suggested bounds")

        # ingest.py must surface it rather than crawling on its own.
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--site", base, "--replace"])
        _assert(r.returncode == 0, f"ingest --site (no sitemap) should exit 0: {r.stderr}")
        _assert(
            '"needs_bounds"' in r.stdout,
            f"ingest.py did not surface needs_bounds: {r.stdout}",
        )

        # --crawl without bounds must be rejected outright.
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--crawl", base])
        _assert(r.returncode != 0, "--crawl without --depth/--max-pages should fail")
        _assert(
            "--depth" in (r.stderr + r.stdout),
            f"unhelpful error for unbounded crawl: {r.stderr}",
        )

        # Bounded crawl: caps at max_pages and never enters /private/.
        r = _run_script(
            "discover.py",
            [
                "--wiki-root", str(wiki_root),
                "--crawl", base,
                "--depth", "1",
                "--max-pages", "3",
                "--replace",
            ],
        )
        _assert(r.returncode == 0, f"bounded crawl failed: {r.stderr}\n{r.stdout}")
        result = _json_tail(r.stdout)
        _assert(
            result["counts"]["total"] == 3,
            f"crawl should stop at --max-pages 3, got {result['counts']}",
        )

        sys.path.insert(0, str(SCRIPTS_DIR))
        from bulk_queue import load_queue

        queue = load_queue(wiki_root, result["job_id"])
        _assert(queue.kind == "web_crawl", f"unexpected kind {queue.kind}")
        _assert(
            not any("/private/" in i.ref for i in queue.items),
            f"crawl entered a robots-disallowed path: {[i.ref for i in queue.items]}",
        )
        print("[OK] needs_bounds handoff + bounded crawl (depth/max-pages/robots)")
    finally:
        MockSiteHandler.no_sitemap = False


def test_non_html_and_404(wiki_root: Path, base: str) -> None:
    r = _run_script(
        "fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/assets/manual.pdf"]
    )
    _assert(r.returncode != 0, "a PDF URL should not be ingested as a web page")
    _assert(
        "Content-Type" in r.stderr and "/ingest" in r.stderr,
        f"non-HTML error should point at local-file ingest: {r.stderr}",
    )

    r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", f"{base}/nope"])
    _assert(r.returncode != 0, "404 should fail the fetch")
    _assert("404" in r.stderr, f"404 not reported: {r.stderr}")
    print("[OK] non-HTML content type and 404 handling")


def main() -> int:
    srv, port = _start_server()
    base = f"http://127.0.0.1:{port}"
    try:
        test_url_helpers()
        test_detect_bulk_from_url()

        with tempfile.TemporaryDirectory() as tmp:
            wiki_root = Path(tmp) / "wiki-root"
            wiki_root.mkdir()
            _write_wikirc(wiki_root)

            slug = test_fetch_single_page(wiki_root, base)
            test_conditional_get(wiki_root, base, slug)
            test_missing_file_recovery(wiki_root, base, slug)
            test_redirect_caching(wiki_root, base)
            test_extract_images(wiki_root, base, slug)
            test_non_html_and_404(wiki_root, base)
            test_hostless_url(wiki_root)
            test_no_userinfo_in_urls(wiki_root)
            test_slug_collision(wiki_root, base)
            test_credential_isolation(base)
            test_cookie_survives_same_origin_redirect(base)
            test_sitemap_size_bounds()

            job_id = test_discover_site(wiki_root, base)
            test_prefetch(wiki_root, base, job_id)
            test_discover_filters(wiki_root, base)
            test_cross_origin_robots_and_non_http(wiki_root, base)
            test_discovery_credentials_not_leaked_cross_origin(wiki_root, base)
            test_queue_options_identity(wiki_root, base)
            test_validation_errors(wiki_root, base)
            test_needs_bounds_and_crawl(wiki_root, base)
    finally:
        srv.shutdown()

    print("\nAll web ingest smoke tests passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)

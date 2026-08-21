# Website Ingest Reference

Load when debugging a web page fetch, a sitemap enumeration, or a crawl.

## Scripts

| Script | Role |
|--------|------|
| `scripts/fetch_web.py` | Fetch and render **one** page → `raw/<slug>.md` |
| `scripts/web_discover.py` | Sitemap / robots.txt discovery and the bounded crawler |
| `scripts/web_url.py` | URL normalization and slug derivation (stdlib only) |

`discover.py` and `prefetch.py` handle web jobs through the same queue as
Confluence and Jira; the only web-specific code is in the three files above.

## Content extraction

Two extractors, in order:

1. **trafilatura** (`output_format="markdown"`, `include_tables=True`,
   `include_links=True`, `favor_precision=True`). This is the normal path. It
   discards navigation, sidebars, cookie banners, and related-article blocks
   that a tag-based heuristic keeps.
2. **BeautifulSoup + markdownify** fallback, used when trafilatura is not
   installed, raises, or returns nothing. It decomposes
   `script/style/noscript/template/nav/header/footer/aside/form/svg/iframe`,
   then picks the first non-empty of `main`, `article`, `[role=main]`,
   `#content`, `.content`, `#main`, falling back to `<body>`. This mirrors
   `fetch_local.parse_html`, so a saved page and a fetched page render alike.

Which one ran is recorded as `extractor` in `raw/<slug>.source.json`. That
matters: if the extractor changes between runs, the rendered Markdown changes,
the SHA gate fires, and the diff shows *why*.

Known trafilatura quirk: relative link targets are sometimes resolved against
the site root rather than the page's directory, so a link may point at
`https://host/other.html` instead of `https://host/dir/other.html`. Prose,
headings, tables, and code fences are unaffected.

**JavaScript-rendered pages.** No browser is involved — the fetcher issues one
HTTP GET. A client-rendered SPA returns an empty shell, extraction yields
nothing, and the script exits with a message saying so. Workaround: save the
rendered page from a browser (Save As → Web Page, Complete) and
`/ingest ./that-file.html`, which goes through `fetch_local.py`.

**Non-HTML URLs** are rejected with a pointer to local-file ingest, which
already handles PDF, DOCX, XLSX, PPTX, CSV, and images natively.

## Slugs

`web_url.web_slug(url)` produces `web-<host>-<path>`:

| URL | Slug |
|-----|------|
| `https://docs.python.org/3/library/json.html` | `web-docs-python-org-3-library-json` |
| `https://www.example.com/` | `web-example-com-home` |
| `https://example.com/p?id=3&utm_source=x` | `web-example-com-p-id-3` |
| a path over 80 chars | truncated to 71 + `-<sha256(url)[:8]>` |

Derived from the URL, not the `<title>`, on purpose: a retitled page must
update its existing raw file rather than silently creating a second one, and
titles like "Overview" collide constantly across one site. `www.` is dropped,
the host's dots and the path's slashes become dashes, a trailing
`index.html` / `.html` / `.php` is stripped, and tracking params
(`utm_*`, `gclid`, `fbclid`, …) are removed before slugging.

## The two diff gates

Web ingest has one more gate than the other sources, in front of the usual two:

- **Gate 0 — conditional GET.** The previous response's `ETag` and
  `Last-Modified` are replayed as `If-None-Match` / `If-Modified-Since`. A
  `304` returns `status="unchanged"` immediately: no parsing, no image work, no
  commit. `--force` skips this.
- **Gate 1 — content SHA.** Unchanged rendered Markdown → `status="unchanged"`,
  exactly as for Confluence and Jira.

The validators live in `.wiki-state/last-fetched.json` under the key
`web:<slug>` — **not** in `source.json`. Two reasons: some servers rotate weak
ETags on every request, which would churn `source.json` and defeat gate 1; and
`raw_store.write_fetch_history()` overwrites `data[<slug>]` wholesale on every
ingest, so a prefixed key is required to survive it. `fetch_slack.py` uses a
prefixed key for the same reason.

**Keyed by the requested URL, not the content slug.** The slug is only known
after the response comes back — a redirect can change it. So the cache key is
`web_slug(<url as given>)` (`req_slug`), computed before any HTTP call; the
stored entry then records the *resolved* slug (`entry["slug"]`) plus the
validators. `_write_validators` writes under both `req_slug` and the resolved
slug when they differ, so ingesting either the original URL or the redirect
target later hits the same cache. Concretely: ingesting
`http://docs.example.com/old-page` (301 → `https://docs.example.com/new-page`)
twice reports `unchanged` via `304` on the second run, even though the URL you
gave never changed and the slug it resolved to did.

**Never trusts a 304 the raw files can't back up.** Before attaching the
conditional headers, `fetch_web.py` checks that both `raw/<slug>.md` and
`raw/<slug>.source.json` exist for the *resolved* slug from the cached entry.
If either is missing — deleted by hand, or lost some other way — the
conditional headers are omitted, forcing a full `200` that rewrites both
files. This is the one diff gate across all of `/ingest` that self-heals from
a missing raw file without needing `--force`; see
[SKILL.md](../SKILL.md)'s edge cases for how the other fetchers behave
instead.

A consequence worth knowing: deleting `.wiki-state/` (but keeping the raw
files) costs one full re-render per page, not a re-ingest — gate 1 still
suppresses the write.

## Images

Web pages are full of chrome, and a vision call per logo is wasted money. Five
filters run in order:

1. **Content subtree only.** Hints are collected from the node the extractor
   selected, so nav/header/footer images are gone before any pattern matching.
2. **Source form.** `data:` URIs, `.svg`, and `.ico` are dropped. Relative
   `src` values are resolved with `urljoin`; for a `srcset`, the
   highest-width candidate wins.
3. **Name/role patterns.** `src`/`class`/`id`/`alt`/`role` matching
   `logo|icon|avatar|sprite|badge|pixel|tracking|spacer|favicon` is dropped.
4. **Declared dimensions.** An `<img>` with `width` or `height` under 100px is
   decoration.
5. **Byte-size floor.** In `extract_images.py`, a web image smaller than
   `web.min_image_bytes` (default 8192) is skipped and counted as
   `skipped_small`, with **no manifest entry** — so the description loop never
   sees it. Markup often omits dimensions, which is why this backstop exists.

At most 20 hints per page are kept; past that a `WARNING:` goes to stderr.
Survivors then go through the normal manifest → `new`/`changed`/`unchanged`
classification and nano-banana description, identical to Confluence
attachments.

`extract_images.py` always sends `web.user_agent` for `web` sources (a bare
`python-requests` User-Agent is `403`d by many CDNs) and uses the `web` rate
limiter. **`web.extra_headers` — Cookie, Authorization, whatever's configured
— is sent only when the image's host matches the page's own host** (the
page URL from `source.json`'s `url` field). An image embedded from a
third-party CDN never receives them: those headers were configured for the
site being ingested, not for every host that site happens to link an image
from, and leaking a session cookie to an unrelated third party is exactly the
kind of thing a config file shouldn't cause silently.

## Bulk discovery

For `--site <url>`, in order:

1. `Sitemap:` directives in `/robots.txt` — authoritative, and free to point at
   another host or a non-standard filename.
2. `/sitemap.xml`, `/sitemap_index.xml`, `/sitemap-index.xml`,
   `/sitemap.xml.gz` — each probed until one returns 200 and looks like a
   sitemap.
3. Nothing found → `discover.py` prints
   `{"status": "needs_bounds", …}` and exits **0**. It does not crawl. The
   caller asks the user for a depth and a page cap, then re-runs with
   `--crawl <url> --depth N --max-pages M`.

Sitemap parsing handles `<urlset>`, `<sitemapindex>` (recursive, depth cap 3,
with a visited set so a self-referential index can't loop), gzipped sitemaps,
and plain-text one-URL-per-line sitemaps. Namespaces are matched by local name,
so a sitemap with an unusual prefix still parses. Ceilings: 50 000 URLs per
job, then a warning.

`--sitemap <url>` skips discovery. Passing a `robots.txt` URL to `--sitemap`
is understood — it is treated as a pointer and the directives inside are read.

**Non-http(s) `<loc>` entries are rejected.** A sitemap occasionally carries a
stray `mailto:` or `ftp:` entry by mistake. `collect_sitemap_urls` drops
anything whose scheme isn't `http`/`https` before it reaches the queue — the
alternative is `fetch_web.py` failing on it one item at a time, deep inside a
bulk run. Dropped counts are reported in a single `WARNING:`.

### Crawling

`--crawl` is breadth-first, same-origin, HTML-only, and requires **both**
`--depth` and `--max-pages`; there is no default, because an unbounded crawl of
an unknown site is the mistake the signature exists to prevent. Link
extraction is a regex over `href` attributes rather than bs4 — discovery only
needs hrefs and stays stdlib-only. `robots.txt` `Crawl-delay` is honored
between requests, and URLs with non-HTML extensions are skipped without being
fetched.

### Filters

Once a sitemap has thousands of URLs:

| Flag | Effect |
|------|--------|
| `--include REGEX` | Keep URLs matching any `--include` (repeatable) |
| `--exclude REGEX` | Drop URLs matching any `--exclude` (repeatable) |
| `--since YYYY-MM-DD` | Keep entries whose `<lastmod>` is on or after the date. Entries with **no** `<lastmod>` are kept — absence isn't evidence of age, and gate 1 will catch them cheaply |
| `--limit N` | Cap the queue size |

Re-running the same `--site` / `--sitemap` URL reuses its existing queue
(`find_matching` keys on kind + query), so incremental site refreshes are the
default. `--replace` starts over.

**All of `--since`, `--depth`, `--max-pages`, and the URL flags are validated
before any HTTP request** (`discover.py`'s `_validate_web_args`) — a
malformed `--since` (must be `YYYY-MM-DD`, zero-padded) fails loudly instead
of silently dropping most of the sitemap: `"2026-06-15" < "2026-6-1"`
lexicographically, so an unpadded date is a real footgun if compared as a
plain string. `--include`/`--exclude` regex syntax errors are caught too,
just later (inside `apply_filters`, since compiling them isn't worth a
separate up-front pass). All of these surface as a plain `ERROR:` line, never
a traceback.

## robots.txt policy

| Path | Behavior |
|------|----------|
| Single page (`--source <url>`) | Advisory. A disallow logs a `WARNING:` and the fetch proceeds — the user named this one page. |
| `--site` / `--sitemap` / `--crawl` | Enforced, **per origin**. Disallowed URLs are dropped from the queue with a per-origin breakdown in the warning; a `Disallow: /` for the entry-point host aborts discovery outright. |
| `--ignore-robots` | Skips enforcement on the bulk paths. Use only where you have permission. |
| `respect_robots: false` in `.wikirc.json` | Same, permanently. |

**Per-origin, not per-job.** A sitemap can legitimately list URLs on another
host — a root sitemap listing a docs subdomain is common. `RobotsCache` in
`web_discover.py` lazily loads and caches one `RobotFileParser` per origin
(seeded with the entry-point's, so that one isn't fetched twice) and checks
each sitemap entry — and each nested `<sitemapindex>` sitemap — against its
**own** origin's rules. Checking everything against the entry-point's robots
would silently apply the wrong site's policy to cross-origin URLs; `--crawl`
doesn't have this problem since it only ever visits one origin by
construction.

Per the robots standard, a `/robots.txt` that returns **401 or 403** means
"disallow everything"; other 4xx means "no restrictions". A robots.txt that
can't be fetched at all is treated as no restrictions, with a warning.

`prefetch.py` passes `--no-robots-check` to `fetch_web.py`, since discovery
already enforced robots for the whole job — re-checking per page would double
the request count.

## Config

```json
"web": {
  "user_agent": "Mozilla/5.0 (compatible; llm-wiki-ingest/1.0)",
  "verify_ssl": true,
  "rate_limit_rps": 1,
  "burst": 2,
  "max_retries": 3,
  "retry_base_delay_seconds": 2,
  "timeout_seconds": 30,
  "respect_robots": true,
  "min_image_bytes": 8192,
  "extra_headers": {}
}
```

Every key has a default, so an existing `.wikirc.json` with no `web` block
works unchanged. `extra_headers` is where a `Cookie` or `Authorization` header
goes for a page behind a login; `config.py --wiki-root` redacts the values but
prints the header names. These headers reach only the page's own host — see
[Images](#images) for the same-origin scoping on embedded images.

## Troubleshooting

### HTTP 403 on a page that loads in the browser

The default User-Agent is being blocked. Set `web.user_agent` to a real
browser string, or add the site's session cookie to `web.extra_headers`.

### "no readable content extracted"

Client-side rendering. Save the page from the browser and ingest the `.html`
file instead.

### The same page keeps producing a "changed" diff

Something in the rendered Markdown is genuinely varying — a rotating CSRF
token, a "last viewed" line, an ad slot, a visitor counter. Check
`git diff raw/<slug>.md` to see what moved. There is no per-source ignore
mechanism; if it's noise inside a region the extractor keeps, the practical
answer is to stop re-ingesting that page.

### A sitemap URL 404s but the site clearly has one

Read `/robots.txt` directly — many sites use a non-standard filename or host
their sitemap on a CDN. `--sitemap <exact-url>` accepts anything.

### A page I ingest keeps re-fetching in full instead of hitting the 304 cache

Either the raw files for it are actually missing (deleted, moved, or never
successfully written) — check `raw/<slug>.md` and `raw/<slug>.source.json`
both exist — or the URL redirects somewhere new each time (a rotating
tracking redirect, a load balancer sending you to a different mirror). A
stable redirect (`http` → `https`, a permanently moved page) is handled
automatically; check `.wiki-state/last-fetched.json`'s `web:<slug>` entry for
the `slug` it resolved to if the caching still looks wrong.

### An image request is missing the Cookie/Authorization header I configured

That's by design if the image is hosted on a different domain than the page —
`web.extra_headers` only reaches the page's own host. If the image genuinely
needs the same credentials and is same-origin, check that
`source.json`'s `url` field actually matches that origin (a redirect to a
different host would change this).

### Discovery found 0 items from a valid sitemap

The sitemap may list only section roots (docs.python.org's lists 8 version
roots, nothing more), or every entry may have been filtered out by
`--include`/`--exclude`/`--since`, or dropped as non-HTML. Run
`discover.py --site <url>` with no filters to see the unfiltered count.

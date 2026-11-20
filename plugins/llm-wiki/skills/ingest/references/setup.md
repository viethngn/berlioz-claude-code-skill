# Setup Guide

Read this when the user hits a dependency error, a config error, or is
setting up the plugin for the first time.

## Requirements

- Python 3.10 or newer
- `git` on PATH
- `bash`

## One-time install

Run the installer once per machine:

```bash
bash <marketplace>/plugins/llm-wiki/install.sh
```

It tries three strategies in order:

1. `uv pip install -r requirements.txt --system` — fastest, if `uv` is on PATH
2. `python3 -m pip install --user -r requirements.txt` — installs to user site-packages
3. `python3 -m pip install -r requirements.txt` — global install (last resort)

Then it runs `check-setup.sh` and prints a green summary.

## Verify

```bash
bash <marketplace>/plugins/llm-wiki/check-setup.sh
```

Reports Python version, imports each dependency, and checks for `git`.

Optionally validate a specific `.wikirc.json`:

```bash
bash <marketplace>/plugins/llm-wiki/check-setup.sh /path/to/wiki/.wikirc.json
```

## The `.wikirc.json` config

Every wiki has a `.wikirc.json` at its root. Fields:

```json
{
  "wiki_root": ".",
  "raw_dir": "raw",
  "wiki_dir": "wiki",
  "auto_commit": true,
  "atlassian": {
    "confluence_base_url": "https://your-confluence.example.com",
    "jira_base_url": "https://your-jira.example.com",
    "confluence_pat": "REPLACE_ME_OR_LEAVE_EMPTY",
    "jira_pat": "REPLACE_ME_OR_LEAVE_EMPTY",
    "verify_ssl": true
  },
  "images": {
    "describe": false
  },
  "web": {
    "user_agent": "Mozilla/5.0 (compatible; llm-wiki-ingest/1.0)",
    "verify_ssl": true,
    "respect_robots": true,
    "min_image_bytes": 8192,
    "extra_headers": {}
  }
}
```

- **`wiki_root`** — Directory containing `raw/` and `wiki/`. Defaults to `.`
  (the directory containing `.wikirc.json`).
- **`raw_dir`**, **`wiki_dir`** — Subdirectory names. Rarely changed.
- **`auto_commit`** — If `true`, `ingest.py` commits changes at the end of each
  run. If `false`, changes are staged but committing is left to the user.
- **`atlassian.*_base_url`** — Root of your Confluence / Jira. No trailing
  `/rest/...` — just the host + optional context path.
- **`atlassian.*_pat`** — Personal Access Token. Leave empty if you don't need
  that source; ingest of that type will fail with a clear message but
  everything else keeps working.
- **`atlassian.verify_ssl`** — Set to `false` only for a corporate network
  that intercepts TLS. Prefer setting a proper CA cert instead (see below).
- **`images.describe`** — `false` by default: ingest downloads no images and
  only text reaches the wiki (each source's `image_hints` stay in its
  `source.json`, so turning this on later can still pick them up). Set it to
  `true` to download embedded images of new or changed sources and have
  Claude — the session you are running, no separate endpoint or key —
  describe the new or changed ones. `ingest.py --describe-images` turns it
  on for one run; `--skip-images` turns it off for one run. An old
  vision-endpoint block left over from an earlier version is ignored.
- **`slack.verify_ssl`** / **`web.verify_ssl`** — Same semantics as
  `atlassian.verify_ssl`, for Slack API and website requests respectively.
- **`web.*`** — Website ingest. Every key has a default, so this whole block is
  optional and public pages need no credentials at all.
  - **`user_agent`** — Sent on every page and image request. Set it to a real
    browser string if a site returns 403 to the default.
  - **`respect_robots`** — When `true` (default), robots.txt is enforced on
    bulk website ingest (`--site` / `--sitemap` / `--crawl`) and advisory for a
    single explicitly-named page.
  - **`min_image_bytes`** — Web images smaller than this are skipped as
    decoration, so no description is spent on icons and spacers (only
    relevant when `images.describe` is on).
  - **`extra_headers`** — Arbitrary headers, e.g.
    `{"Cookie": "session=…"}` for a page behind a login. `config.py` redacts the
    values when printing the config.
  - **`rate_limit_rps`**, **`burst`**, **`max_retries`**,
    **`retry_base_delay_seconds`**, **`timeout_seconds`** — same semantics as
    the `atlassian` block. Defaults are deliberately polite (1 rps, burst 2).

`.wikirc.json` is git-ignored by default. Only `.wikirc.example.json` (with
placeholder values) is committed.

## Troubleshooting

### `pip install` fails with a network error

Options in order of preference:

**Corporate PyPI mirror:**

```bash
pip config set global.index-url https://your-mirror.example.com/simple
pip config set global.trusted-host your-mirror.example.com
```

Or in-shell:

```bash
export PIP_INDEX_URL=https://your-mirror.example.com/simple
export PIP_TRUSTED_HOST=your-mirror.example.com
```

**TLS-intercepting proxy:**

Export the corporate root CA:

```bash
export SSL_CERT_FILE=/path/to/corp-ca.pem
export REQUESTS_CA_BUNDLE=/path/to/corp-ca.pem
```

**Air-gapped machine:**

Download wheels on a connected machine:

```bash
pip download -r requirements.txt -d ./wheels
```

Copy `./wheels/` to the target machine and install offline:

```bash
pip install --no-index --find-links ./wheels -r requirements.txt
```

Note that `trafilatura` (website ingest) pulls in a handful of transitive
dependencies — `lxml`, `charset-normalizer`, `courlan`, `htmldate`, `justext`.
`pip download -r requirements.txt` collects them all, so nothing extra is
needed, but expect more wheels than the line count of `requirements.txt`
suggests. If `trafilatura` can't be installed at all, website ingest still
works: `fetch_web.py` falls back to the BeautifulSoup + markdownify extractor
already required by local-file ingest, with somewhat noisier output.

### `check-setup.sh` reports a missing package after `install.sh` succeeded

Common causes:

- Multiple Python interpreters. Set `PYTHON=/full/path/to/python3` before
  running `install.sh` and `check-setup.sh` to pin the version.
- `--user` install went to a different Python's user-site. Try
  `python3 -m pip install --user -r requirements.txt` directly and confirm the
  install path.

### `verify_ssl: true` in `.wikirc.json` but requests fail with SSL errors

Set `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to your corporate CA bundle path.
Only set `verify_ssl: false` as a last resort — it disables MITM protection
for those API calls.

### PAT auth returns 401

- Confluence Server/DC uses `Authorization: Bearer <PAT>`.
- Confluence Cloud uses `Authorization: Basic <base64(email:api_token)>` —
  this plugin targets Server/DC by default. For Cloud, put
  `email:api_token` base64-encoded in the `*_pat` field and it will still work
  as a Bearer value (Cloud accepts either after 2023).
- Jira: same rules.
- Some corporate networks strip the `Authorization` header at the proxy —
  check with `curl -v` first.

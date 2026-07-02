---
name: ingest
description: |
  Ingests a source — a Confluence page URL, a Jira issue URL or key, or a local
  file (Markdown, plain text, HTML, PDF, DOCX, or an image) — into an LLM wiki.
  Fetches the content, extracts any embedded images, describes only the
  new-or-changed images via a nano-banana-pro-compatible vision endpoint,
  writes the raw source and wiki pages, and commits the result to git so the
  next ingest can diff against it.

  Use this skill whenever the user wants to add, update, refresh, re-ingest, or
  import a source into their wiki. Trigger on phrases like: "ingest this
  Confluence page", "add this Jira ticket to the wiki", "pull this URL into the
  wiki", "ingest this PDF", "process this document", "add this file to the
  wiki", "refresh this source", "re-ingest this page", "update the wiki from
  this doc", or when the user pastes a Confluence/Jira URL alongside "wiki".

  Requires a per-wiki `.wikirc.json` file with Confluence, Jira, and nano-banana
  endpoints and Personal Access Tokens. If the config is missing or scripts
  cannot import their dependencies, direct the user to
  references/setup.md before proceeding.
---

# Ingest — LLM Wiki

Pull one source into a wiki, describe its images on diff, update the wiki
pages, and commit the result to git so future runs can compare against the
committed state.

## Prerequisites

Before running any script, verify:

1. The wiki root has a `.wikirc.json` (not `.wikirc.example.json`). If missing,
   direct the user to `/create-wiki` or to fill in the example config.
2. Python dependencies are installed. If any script exits with a "Missing
   dependencies" message, direct the user to
   [references/setup.md](references/setup.md) and stop.
3. `git` is available and the wiki root is a git repository. If not,
   `git init` first (bootstrap.py does this for new wikis).

## Required inputs

Ask for these upfront if not clear from the user's message:

| Input | Format |
|-------|--------|
| Source | Confluence URL, Jira URL / key, or absolute local file path |
| Wiki root | Path to the wiki directory (defaults to cwd) — must contain `.wikirc.json` |

The scripts auto-detect the source type. You do not need to ask the user.

## Workflow

Follow these phases in order. `${SKILL_DIR}` refers to the directory containing
this file. `${WIKI_ROOT}` is the wiki directory (contains `.wikirc.json`).

### Phase 1 — Detect and fetch the source

Run the orchestrator, which will dispatch to the correct fetcher, extract
images, apply the image diff gate, and stage everything under `${WIKI_ROOT}/raw/`:

```bash
python3 "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --source "<Confluence URL | Jira key | local file path>"
```

The orchestrator prints a JSON summary of what happened to stdout, one line
per key event. Parse it to know which files were created and how many images
were described.

**Detection rules** (already implemented in `ingest.py`; here for your
reference):

- URL containing `/pages/<digits>/` or `pageId=` → Confluence
- URL containing `/browse/<KEY>` or a bare `KEY-123` → Jira
- Anything else beginning with `/` or `~` or an existing path → local file

### Phase 2 — Review takeaways with the user

After the orchestrator finishes, read the newly written `raw/<slug>.md` and any
image description files under `raw/images/<slug>/`. Then tell the user:

> I ingested **[title]** from [source-type]. Key takeaways:
>
> - [3-6 bullet points on what the source is about]
>
> Images: [N new, M changed, K unchanged, described via nano-banana-pro]
>
> Ready to update the wiki? I'll create/update these pages:
> - [[proposed-page-1]] — [one-line reason]
> - [[proposed-page-2]] — [one-line reason]
> - Update [[existing-page]] with [what changes]

Wait for the user's confirmation or corrections before writing wiki pages.

### Phase 3 — Update wiki pages

Follow the rules in [references/page-format.md](references/page-format.md).
For each page you touch:

- Use the page template (Summary / Sources / Last updated / body).
- Add or update `[[wiki-links]]` to connect related concepts.
- Cite every factual claim: `(source: <raw-filename>)`.
- Update `wiki/index.md` with new pages and one-line descriptions.
- Append an entry to `wiki/log.md` with the date, source name, and what
  changed.

A single source can touch 10-15 wiki pages. That is normal — do not batch
edits into a single mega-page.

### Phase 4 — Commit to git

Run the commit at the end. `ingest.py` handles this when `auto_commit=true`
(the default), but you can call it explicitly:

```bash
python3 "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --commit-only \
  --slug "<slug-from-phase-1>" \
  --new-images N \
  --changed-images M
```

Or manage git yourself with `git add raw/ wiki/ && git commit -m "..."` if the
user prefers `auto_commit=false`.

**Commit message format:**

```
ingest: <slug> (N new, M changed images)
```

## Concrete rules

- **Slug**: `slugify(title)` for Confluence, `KEY-123-<slug-of-summary>` for
  Jira, filename stem for local files. Enforced by `ingest.py`.
- **Raw layout**:
  - `raw/<slug>.md` — Markdown-converted source content
  - `raw/<slug>.source.json` — stable metadata:
    `{ "type", "url" or "path", "title", "content_sha256", "source_sha256"
    (local only), "image_hints", "version_number" (Confluence), "updated_at"
    (Jira) }`. **No wall-clock timestamps.**
  - `raw/images/<slug>/<n>.<ext>` — downloaded image bytes
  - `raw/images/<slug>/<n>.md` — nano-banana-pro description
  - `raw/images/<slug>/.manifest.json` — per-image `{ sha256, source_url,
    description_file, described_at }`; the source of truth for image diffs
    and URL-based dedup
- **Volatile state lives outside git**:
  - `.wiki-state/last-fetched.json` at the wiki root records the timestamp
    and status of the most recent fetch per slug. Git-ignored via the
    template `.gitignore`. Not part of the tracked history.
- **Content diff gate (Layer 1)**: each fetcher computes a SHA-256 over the
  rendered Markdown. If it matches the previous `content_sha256`, the
  fetcher returns `status="unchanged"` and does not rewrite `raw/<slug>.md`
  or `raw/<slug>.source.json`. `fetch_local.py` also fast-paths on
  `source_sha256` (the raw file bytes) to avoid re-parsing PDFs/DOCX.
- **Orchestrator gate (Layer 2)**: when the fetcher reports `unchanged`,
  `ingest.py` skips image download, image description, and the git commit.
  Only `.wiki-state/last-fetched.json` is updated. Pass `--force` to
  bypass this and re-run every step.
- **Image dedup gate (Layer 3)**: `extract_images.py` downloads each image
  into memory, hashes it, and looks up the manifest by `source_url` first,
  then by `sha256`. Matches reuse the existing filename; mismatched hashes
  overwrite the same filename in place. This prevents duplicate files on
  re-ingest and keeps nano-banana-pro calls tied to actual byte-level
  changes.
- **Image description gate (Layer 4)**: `image_manifest.py.classify()`
  returns `new` / `changed` / `unchanged`. Only `new` or `changed` images
  invoke `describe_image.py`; unchanged images reuse the existing `.md`
  description.
- **PAT auth**: `Authorization: Bearer <PAT>` for both Confluence and Jira
  Server/DC. If the required PAT is empty in `.wikirc.json`, the fetch script
  exits with a clear message — direct the user to fill in the token.
- **Never modify anything in `raw/`** during Phase 3 (wiki update). `raw/` is
  the immutable ingested source; wiki pages are your synthesis.

## Individual script reference

You can run scripts individually for debugging or non-standard flows.

| Script | Purpose |
|--------|---------|
| `scripts/ingest.py` | Orchestrator — dispatches to the right fetcher, runs image diff, commits |
| `scripts/config.py --wiki-root <path>` | Print the resolved `.wikirc.json` (redacts PATs) |
| `scripts/fetch_confluence.py --wiki-root <path> --url <url>` | Fetch one Confluence page |
| `scripts/fetch_jira.py --wiki-root <path> --key <KEY-123>` | Fetch one Jira issue |
| `scripts/fetch_local.py --wiki-root <path> --path <file>` | Ingest one local file |
| `scripts/extract_images.py --raw <file>` | Extract image references from a raw source |
| `scripts/image_manifest.py --slug <slug> --wiki-root <path> --status` | Print diff status per image |
| `scripts/describe_image.py --wiki-root <path> --image <path> --output <path>` | Describe one image |

All scripts respond to `--help` with their full argument list.

## Edge cases

- **Confluence page has no body** (e.g. draft): warn the user and stop — do
  not create an empty raw file.
- **Jira ticket not found or 403**: surface the API status code to the user
  and suggest checking the PAT.
- **Local file type unsupported** (e.g. `.xlsx`): skill exits with a list of
  supported types. Suggest exporting to PDF or Markdown.
- **Image URL is authenticated** (Confluence attachment): the fetcher passes
  the Confluence PAT when downloading images from the same host.
- **`nano_banana.api_key` empty**: image description is skipped; the manifest
  records images without descriptions and the user is warned. Everything else
  still runs.
- **Wiki not a git repo**: `ingest.py` refuses to run with `auto_commit=true`
  and instructs the user to `git init` or run `/create-wiki`.
- **Re-ingest of unchanged source**: the content diff gate matches; skill
  reports `status="unchanged"`, skips image download / description / commit,
  and only updates `.wiki-state/last-fetched.json`. Tell the user "already
  up to date — nothing to commit". Pass `--force` to override.
- **User manually deleted a file under `raw/`**: the diff gate still sees
  the source as unchanged (source hash matches) and won't restore the file.
  Advise the user to run with `--force`.

## Reference docs

| Doc | Load when |
|-----|-----------|
| [references/setup.md](references/setup.md) | User hits any dependency or config error, or is setting up for the first time |
| [references/atlassian-api.md](references/atlassian-api.md) | Debugging Confluence/Jira fetches or writing a bulk-ingest variant |
| [references/local-files.md](references/local-files.md) | Debugging local file parsing or supporting a new format |
| [references/page-format.md](references/page-format.md) | Phase 3 (wiki update) — page template and citation rules |

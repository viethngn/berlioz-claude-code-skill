---
name: log
description: |
  Turns meeting notes, transcripts, action logs, or pasted text into
  structured Event entries on a per-day wiki page — one page per calendar
  day, each Event capturing Action/What/When/Where/Who/Why, links to related
  past Events, and Next steps. What/Who are resolved as read-only links into
  other configured llm-wiki instances (a topic wiki, a people wiki) via this
  wiki's `.wikirc.json` `linked_wikis` config — never written into those
  wikis. Works like /ingest but for decisions and events rather than
  documents: raw material lands in this wiki's own raw/ (a local file, or
  pasted text handed to it directly), and the wiki-update phase produces or
  updates a `wiki/YYYY-MM-DD.md` page instead of a topic page.

  Bare `/log` with no argument auto-scans this wiki's raw/ for sources not
  yet reflected in a day-page (tracked via .wiki-state/last-logged.json) and
  processes only what's pending — the diary equivalent of /ingest's bare
  refresh-all.

  Use this skill whenever the user wants to log a meeting, log a decision,
  record what just happened, backfill the diary from an old transcript, log
  action items, or check what's pending in the diary. Trigger on phrases
  like: "log this meeting", "log this transcript", "log what we just
  decided", "add this to the diary", "backfill the log from this note",
  "log this", "what's pending in the log", "process my notes into the diary".

  Requires a `.wikirc.json` with a `linked_wikis` array configured (see
  `skills/ingest/scripts/config.py`) if Who/What resolution against other
  wikis is wanted — /log still works without it, just leaving every mention
  unresolved.
---

# Log — LLM Wiki Work Diary

Extracts discrete 5W1H Events from raw meeting notes, transcripts, or action
logs and files them onto one flat page per calendar day
(`wiki/YYYY-MM-DD.md`) — a day-page is a page like any other in the wiki, not
a new kind of folder. Runs end-to-end automatically, same as `/ingest`: no
approval pauses except for genuinely ambiguous Who/What matches, which are
resolved optimistically and simply flagged, never blocked on.

## Prerequisites

Same as `/ingest`: a `.wikirc.json` at the wiki root, Python dependencies
installed (see `../ingest/references/setup.md` if any script reports missing
dependencies), and a git repository (for the final commit).

## Resolving the Python interpreter

Identical to `/ingest`/`/lint`:

```bash
_LLMWIKI_VENV="${LLMWIKI_VENV:-${HOME}/.llm-wiki-venv}"
if [ -x "${_LLMWIKI_VENV}/bin/python3" ]; then
  WIKI_PY="${_LLMWIKI_VENV}/bin/python3"
else
  WIKI_PY="${PYTHON:-python3}"
fi
```

Use `${WIKI_PY}` instead of `python3` below. `${SKILL_DIR}` is this file's
directory; `${WIKI_ROOT}` is the wiki directory (contains `.wikirc.json`).
`${INGEST_DIR}` is `${SKILL_DIR}/../ingest/scripts` — several phases below
reuse `ingest.py` directly rather than duplicating its fetch/commit logic.

## Phase 0 — Detect source shape

- An explicit file path was given → **local-file path** (Phase 1a).
- Text was pasted/typed directly into the conversation, no path → **pasted-text
  path** (Phase 1b).
- No argument at all → **bare auto-scan** (Phase 1c).

## Phase 1 — Get the raw material into `raw/`

### 1a — Local file path

Reuse `/ingest`'s existing local-file dispatch verbatim — nothing new to
build for this case, and it gets image extraction for free:

```bash
${WIKI_PY} "${INGEST_DIR}/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --source "<path>" \
  --no-commit
```

Parse the JSON summary for `slug` and `status`. If `status == "unchanged"`,
the file's content hasn't changed since it was last ingested — skip straight
to checking whether it's already been *logged* (see 1c's pending check) since
an unchanged fetch doesn't mean it's already a day-page.

### 1b — Pasted text

No backing file exists yet, so write one:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/write_raw_note.py" \
  --wiki-root "${WIKI_ROOT}" \
  --title "<a short descriptive title>" \
  --source-label "<free text: where this came from, e.g. 'pasted meeting notes'>" \
  <<'EOF'
<the pasted note body, verbatim>
EOF
```

Piping the body via stdin (heredoc) avoids shell-escaping arbitrarily long or
punctuation-heavy pasted text. Parse the JSON summary for `slug` and
`status`, same shape as `ingest.py`'s.

### 1c — Bare auto-scan

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/log_state.py" --wiki-root "${WIKI_ROOT}" --pending
```

Returns `{"pending": [<slug>, ...]}` — every `raw/*.source.json` whose slug
isn't yet in `.wiki-state/last-logged.json`. Iterate Phases 2-6 once per
pending slug. If the list is empty, report "everything is already logged"
— that is the expected steady state, not a silent no-op.

## Phase 2 — Extract Events (non-blocking report)

Read `raw/<slug>.md`. Identify one or more discrete Events per
[references/event-format.md](references/event-format.md) — a single meeting
transcript often contains several distinct decisions/events, each with its
own Action/What/When/Where/Who/Why. Report what you found as an FYI, then
proceed automatically — do not wait for confirmation:

> Logging **[source title]**. Found N events:
> - [HH:MM] [Action] — [What] (involving [Who])
> - ...

## Phase 3 — Resolve Who/What, write day-pages

For each name/topic mentioned in an Event's `Who`/`What` fields:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/resolve_link.py" \
  --wiki-root "${WIKI_ROOT}" --role who --query "<name>"
# or --role what, or --label <label> for a specific linked wiki
```

- A confident match → `[[label/slug]]`.
- No confident match → the plain-text name plus an unresolved marker
  (`⚠ *(unresolved — no match in <label>)*`), collected for Phase 7's report.
- `configured: false` in the result means that `linked_wikis` entry is still
  a placeholder (unfilled path) — treat every mention meant for it as
  unresolved and say so once in the final report, not per-mention.
- **Never** write into the linked wiki to create a page for an unresolved
  mention — that boundary is intentional (see
  [references/cross-wiki-links.md](references/cross-wiki-links.md)).

For each Event, determine its day-page from the `When` date (which may be in
the past — backfilling from an old transcript is expected, not an edge
case):

- If `<wiki-root>/templates/day-page.md` doesn't exist yet, copy it in from
  `${SKILL_DIR}/templates/day-page.md` first (self-healing seed — `/create-wiki`
  doesn't know about day-pages, so `/log` owns making sure the template
  exists).
- If `wiki/YYYY-MM-DD.md` doesn't exist, create it from that template.
- Insert the Event's `##` section in time-of-day order (earliest first)
  within that page.
- **Never edit a previously-logged Event's substantive fields in place.** A
  correction appends a new `## ... — Update <date>` block with an
  `**Amends**:` link to the original heading. Full rules in
  [references/event-format.md](references/event-format.md).

## Phase 4 — Update `wiki/index.md`'s Diary section

Maintain a rolling list of the most recent ~14 day-pages under a `## Diary`
section, newest-first (`[[YYYY-MM-DD]]`). Once that list would exceed the
wiki's documented category-split threshold (~40 outbound links, see
`CLAUDE.md`'s "Scale runway" section if present), roll older entries into a
monthly rollup page (`wiki/YYYY-MM-index.md`) and link to that instead of
listing every day individually.

## Phase 5 — Append one entry to the operational `wiki/log.md`

This is the **existing, separate** append-only operational log that
`/ingest` and `/lint` already write to — not the new day-pages. Append:

```markdown
## YYYY-MM-DD (log)

- Logged `raw/<slug>.md` — N events across day-pages [[YYYY-MM-DD]], [[YYYY-MM-DD]]
```

Do not conflate this with the day-pages themselves, even though both now
live flat in the same `wiki/` directory.

## Phase 6 — Mark watermark and commit

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/log_state.py" \
  --wiki-root "${WIKI_ROOT}" --mark "<slug>" --pages "YYYY-MM-DD,YYYY-MM-DD"

${WIKI_PY} "${INGEST_DIR}/ingest.py" \
  --wiki-root "${WIKI_ROOT}" --commit-only --slug "<slug>" \
  --message "log: <slug> (N events, M day-pages)"
```

`--commit-only` reuses the exact same commit/push machinery `/ingest` uses
(`git add <raw_dir> <wiki_dir>`, then commit, then push if `auto_push`).
`--slug` is only used for `ingest.py`'s *default* commit message, which
`--message` overrides here — any placeholder value for `--slug` is fine as
long as `--message` is always passed.

If processing multiple pending sources (bare auto-scan), repeat Phases 2-6
per source rather than batching into one giant commit — this keeps each
commit's message meaningfully tied to one source, same as `/ingest`'s bulk
synthesis loop.

## Final report

List every unresolved Who/What reference collected across the run, grouped
by which linked wiki they were meant for, so the user can decide whether to
`/ingest` more material into that wiki or correct a name.

## Concrete rules

- **Day-pages are flat `wiki/YYYY-MM-DD.md` files** — no `wiki/diary/`
  subfolder. This matches every other page in the wiki and keeps `/lint`'s
  ordinary page scan working on them with no changes to `lint.py`.
- **Cross-wiki links (`[[label/slug]]`) are read-only.** `/log` never writes
  into a linked wiki, regardless of match confidence.
- **Immutability**: never edit a previously-logged Event's substantive
  fields; append a dated `Update`/`Amends` block instead. Checking off a
  `Next steps` checkbox is the one legitimate in-place edit.
- **Never edit `raw/`'s contents** — same rule as `/ingest`/`/lint`.

## Reference docs

| Doc | Load when |
|-----|-----------|
| [references/event-format.md](references/event-format.md) | Extracting Events or writing/updating a day-page |
| [references/cross-wiki-links.md](references/cross-wiki-links.md) | Resolving or writing a `[[label/slug]]` link |
| `../ingest/references/setup.md` | A script reports missing dependencies |

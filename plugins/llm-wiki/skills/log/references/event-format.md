# Event & Day-Page Format Reference

Load during Phase 2/3 of `/log` (Event extraction and day-page synthesis), or
whenever you're reading/writing a day-page. This is the canonical template
the `log` skill enforces — mirrors `skills/ingest/references/page-format.md`
for the generic page shape, adapted for one-page-per-day event logging.

## Day-page container

A day-page is `wiki/YYYY-MM-DD.md` — a **flat page, same as every other page
in the wiki**. There is no `wiki/diary/` or similar subfolder; this keeps the
wiki structurally identical to every other llm-wiki instance, and keeps
day-pages visible to `/lint`'s ordinary (non-recursive) `wiki/*.md` scan.

```markdown
# YYYY-MM-DD

**Summary**: Events logged on this day.

**Sources**:
- `raw/<slug-1>.md`
- `raw/<slug-2>.md`

**Last updated**: YYYY-MM-DD

---

## HH:MM — <Action> <What>

...Event fields (see below)...

## HH:MM — <Action> <What>

...
```

The seed template lives at `templates/day-page.md` (both the plugin's own
copy, `skills/log/templates/day-page.md`, and the wiki's own
`<wiki-root>/templates/day-page.md`, seeded from the plugin's copy — see
"Templates" below).

**Ordering within a day**: earliest-first (chronological), by the Event's
`When` time-of-day. This is the opposite of `wiki/index.md`'s Diary section,
which lists day-pages newest-first — don't conflate the two orderings.

## Event fields

Each Event is one `##` section within a day-page:

```markdown
## HH:MM — Action What

**Action**: What happened, in a few words.
**What**: [[pdm/gmd-domo-reporting]]
**When**: 2026-09-04T14:30+09:00
**Where**: Slack #ad-suite-pm
**Who**: [[people/reyad-ahammad]], [[people/aaron-wang]]
**Why**: Rationale for the decision or action.
**Related events**: [[2026-08-30#09:00 — Earlier related event]]
**Next steps**:
- [ ] [[people/aaron-wang]] to draft the RFC by 2026-09-10
**Sources**: `raw/<slug>.md`
```

Field rules:

- **Action**: the outcome/decision — a short verb phrase.
- **What**: the entity/topic/object the action is about. Resolve it against
  the linked wiki tagged `role: "what"` in `.wikirc.json` and link it as
  `[[label/slug]]` (see `references/cross-wiki-links.md`) on a confident
  match. On no confident match, write the plain-text name plus an unresolved
  marker: `<name> ⚠ *(unresolved — no match in <label>)*`. **Never** write
  into the linked wiki to create a page for it — collect it in `/log`'s final
  report instead.
- **When**: full ISO 8601 with a UTC offset. This is the *event* time, which
  may be well in the past when backfilling from an old transcript — it is NOT
  the time `/log` happened to run.
- **Where**: free text — the channel or medium (Slack, Teams, Zoom, in
  person, email, ...).
- **Who**: same resolution rule as `What`, but against the linked wiki tagged
  `role: "who"`. Comma-separated `[[label/slug]]` links for multiple people.
- **Why**: prose rationale. May contain inline `[[links]]` to other wiki
  pages (this wiki's own pages, or cross-wiki) for supporting context.
- **Related events**: links to other Event headings, using Obsidian's native
  heading-link syntax `[[YYYY-MM-DD#Exact Heading Text]]` — chosen over a
  same-page-only anchor because related events routinely cross day-pages.
- **Next steps**: a Markdown task list. Each item names who needs to do what
  by when — `[ ] [[label/slug]] to <action> by <date>`. Check the box off in
  a later `/log` run once the source material confirms it's done (this IS a
  legitimate in-place edit — checking a box is not amending the Event's
  substance).
- **Sources**: same citation convention as the rest of the wiki —
  `raw/<slug>.md`.

## Immutability and backfill

**Never edit a previously-logged Event's substantive fields in place.** If a
later source corrects or updates something about an Event already written to
a day-page, append a new Event-shaped block right after it:

```markdown
## HH:MM — Action What — Update YYYY-MM-DD

**Amends**: [[YYYY-MM-DD#Original Heading Text]]
**Action**: What changed.
...(only the fields that changed; omit the rest)...
**Sources**: `raw/<new-slug>.md`
```

This mirrors the ADR/event-sourcing pattern the rest of this design borrows
from: a correction is a new, separately-dated fact, not a silent rewrite of
history. Checking off a `Next steps` checkbox is the one exception noted
above.

## Templates

`templates/day-page.md` in the wiki root is the seed template for a brand-new
day-page, mirroring how `templates/page.md` seeds every other page. Since
`/create-wiki`'s `bootstrap.py` doesn't know about day-pages, `/log` is
responsible for making sure this file exists:

- Checked once during the `daily-log-wiki` scaffold step (copied in from the
  plugin's `skills/log/templates/day-page.md`).
- Self-healed by `/log` itself before writing the first day-page in a
  session, in case it's ever missing: copy it in from the plugin's canonical
  copy rather than erroring.

## Citation rules

Identical to the rest of the wiki: every factual claim cites its source
inline, `(source: raw/<slug>.md)`. Uncited claims get
`(source: needs verification)`.

## Cross-wiki links

See `references/cross-wiki-links.md` for the `[[label/slug]]` resolution
mechanism, the `role: "who"`/`role: "what"` convention, and the accepted
Obsidian and `/lint` limitations.

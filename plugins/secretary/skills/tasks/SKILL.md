---
name: tasks
description: |
  Add, update, list/render, mark done, remove (archive), and add subtasks
  to todo items in a project bootstrapped by /create-secretary. Every
  add/update/done/remove action commits to git immediately. Rendering
  groups tasks Overdue → Due soon → Later, with subtasks indented under
  their parent and a due-soon/overdue digest available on demand.
  Automatically syncs Slack (and Outlook, once configured) before
  answering questions about the list, reconciling into existing tasks
  rather than creating duplicates.

  Use this skill whenever the user wants to manage their todo list: add a
  task, update or edit a task, mark something done, remove or delete a
  task, add a subtask, list or show their tasks, or ask what's due/overdue
  — or wants to be caught up on Slack/Outlook action items. Trigger on
  phrases like: "add a task", "what's on my list", "what's due this
  week", "mark T-0007 done", "add a subtask to T-0003", "remove T-0002",
  "show my todos", "catch me up", "what's new", "anything I'm missing",
  "check my messages".
---

# Tasks

Day-to-day task management, backed by `secretary/tasks/`,
`secretary/archived/`, and `secretary/index/index.md`. All operations go
through `scripts/tasks_cli.py`, which wraps the shared engine in
`../../scripts/task_store.py` — never hand-edit task files directly.

## Auto-sync first, always both sources

Before listing tasks, rendering a digest, or answering anything about
"what's on my plate" / "catch me up" / etc., **run the sync gather step
first** so the list reflects Slack (and Outlook) before you answer —
the user should never have to explicitly say "check Slack":

```bash
python3 "${PLUGIN_ROOT}/scripts/sync.py" --tasks-root "${PROJECT_ROOT}"
```

This always attempts **both** `slack` and `outlook` (never just one) and
never writes anything itself — it returns, per source, one of:

- `not_configured` — nothing to do (e.g. Outlook hasn't been connected via
  `/connect-outlook` yet, or Slack has no channels configured). Skip
  silently; don't nag the user about a source they haven't set up. If the
  user specifically asks why Outlook isn't syncing, point them at
  `/connect-outlook` rather than trying to build/debug the connection
  yourself.
- `delegate` (both Slack's usual state and Outlook's once connected) —
  `instruction` tells you exactly which MCP tool calls to make
  (`slack_read_channel`/`slack_search_public*` for Slack;
  `mail`/`calendar` for Outlook) and the date window. Make those calls,
  then extract.
- `ready` — `material` already has fetched content (the llm-wiki-fetcher
  fallback path); read it directly, no MCP calls needed.
- `error` — report the `note` plainly; don't retry in a loop.

## From raw material to reconciled tasks — never duplicate

For each message/item you judge to be an actual action item:

1. Compute a `source_ref` (Slack: `<channel_id>:<ts>`; Outlook: the
   message/event id) — this is the exact-match dedup key.
2. Check it against the `existing_source_refs` list `sync.py` returned for
   that source. If present, you already have a task for this exact item —
   still call `upsert` (below) so an edited message refreshes it, but don't
   treat it as new.
3. **Also check for a same-work-item match by judgment**, not just exact
   ref: if an open task's title/body is clearly about the same thing (e.g.
   a Slack follow-up "any update on the deck?" about an existing "Finish Q3
   deck" task), prefer **updating that task** (`update ID ...`, and append
   the new context to its body) over creating a second one. This is a
   judgment call — when it's ambiguous, ask rather than guess.
4. Call the reconciling upsert (creates OR updates — never a plain `add`
   for synced items):
   ```bash
   python3 "${SKILL_DIR}/scripts/tasks_cli.py" --tasks-root "${PROJECT_ROOT}" \
     upsert --source slack --source-ref "C123:1700000000.001" \
       --title "Review the deck" --due-date 2026-08-01
   ```
   Report its `verdict`: `created`, `updated`, `unchanged` (no-op, don't
   mention it), or `skipped` (matched a task the user already archived —
   don't resurrect it; you can mention "you'd already dismissed this one").

## Propose-then-confirm

- **New tasks from a sync** (verdict would be `created`) and **judgment-based
  merges** (step 3 above): propose the candidate list to the user first —
  title, due date if any, source — and only call `upsert` for the ones they
  approve. Don't write unbidden.
- **Exact `source_ref` refreshes** (verdict `updated`/`unchanged` for an
  already-known item): apply silently, no confirmation needed — it's just
  keeping a known task's synced fields current, not a new commitment.
- **Manual `add`/`update`/`done`/`remove`** requests from the user: unchanged
  from before, act directly, no confirmation needed (explicit user intent).

## Saving sync settings

If the user wants a specific window/channels remembered, offer to write
`secretary/sync.json` (channels, search, `withinDays`, `autoSyncOnStart`) —
see `plugins/secretary/README.md` for the shape. Not created automatically;
only write it after the user confirms what they want synced.

## Locating the task store

Run commands with `--tasks-root <project-root>` (the directory that
*contains* `secretary/`, not `secretary/` itself — default: cwd). If
`secretary/tasks/` doesn't exist there, the CLI returns an error — tell the
user to run `/create-secretary` first rather than creating the folder
yourself.

## Intent → subcommand

| User says | Subcommand |
|---|---|
| "add a task to ..." | `add --title "..." [--due-date ...] [--priority ...]` |
| "add a subtask to T-0003" | `add --title "..." --parent-id T-0003` |
| "what's on my list" / "show my tasks" | `list --format text` |
| "what's overdue" | `list --overdue --format text` |
| "what's due this week" | `list --due-within 7 --format text` |
| "mark T-0007 done" / "I finished T-0007" | `done T-0007` |
| "remove T-0002" / "delete T-0002" | `remove T-0002` |
| "remove T-0002 and its subtasks" | `remove T-0002 --cascade` |
| "change the due date of T-0007 to ..." | `update T-0007 --due-date ...` |
| "what's due today" / digest | `digest --format text` |
| "catch me up" / "what's new" / "check my messages" | `sync.py` (both sources) → extract → confirm → `upsert` per approved item, **then** `list`/`digest` |
| (internal: reconciling a synced item) | `upsert --source ... --source-ref ... --title ...` |

Example invocation:
```bash
python3 "${SKILL_DIR}/scripts/tasks_cli.py" --tasks-root "${PROJECT_ROOT}" \
  add --title "Finish Q3 roadmap deck" --due-date 2026-08-01 --priority high \
  --done-when "Deck reviewed by design lead and uploaded to Drive"
```

Every mutating subcommand (`add`/`update`/`done`/`remove`/`upsert`) returns
JSON including the task's current fields and `warnings` — report those to
the user rather than silently swallowing them. Commits happen automatically
inside `task_store.py`; don't ask the user whether to commit, and don't
run `git commit` yourself.

## Edge cases

- **`secretary/tasks/` missing**: the CLI errors with a clear message —
  tell the user to run `/create-secretary` first.
- **Ambiguous reference** ("mark the roadmap task done" with several
  matches): run `list --format json`, find candidates by title, and ask
  the user to confirm the id rather than guessing.
- **`done`/`remove` with open subtasks**: the result's `warnings` field
  says so — the action still completes (explicit user intent wins), but
  mention the warning. For `remove`, offer `--cascade` if the user
  actually wants the whole subtree gone.
- **Ready-to-close parent**: `list`/`digest` output marks a parent with
  `⚠ all subtasks done — ready to close?` when every child is
  done/archived — ask the user before marking the parent done yourself.
- **Broken wiki refs**: `list --format text` marks any `refs` entry that
  doesn't resolve to a `wiki/<slug>.md` page with `⚠ broken ref(s)`. This
  is informational only — it never blocks the action.
- **Reopening a done/removed task**: not currently supported by this
  skill. If the user wants an archived task active again, tell them to
  recreate it as a new task rather than resurrecting the old one.
- **A sync source is `not_configured`**: normal, common state (Outlook
  before `connect-outlook`; Slack before any channel is configured). Don't
  treat it as an error or ask the user to fix it unless they specifically
  ask about that source.
- **Sync finds nothing actionable**: fine — say so briefly ("checked Slack,
  nothing new") rather than staying silent, so the user knows the sync ran.
- **A synced message is ambiguous** (might be a new task, might be an
  update to an existing one, might be nothing): default to asking rather
  than guessing — creating a spurious task is cheap to undo but noisy;
  silently merging into the wrong task is worse.

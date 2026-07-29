# secretary

A personal task/todo secretary, backed by a git-committed local file store —
no external task service required.

## Layout

```
secretary/tasks/          -- active tasks (todo / in_progress), one file per task
secretary/archived/       -- done or removed tasks (moved here, never deleted)
secretary/index/index.md  -- maintained listing of every task, active and archived
```

## Skills

- `create-secretary` — turns a project (new or existing) into a secretary
  agent: scaffolds the layout above, creates or merges secretary
  instructions into `CLAUDE.md`, wires up a `SessionStart` hook that
  surfaces overdue/due-soon tasks at the start of every session, and
  ensures the project is git-initialized.
- `tasks` — day-to-day CRUD: add, update, mark done, remove (archive), add
  subtasks, and render the list (flat/tree, filterable). Every mutating
  action commits to git immediately.

Task `refs` reuse the `[[wiki-link]]` syntax from the `llm-wiki` plugin —
if a project also has a `wiki/` directory, a task's refs can point directly
at wiki pages, and `tasks` flags any ref that doesn't resolve.

## Keeping tasks in sync (Slack, Outlook)

The `tasks` skill automatically checks Slack and Outlook for new action
items before answering anything about the list — both sources, every time,
with no separate "check Slack" command. This is implemented as a connector
framework (`scripts/connectors/{base,slack,outlook}.py`) fanned out by
`scripts/sync.py`, which only *gathers* raw material and never writes; the
`tasks` skill extracts candidate todos and writes them via a reconciling
`tasks_cli.py upsert`.

**Reconciliation, not duplication.** Every synced task carries a `sourceRef`
(a stable id for the message/item it came from — Slack: `<channel_id>:<ts>`,
Outlook: a message/event id) and a `sourceHash`. Re-running sync over the
same message updates the existing task in place (or no-ops if nothing
changed) instead of creating a second one; a task the user already
archived is never resurrected. Git remains the change log — sync commits
are tagged `secretary: sync-add <id> from <source>` / `sync-update`.

**Slack**: primary path is the Slack MCP tools already connected in the
agent's session (no token, no extra dependency). Fallback: if the
`llm-wiki` plugin is installed alongside this one *and* its `.wikirc.json`
has a real Slack token, `sync.py --slack-mode fetcher` reuses `llm-wiki`'s
`fetch_slack.py` directly. Default window when unspecified: the last 3
days, all messages (read and unread).

**Outlook**: no OAuth is implemented in this plugin (see "Not included"
below). Investigation found that R-Musubi (a separate macOS app) stores its
own Outlook connection as a plain `{url, token}` pair in
`~/Library/Application Support/R-Musubi/settings.json` — not a private
keychain session — so the `outlook` connector reads that file and reuses it
automatically *if* it's ever filled in there. Until then (or until
`secretary/sync.json` sets `outlook.url`/`outlook.token` directly), Outlook
reports `not_configured` and is skipped quietly.

**Config** (optional, `secretary/sync.json` — not created by bootstrap;
the `tasks` skill offers to write it after a first successful sync):
```json
{
  "autoSyncOnStart": true,
  "withinDays": 3,
  "slack": { "channels": ["general"], "search": null, "mode": "auto" },
  "outlook": { "url": null, "token": null }
}
```
With `autoSyncOnStart: true`, the `SessionStart` hook also tells the agent
to sync at the start of every session, not just when you ask.

## Not included (yet)

Outlook/calendar/mail **OAuth**. Investigated separately (a device-code
flow against the open-source `github.com/desek/outlook-local-mcp`, MIT
licensed, exposing `mail`/`calendar`/`account` MCP tools) but not built
here — a natural candidate for a future `connect-outlook` skill. The
`outlook` connector above is the seam that skill plugs into.

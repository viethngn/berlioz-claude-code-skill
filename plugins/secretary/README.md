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
- `connect-outlook` — one-time setup that installs `outlook-local-mcp`
  directly from upstream and walks through the Microsoft device-code
  sign-in. After this, `tasks` checks Outlook automatically like Slack.

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

**Outlook**: real, via `outlook-local-mcp` (github.com/desek/outlook-local-mcp,
MIT licensed) — a stdio MCP server exposing `mail`/`calendar`/`account`
tools, authenticated with a Microsoft device-code sign-in. Run
`/connect-outlook` once: it installs the binary directly from upstream
(`go install github.com/desek/outlook-local-mcp/cmd/outlook-local-mcp@latest`,
no Azure AD app registration needed) and walks through the one-time sign-in.
This plugin ships its own `.mcp.json`, so the server registers and connects
automatically once the plugin is enabled — see `scripts/outlook_mcp_server.py`.
Until `/connect-outlook` has been run, Outlook reports `not_configured` and
is skipped quietly, same as an unconfigured Slack.

**Trust note**: this grants a real Microsoft Graph token to a small,
unaffiliated third-party open-source binary running locally — a different
trust posture than the fully-hosted Slack/Figma/Atlassian integrations.
Access is **read-only by design** (`OUTLOOK_MCP_READ_ONLY=true`, mail
management disabled, both hardcoded, not user-configurable) — it can read
mail/calendar to derive todos but can't send, delete, or modify anything.
The signed-in token cache lives at `~/.secretary/outlook/accounts.json`
(outside any git working tree); `/connect-outlook` explains this before
doing anything.

**Config** (optional, `secretary/sync.json` — not created by bootstrap;
the `tasks` skill offers to write it after a first successful sync):
```json
{
  "autoSyncOnStart": true,
  "withinDays": 3,
  "slack": { "channels": ["general"], "search": null, "mode": "auto" },
  "outlook": { "enabled": true }
}
```
With `autoSyncOnStart: true`, the `SessionStart` hook also tells the agent
to sync at the start of every session, not just when you ask. `outlook.enabled`
is optional (defaults `true`) — set it `false` to pause Outlook checks
without undoing the sign-in.

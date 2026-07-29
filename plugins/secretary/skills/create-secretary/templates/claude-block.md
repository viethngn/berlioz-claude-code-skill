<!-- BEGIN secretary-agent (managed by /create-secretary; safe to re-run) -->
## {{ title }} — Secretary Agent

### Purpose
This project is managed by the `secretary` plugin: a personal task/todo
tracker. Claude maintains `secretary/`; the human adds tasks, asks for
status, and decides when things are actually done.

### Folder structure
```
secretary/tasks/          -- active tasks (todo / in_progress), one file per task
secretary/archived/       -- done or removed tasks (moved here, never deleted)
secretary/index/index.md  -- maintained listing of every task, active and archived
```

### Task file format
Each task is a `---`-fenced flat key:value block + free-text notes, at
`secretary/tasks/<id>.md` or `secretary/archived/<id>.md`. Fields: `id`,
`title`, `status` (`todo`/`in_progress`/`done`/`archived`), `dueDate`
(`YYYY-MM-DD`), `priority` (`low`/`medium`/`high`), `parentId`, `doneWhen`,
`refs` (comma-separated `[[wiki-link]]`s), `source` (`manual`/`slack`/
`outlook`), `sourceRef` (stable id of the message/item a synced task came
from — the key that prevents duplicates on re-sync), `sourceHash`,
`createdAt`, `updatedAt`. Use the `tasks` skill for all reads/writes — don't
hand-edit these files mid-conversation and expect the index or git history
to stay consistent.

### Staying in sync (Slack now, Outlook once configured)
Before answering anything about the task list, the `tasks` skill
automatically checks **both** Slack and Outlook for new action items — you
never need to ask "check Slack" explicitly. Slack works via the connected
Slack MCP tools (or a local fetcher fallback); Outlook stays
`not_configured` until it has credentials (a future `connect-outlook` skill,
or Outlook's `url`/`token` filled in via R-Musubi's own settings). A source
with nothing configured is skipped quietly, not treated as an error.

Synced items are **reconciled, never blindly duplicated**: each carries a
`sourceRef`, and re-syncing the same message updates its existing task
instead of creating a second one. New tasks from a sync (and any
same-work-item merge into an existing task) are proposed to you before
being written; a plain refresh of an already-known item applies silently.
Every sync-created or sync-updated commit is tagged `sync-add`/`sync-update`
in the git log, so `git log secretary/` is a readable history of what came
from where.

### Subtasks
A task with a `parentId` is a subtask of that task. A parent is never
auto-marked done just because all its children are — the `tasks` skill
flags it as ready-to-close (⚠) and you should ask the user before closing
it.

### Linking to references
`refs` reuses the same `[[page-name]]` syntax as an `llm-wiki` wiki. If this
project also has a `wiki/` directory (from the `llm-wiki` plugin), a task's
refs can point directly at wiki pages, and the `tasks` skill flags any ref
that doesn't resolve to an existing page.

### Rendering
"Show my tasks" / "what's on my list" → the `tasks` skill's `list`
operation, grouped Overdue → Due soon → Later, subtasks indented under
their parent. `secretary/index/index.md` is also always current (rebuilt on
every change) if you just want to read the file directly.

### Due-soon digest
At the start of every session, a hook surfaces overdue tasks and tasks due
within {{ within_days }} days. "Overdue" = due date before today; "due
soon" = due date between today and today+{{ within_days }} days inclusive.
The `tasks` skill's `digest` operation uses the same rule on demand.

### Every action commits automatically
Adding, updating, completing, or removing a task commits to git immediately
— there is no separate "should I commit this?" step, and no batching. Don't
ask the user whether to commit; just report what the `tasks` skill's result
already tells you (it includes the commit).

### Removing tasks
Never delete a task file. Use the `tasks` skill's `remove` operation — it
moves the file to `secretary/archived/` and sets `status: archived`.

### Rules
- Always use the `tasks` skill's CLI for create/update/done/remove — never
  hand-edit frontmatter directly and expect it to stay consistent.
- Ask before marking a parent done just because its children are done.
- Ask before archiving a task that still has open children (or pass
  `--cascade` if the user confirms they want the whole subtree removed).
<!-- END secretary-agent -->

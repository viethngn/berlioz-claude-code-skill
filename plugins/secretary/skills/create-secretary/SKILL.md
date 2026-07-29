---
name: create-secretary
description: |
  Turns the current (or a specified) project into a personal secretary
  agent: scaffolds secretary/tasks/, secretary/archived/, and
  secretary/index/index.md; creates a CLAUDE.md if the project has none, or
  merges secretary instructions into an existing one via a managed,
  idempotent block; merges a SessionStart due-soon/overdue digest hook and
  this marketplace's pin into .claude/settings.json; and ensures the
  project is git-initialized, committing the scaffold immediately. Safe to
  run against an existing project with its own files, CLAUDE.md, and git
  history — never overwrites unrelated content.

  Use this skill whenever the user wants to turn a project into a secretary
  agent, set up task tracking, bootstrap a todo list, or add the secretary
  plugin to a project. Trigger on phrases like: "turn this project into a
  secretary agent", "set up task tracking here", "bootstrap the secretary",
  "add a todo list to this project", "make this my secretary agent".
---

# Create Secretary

Scaffold the `secretary` task store into a project and wire up the
due-soon digest — in one run, safe to re-run.

## Required inputs

Ask upfront if not provided:

| Input | Format |
|-------|--------|
| Target directory | Absolute or `~`-relative path (default: cwd) |
| Title (optional) | Free-form; used in the CLAUDE.md heading (default: target dir's basename) |

Unlike a fresh wiki, the target does **not** need to be empty — this is
meant to run against a project the user already has, alongside whatever
else is in it.

## Workflow

### Phase 1 — Confirm the plan

> I'll turn `<target>` into a secretary agent:
>
> - `secretary/tasks/`, `secretary/archived/`, `secretary/index/index.md`
> - `CLAUDE.md` — created fresh, or a managed section merged into your
>   existing one (never touches the rest of the file)
> - `.claude/settings.json` — merges in the due-soon `SessionStart` hook
>   and this marketplace's pin (other settings untouched)
> - `git init` if the project isn't already a repo, then one commit with
>   just the files above
>
> Continue?

### Phase 2 — Run bootstrap.py

```bash
python3 "${SKILL_DIR}/scripts/bootstrap.py" \
  --target "${TARGET}" \
  --title "${TITLE:-<target-basename>}" \
  --within-days "${WITHIN_DAYS:-3}"
```

Optional flags:
- `--force` — replace the CLAUDE.md secretary block wholesale instead of
  merging (rare — confirm with the user first)
- `--marketplace <path>` — pin a specific marketplace source; defaults to
  auto-detected

`bootstrap.py` prints one JSON summary to stdout: `created` (new dirs),
`claude_md` (`action`: created/updated in place/appended/replaced),
`settings` (`hook_added`: true/false), `git` (`initialized`: true/false),
`commit` (`committed`: true/false, `rev`), `warnings`, `next_steps`.

### Phase 3 — Report the result

Read the JSON and tell the user plainly what happened:
- Which of `secretary/tasks/`, `secretary/archived/`, `secretary/index/`
  were newly created vs. already existed.
- What happened to `CLAUDE.md` (`claude_md.action`).
- Whether the digest hook was newly wired up (`settings.hook_added`) or was
  already there from a previous run.
- Whether git was freshly initialized, and that the scaffold commit
  (`commit.rev`) contains only these files — nothing else in their project
  was touched or committed.

If the plugin isn't installed as a marketplace plugin yet, show the
`/plugin marketplace add <marketplace>` + `/plugin install
secretary@berlioz-claude-code-skill` commands from `next_steps`.

### Phase 4 — First-run guidance

Ask if they want to add their first task right away. If yes, hand off to
the `tasks` skill.

## Edge cases

- **`secretary/tasks/` (or `archived/`/`index/`) already exists**: skipped,
  reported under `created` as absent — existing task files are never read
  or touched during bootstrap.
- **`CLAUDE.md` exists without markers**: the managed block is appended
  after the existing content.
- **`CLAUDE.md` exists with markers already** (re-run): the block is
  replaced in place — same content in, same content out, no duplication.
- **`.claude/settings.json` exists with unrelated hooks or a differently-
  matched `SessionStart` entry**: both preserved; the secretary hook is
  appended as a new array item, never replacing others.
- **The secretary hook is already present** (from a previous run):
  `settings.hook_added` is `false` — not duplicated.
- **Target is already a git repo, possibly with uncommitted changes
  elsewhere**: `git init` is skipped; the scaffold commit stages *only* the
  files this run created/modified, so any unrelated in-progress work is
  left exactly as it was.
- **`git` not on PATH**: scaffolding still completes; `git`/`commit`
  results note it was skipped.

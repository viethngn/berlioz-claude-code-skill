#!/usr/bin/env python3
"""Shared task-store engine for the `secretary` plugin. Stdlib only.

Parses/writes task files, computes the due-soon/overdue digest, renders the
maintained `secretary/index/index.md`, and commits every mutation to git.
Imported by both `scripts/due_soon.py` (the SessionStart hook, read-only)
and `skills/tasks/scripts/tasks_cli.py` (the day-to-day CRUD skill) so the
two can never drift on how a task is parsed or a due date is judged.

Layout under a project root:
    secretary/tasks/<id>.md      -- active tasks (todo / in_progress)
    secretary/archived/<id>.md   -- done or removed tasks (moved here, never deleted)
    secretary/index/index.md     -- maintained listing of every task
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# Same pattern as plugins/llm-wiki/skills/lint/scripts/lint.py's WIKI_LINK_RE.
# Duplicated intentionally -- no cross-plugin import/dependency.
WIKI_LINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")

OPEN_STATUSES = ("todo", "in_progress")
CLOSED_STATUSES = ("done", "archived")
FIELD_ORDER = (
    "id", "title", "status", "dueDate", "priority", "parentId",
    "doneWhen", "refs", "source", "sourceRef", "sourceHash",
    "createdAt", "updatedAt",
)
ID_RE = re.compile(r"^T-(\d+)$")


def tasks_dir(root: Path) -> Path:
    return root / "secretary" / "tasks"


def archived_dir(root: Path) -> Path:
    return root / "secretary" / "archived"


def index_path(root: Path) -> Path:
    return root / "secretary" / "index" / "index.md"


def wiki_root(root: Path) -> Path:
    return root / "wiki"


# ---------------------------------------------------------------------------
# Frontmatter I/O
# ---------------------------------------------------------------------------

def parse_task_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: missing opening '---' frontmatter fence")
    fields: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if line.strip():
            if ":" not in line:
                raise ValueError(f"{path}: malformed frontmatter line: {line!r}")
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        i += 1
    if i >= len(lines):
        raise ValueError(f"{path}: missing closing '---' frontmatter fence")
    body = "\n".join(lines[i + 1:]).strip("\n")
    task = {key: fields.get(key, "") for key in FIELD_ORDER}
    task["body"] = body
    task["_path"] = path
    return task


def _one_line(value: str) -> str:
    """Frontmatter is single-line-per-field by contract. Any newline in a
    value (common for task titles/notes derived from Slack/Outlook message
    text) would otherwise be written as a bare line inside the `---` fence,
    which parse_task_file then rejects -- silently dropping the whole task.
    Collapse CR/LF (and the surrounding whitespace) to a single space here,
    at the one write choke point, so every caller is safe. Multi-line prose
    still belongs in the body, below the fence."""
    return re.sub(r"\s*[\r\n]+\s*", " ", str(value)).strip()


def serialize_task(task: dict) -> str:
    lines = ["---"]
    for key in FIELD_ORDER:
        lines.append(f"{key}: {_one_line(task.get(key) or '')}")
    lines.append("---")
    lines.append("")
    body = (task.get("body") or "").strip("\n")
    if body:
        lines.append(body)
    return "\n".join(lines) + "\n"


def write_task(root: Path, task: dict, archived: bool) -> Path:
    target_dir = archived_dir(root) if archived else tasks_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{task['id']}.md"
    path.write_text(serialize_task(task), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Loading / lookup
# ---------------------------------------------------------------------------

def _load_dir(d: Path) -> list[dict]:
    out = []
    if not d.exists():
        return out
    for p in sorted(d.glob("T-*.md")):
        try:
            out.append(parse_task_file(p))
        except ValueError as e:
            print(f"warning: skipping malformed task file: {e}", file=sys.stderr)
    return out


def load_all_tasks(root: Path, include_archived: bool = False) -> list[dict]:
    tasks = _load_dir(tasks_dir(root))
    if include_archived:
        tasks += _load_dir(archived_dir(root))
    return tasks


def find_task(root: Path, task_id: str) -> Optional[dict]:
    """Locate a task by id in either tasks/ or archived/. Sets `_archived`."""
    for d, is_archived in ((tasks_dir(root), False), (archived_dir(root), True)):
        path = d / f"{task_id}.md"
        if path.exists():
            task = parse_task_file(path)
            task["_archived"] = is_archived
            return task
    return None


def find_by_source_ref(root: Path, source_ref: str) -> Optional[dict]:
    """Locate an existing task by its `sourceRef` (across tasks/ + archived/).

    This is the deterministic anti-duplication key: the same Slack message
    (`<channel_id>:<ts>`) or Outlook item always maps back to the one task it
    created, so a re-sync updates that task rather than spawning a second one.
    Sets `_archived`. Returns None if `source_ref` is empty or unmatched.
    """
    source_ref = (source_ref or "").strip()
    if not source_ref:
        return None
    for d, is_archived in ((tasks_dir(root), False), (archived_dir(root), True)):
        if not d.exists():
            continue
        for p in sorted(d.glob("T-*.md")):
            try:
                task = parse_task_file(p)
            except ValueError:
                continue
            if (task.get("sourceRef") or "").strip() == source_ref:
                task["_archived"] = is_archived
                return task
    return None


def content_hash(*parts: str) -> str:
    """Stable short hash of the source material a task was derived from, so a
    re-sync can tell 'same item, edited text' from 'nothing changed'."""
    joined = "\x1f".join((p or "") for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def next_id(root: Path) -> str:
    max_n = 0
    for d in (tasks_dir(root), archived_dir(root)):
        if not d.exists():
            continue
        for p in d.glob("T-*.md"):
            m = ID_RE.match(p.stem)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"T-{max_n + 1:04d}"


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _is_git_repo(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def commit_change(root: Path, paths: list[Path], message: str) -> Optional[str]:
    """git add <paths> && git commit -m message, scoped to exactly `paths`.

    Never `git add -A` -- an unrelated in-progress change elsewhere in the
    project must never be swept into a secretary commit. Non-fatal: a
    missing `git` binary or a target that isn't a repo just skips the
    commit (warns to stderr) -- the file write already succeeded.
    """
    root = Path(root)
    if not _is_git_repo(root):
        print("warning: not a git repo -- skipping commit", file=sys.stderr)
        return None
    try:
        subprocess.run(
            ["git", "-C", str(root), "add", *[str(p) for p in paths]],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", message],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(
                f"warning: git commit skipped: {result.stdout.strip() or result.stderr.strip()}",
                file=sys.stderr,
            )
            return None
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        return rev.stdout.strip() if rev.returncode == 0 else None
    except FileNotFoundError:
        print("warning: git not found on PATH -- skipping commit", file=sys.stderr)
        return None
    except subprocess.CalledProcessError as e:
        print(f"warning: git add failed: {e.stderr}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Mutations -- every one writes the file(s), regenerates the index, and
# commits. `update_task` is the single place that decides which folder a
# task belongs in, so add/done/archive can't drift from that rule.
# ---------------------------------------------------------------------------

def add_task(
    root: Path,
    *,
    title: str,
    due_date: str = "",
    priority: str = "medium",
    parent_id: str = "",
    done_when: str = "",
    refs: str = "",
    source: str = "manual",
    source_ref: str = "",
    source_hash: str = "",
    body: str = "",
    _commit_message: Optional[str] = None,
) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    task = {
        "id": next_id(root),
        "title": title,
        "status": "todo",
        "dueDate": due_date,
        "priority": priority or "medium",
        "parentId": parent_id,
        "doneWhen": done_when,
        "refs": refs,
        "source": source or "manual",
        "sourceRef": source_ref,
        "sourceHash": source_hash,
        "createdAt": now,
        "updatedAt": now,
        "body": body,
    }
    path = write_task(root, task, archived=False)
    idx_path = render_index_md(root)
    if _commit_message:
        message = _commit_message
    elif source_ref:
        message = f'secretary: sync-add {task["id"]} from {source}'
    else:
        message = f'secretary: add {task["id"]} "{title}"'
    commit_change(root, [path, idx_path], message)
    task["_warnings"] = []
    return task


def update_task(root: Path, task_id: str, _commit_message: Optional[str] = None, **changes) -> dict:
    task = find_task(root, task_id)
    if task is None:
        raise KeyError(f"no such task: {task_id}")
    was_archived = task.pop("_archived", False)
    old_path = task.pop("_path")
    # `None` means "caller didn't supply this field -- leave it alone";
    # an empty string means "clear this field" (e.g. remove a due date).
    # cmd_update passes None for every un-supplied argparse arg, so the two
    # are never conflated.
    for key, value in changes.items():
        if value is not None:
            task[key] = value
    task["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    should_archive = task["status"] in CLOSED_STATUSES
    new_path = write_task(root, task, archived=should_archive)
    if Path(old_path) != new_path and Path(old_path).exists():
        Path(old_path).unlink()
    idx_path = render_index_md(root)
    message = _commit_message or f"secretary: update {task_id}"
    commit_change(root, [new_path, idx_path], message)
    task["_warnings"] = []
    return task


def mark_done(root: Path, task_id: str) -> dict:
    all_tasks = load_all_tasks(root, include_archived=True)
    open_children = [
        t for t in all_tasks
        if t.get("parentId") == task_id and t.get("status") in OPEN_STATUSES
    ]
    task = update_task(root, task_id, _commit_message=f"secretary: mark {task_id} done", status="done")
    if open_children:
        task["_warnings"] = [f"{len(open_children)} subtask(s) still open"]
    return task


def archive_task(
    root: Path,
    task_id: str,
    reason: str = "",
    cascade: bool = False,
    _seen: Optional[set] = None,
) -> dict:
    """`_seen` guards cascade recursion against a cyclic/self-referential
    `parentId` chain (e.g. a task whose parentId points at itself, or a
    parent/child loop) -- without it, such a chain would recurse forever."""
    seen = _seen if _seen is not None else set()
    if task_id in seen:
        task = find_task(root, task_id)
        if task:
            task["_warnings"] = [f"cycle detected in parentId chain at {task_id} -- stopped cascading"]
        return task or {"id": task_id, "_warnings": ["cycle detected -- task not found"]}
    seen.add(task_id)

    all_tasks = load_all_tasks(root, include_archived=True)
    children = [t for t in all_tasks if t.get("parentId") == task_id and t["id"] not in seen]
    open_children = [t for t in children if t.get("status") in OPEN_STATUSES]

    changes = {"status": "archived"}
    if reason:
        current = find_task(root, task_id)
        prior_body = (current.get("body") or "").strip() if current else ""
        changes["body"] = (prior_body + f"\n\n_Removed: {reason}_").strip()
    message = f"secretary: archive {task_id}" + (f" ({reason})" if reason else "")
    task = update_task(root, task_id, _commit_message=message, **changes)

    warnings = []
    if cascade:
        for child in children:
            archive_task(root, child["id"], reason=f"parent {task_id} removed", cascade=True, _seen=seen)
    elif open_children:
        warnings.append(f"{len(open_children)} subtask(s) still open and not archived")
    task["_warnings"] = warnings
    return task


# ---------------------------------------------------------------------------
# Reconciliation -- the single entry point for source-synced tasks (Slack,
# Outlook, ...). Matches an incoming item to its existing task by sourceRef
# and UPDATES it rather than creating a duplicate; only genuinely new items
# become new tasks. This is what keeps "re-sync my Slack" idempotent.
# ---------------------------------------------------------------------------

def upsert_from_source(
    root: Path,
    *,
    source: str,
    source_ref: str,
    source_hash: str = "",
    title: str = "",
    due_date: str = "",
    priority: str = "medium",
    refs: str = "",
    done_when: str = "",
    body: str = "",
    verdict_only: bool = False,
) -> dict:
    """Create-or-update a task derived from an external source.

    Returns the task dict with a `_verdict` of:
      - "created"   -- no prior task for this sourceRef; a new one was written.
      - "updated"   -- matched an ACTIVE task whose sourceHash changed; the
                       existing task (same id/file) was refreshed in place.
      - "unchanged" -- matched an active task, sourceHash identical; no write,
                       no commit.
      - "skipped"   -- matched an ARCHIVED task (user already finished/dismissed
                       it); NOT resurrected. `_warnings` explains.

    With no `source_ref`, falls back to a plain add_task (manual-style items
    never auto-dedup). `verdict_only=True` computes the verdict without
    writing -- used by `sync.py --dry-run` to preview.
    """
    source_ref = (source_ref or "").strip()
    if not source_hash:
        source_hash = content_hash(title, body, due_date)

    if not source_ref:
        if verdict_only:
            return {"_verdict": "created", "sourceRef": ""}
        task = add_task(
            root, title=title, due_date=due_date, priority=priority, refs=refs,
            done_when=done_when, source=source, source_hash=source_hash, body=body,
        )
        task["_verdict"] = "created"
        return task

    existing = find_by_source_ref(root, source_ref)

    if existing is None:
        if verdict_only:
            return {"_verdict": "created", "sourceRef": source_ref}
        task = add_task(
            root, title=title, due_date=due_date, priority=priority, refs=refs,
            done_when=done_when, source=source, source_ref=source_ref,
            source_hash=source_hash, body=body,
        )
        task["_verdict"] = "created"
        return task

    if existing.get("_archived"):
        existing["_verdict"] = "skipped"
        existing["_warnings"] = [
            f"matches archived task {existing['id']} (previously closed/dismissed) -- not resurrected"
        ]
        return existing

    if (existing.get("sourceHash") or "") == source_hash:
        existing["_verdict"] = "unchanged"
        existing["_warnings"] = []
        return existing

    if verdict_only:
        existing["_verdict"] = "updated"
        return existing

    # Refresh only source-DERIVED content. Priority, doneWhen, and any due
    # date the user set locally are left untouched -- a re-sync updates the
    # message text, it doesn't stomp the user's own edits. Body is APPENDED
    # to, never replaced: if the user added their own notes via `update` in
    # the meantime, overwriting `body` outright would silently destroy them
    # (mirrors archive_task's append-not-replace pattern for its `reason`,
    # just above). camelCase keys map straight onto the task dict in
    # update_task; None => "leave alone".
    changes = {"title": title, "sourceHash": source_hash}
    if body:
        prior_body = (existing.get("body") or "").strip()
        changes["body"] = f"{prior_body}\n\n_Synced update from {source}:_ {body}".strip() if prior_body else body
    if due_date:
        changes["dueDate"] = due_date
    if refs:
        changes["refs"] = refs
    task = update_task(
        root,
        existing["id"],
        _commit_message=f'secretary: sync-update {existing["id"]} from {source}',
        **changes,
    )
    task["_verdict"] = "updated"
    return task


# ---------------------------------------------------------------------------
# Subtasks / references
# ---------------------------------------------------------------------------

def build_tree(tasks: list[dict]) -> list[dict]:
    by_id = {t["id"]: dict(t, children=[]) for t in tasks}
    roots = []
    for t in by_id.values():
        parent_id = t.get("parentId")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(t)
        else:
            roots.append(t)
    return roots


def children_all_done(task_id: str, tasks: list[dict]) -> bool:
    children = [t for t in tasks if t.get("parentId") == task_id]
    if not children:
        return False
    return all(t.get("status") in CLOSED_STATUSES for t in children)


def verify_refs(task: dict, wiki_dir: Path) -> list[str]:
    """Refs whose slug doesn't resolve to a wiki page. [] if no wiki/ dir."""
    if not wiki_dir.exists():
        return []
    broken = []
    for ref in WIKI_LINK_RE.findall(task.get("refs") or ""):
        slug = ref.strip()
        if not (wiki_dir / f"{slug}.md").exists() and not (wiki_dir / "archive" / f"{slug}.md").exists():
            broken.append(slug)
    return broken


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def compute_digest(tasks: list[dict], within_days: int = 3, today: Optional[date] = None) -> dict:
    """overdue: dueDate < today. due_soon: today <= dueDate <= today+within_days.

    Only todo/in_progress tasks with a non-empty dueDate are considered.
    """
    today = today or date.today()
    cutoff = today + timedelta(days=within_days)
    overdue, due_soon = [], []
    for t in tasks:
        if t.get("status") not in OPEN_STATUSES:
            continue
        due = _parse_date(t.get("dueDate"))
        if due is None:
            continue
        if due < today:
            overdue.append(t)
        elif due <= cutoff:
            due_soon.append(t)
    overdue.sort(key=lambda t: _parse_date(t["dueDate"]))
    due_soon.sort(key=lambda t: _parse_date(t["dueDate"]))
    return {"overdue": overdue, "due_soon": due_soon}


def format_digest_text(digest: dict, cap: int = 10) -> str:
    overdue, due_soon = digest["overdue"], digest["due_soon"]
    if not overdue and not due_soon:
        return "Nothing overdue or due soon."

    def section(label: str, items: list[dict], ask: str) -> list[str]:
        lines = ["", f"{label}:"]
        for t in items[:cap]:
            lines.append(f"- {t['id']} {t['title']} (due {t['dueDate']})")
        if len(items) > cap:
            lines.append(f"- +{len(items) - cap} more — ask me to {ask}")
        return lines

    parts = []
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if due_soon:
        parts.append(f"{len(due_soon)} due soon")
    lines = [f"**Secretary — {', '.join(parts)}**"]
    if overdue:
        lines += section("Overdue", overdue, "list overdue tasks")
    if due_soon:
        lines += section("Due soon", due_soon, "list due-soon tasks")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _format_line(task: dict, tasks: list[dict], wiki_dir: Optional[Path] = None) -> str:
    due = f" (due {task['dueDate']})" if task.get("dueDate") else ""
    marker = ""
    if task.get("status") in OPEN_STATUSES and children_all_done(task["id"], tasks):
        marker = " ⚠ all subtasks done — ready to close?"
    if wiki_dir is not None:
        broken = verify_refs(task, wiki_dir)
        if broken:
            marker += f" ⚠ broken ref(s): {', '.join(broken)}"
    return f"- {task['id']} [{task.get('status')}] {task['title']}{due}{marker}"


def render_tree_text(
    tasks: list[dict],
    within_days: int = 3,
    title: str = "Tasks",
    wiki_dir: Optional[Path] = None,
    all_tasks: Optional[list[dict]] = None,
) -> str:
    """Render `tasks` (the rows to display) grouped Overdue/Due soon/Later.

    Renders exactly the rows passed in -- the caller (cmd_list) decides which
    statuses to include, so `list --status done` can show closed tasks here.
    `all_tasks` (defaults to `tasks`) is used only for `children_all_done`
    lookups -- pass the full set (including archived) so a parent whose
    only children have already been completed/removed still gets flagged
    ready-to-close, even though the display set may only list active rows.
    """
    today = date.today()
    cutoff = today + timedelta(days=within_days)
    lookup_tasks = all_tasks if all_tasks is not None else tasks
    display = list(tasks)
    display_ids = {x["id"] for x in display}
    top_level = [t for t in display if not t.get("parentId") or t.get("parentId") not in display_ids]

    def bucket(t: dict) -> int:
        due = _parse_date(t.get("dueDate"))
        if due is None:
            return 2
        if due < today:
            return 0
        if due <= cutoff:
            return 1
        return 2

    groups: dict[int, list[dict]] = {0: [], 1: [], 2: []}
    for t in top_level:
        groups[bucket(t)].append(t)
    for g in groups.values():
        g.sort(key=lambda t: t.get("dueDate") or "9999-99-99")

    lines = [f"# {title}"]
    for label, key in (("Overdue", 0), ("Due soon", 1), ("Later / no due date", 2)):
        lines.append("")
        lines.append(f"## {label}")
        group = groups[key]
        if not group:
            lines.append("_none_")
            continue
        for t in group:
            lines.append(_format_line(t, lookup_tasks, wiki_dir))
            for child in display:
                if child.get("parentId") == t["id"]:
                    lines.append("  " + _format_line(child, lookup_tasks, wiki_dir))
    return "\n".join(lines)


def render_index_md(root: Path) -> Path:
    """Regenerate secretary/index/index.md from current on-disk state.

    Called by every mutating function right before its commit, so the
    index is always exactly as current as the task files it lists.
    """
    tasks = load_all_tasks(root, include_archived=True)
    active = [t for t in tasks if t.get("status") in OPEN_STATUSES]
    done = [t for t in tasks if t.get("status") == "done"]
    removed = [t for t in tasks if t.get("status") == "archived"]

    def entry(task: dict) -> str:
        folder = "archived" if task["status"] in CLOSED_STATUSES else "tasks"
        due = f" — due {task['dueDate']}" if task.get("dueDate") else ""
        parent = f" (subtask of {task['parentId']})" if task.get("parentId") else ""
        marker = ""
        if task["status"] in OPEN_STATUSES and children_all_done(task["id"], tasks):
            marker = " ⚠ ready to close"
        return f"- [{task['id']}]({folder}/{task['id']}.md) {task['title']} [{task['status']}]{due}{parent}{marker}"

    lines = ["# Tasks", "", f"_Last updated: {datetime.now().isoformat(timespec='seconds')}_", ""]
    lines.append(f"## Active ({len(active)})")
    if active:
        for t in sorted(active, key=lambda t: t.get("dueDate") or "9999-99-99"):
            lines.append(entry(t))
    else:
        lines.append("_No tasks yet._")
    lines.append("")
    lines.append(f"## Done ({len(done)})")
    if done:
        for t in sorted(done, key=lambda t: t.get("updatedAt") or "", reverse=True):
            lines.append(entry(t))
    else:
        lines.append("_None._")
    lines.append("")
    lines.append(f"## Removed ({len(removed)})")
    if removed:
        for t in sorted(removed, key=lambda t: t.get("updatedAt") or "", reverse=True):
            lines.append(entry(t))
    else:
        lines.append("_None._")

    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

#!/usr/bin/env python3
"""Turn an existing project into a secretary agent.

Unlike llm-wiki's create-wiki (which bootstraps a brand-new, normally-empty
wiki repo), this scaffolds INTO a project that may already have its own
CLAUDE.md, .claude/settings.json, and git history -- so every step here is
additive/merging, never a blanket overwrite:

    - secretary/tasks/, secretary/archived/, secretary/index/index.md are
      created only if missing; existing task files are never touched.
    - CLAUDE.md: created fresh if absent; otherwise a managed block is
      inserted (or updated in place on re-run) via BEGIN/END markers,
      never clobbering the rest of the file.
    - .claude/settings.json: the `marketplaces` list and the `SessionStart`
      hook are merged in; other keys/hooks are left exactly as they were.
    - git: initialized automatically if the target isn't already a repo;
      either way, exactly one scaffold commit is made, containing ONLY the
      files this run created or modified (never `git add -A`), so any
      unrelated in-progress work in the target project is left untouched.

This script is intentionally self-contained (does not import
scripts/task_store.py) -- the one-time scaffold stays decoupled from the
runtime engine's schema, same as create-wiki's bootstrap.py never imports
lint.py.

Usage:
    python3 bootstrap.py --target /path/to/project [--title "My Project"]
    python3 bootstrap.py --target ... --force              # replace CLAUDE.md block wholesale
    python3 bootstrap.py --target ... --within-days 5
    python3 bootstrap.py --target ... --marketplace /path/to/berlioz-claude-code-skill
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates"

BEGIN_MARKER_PREFIX = "<!-- BEGIN secretary-agent"
END_MARKER = "<!-- END secretary-agent -->"


def resolve_marketplace(explicit: Optional[Path]) -> Optional[Path]:
    if explicit is not None:
        return explicit.resolve()
    current = SCRIPT_DIR
    for _ in range(8):
        candidate = current / ".claude-plugin" / "marketplace.json"
        if candidate.exists():
            return current.resolve()
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def render_template(template_path: Path, context: dict) -> str:
    text = template_path.read_text(encoding="utf-8")
    for key, value in context.items():
        text = text.replace("{{ " + key + " }}", str(value))
    return text


def write_file(dest: Path, content: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


def is_git_repo(path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, text=True)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Folder scaffold
# ---------------------------------------------------------------------------

def ensure_task_dirs(target: Path) -> list[str]:
    """Create secretary/{tasks,archived,index/index.md}, skipping anything
    that already exists. Returns the list of paths actually created."""
    created: list[str] = []

    for rel in ("secretary/tasks", "secretary/archived"):
        d = target / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(rel + "/")

    index_dir = target / "secretary" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_md = index_dir / "index.md"
    if not index_md.exists():
        write_file(index_md, "# Tasks\n\n_No tasks yet._\n")
        created.append("secretary/index/index.md")

    return created


# ---------------------------------------------------------------------------
# CLAUDE.md merge
# ---------------------------------------------------------------------------

def merge_claude_md(target: Path, rendered_block: str, force: bool) -> dict:
    """Insert or update the managed CLAUDE.md block via BEGIN/END markers.

    `force` only ever affects HOW the block gets updated when markers already
    exist — never whether the rest of the file survives. Replacing the whole
    file (the previous behavior) silently destroyed any of the user's own
    CLAUDE.md content outside the markers, contradicting this module's own
    "never clobbering the rest of the file" guarantee.
    """
    claude_md = target / "CLAUDE.md"
    block = rendered_block.strip("\n")

    if not claude_md.exists():
        write_file(claude_md, block + "\n")
        return {"path": "CLAUDE.md", "action": "created"}

    existing = claude_md.read_text(encoding="utf-8")

    start = existing.find(BEGIN_MARKER_PREFIX)
    end = existing.find(END_MARKER)
    if start != -1 and end != -1:
        end_full = end + len(END_MARKER)
        new_content = existing[:start] + block + existing[end_full:]
        write_file(claude_md, new_content)
        action = "updated in place (--force)" if force else "updated in place"
        return {"path": "CLAUDE.md", "action": action}

    sep = "" if existing.endswith("\n\n") else ("\n\n" if existing.endswith("\n") else "\n\n")
    write_file(claude_md, existing + sep + block + "\n")
    action = "appended (--force, no existing markers found)" if force else "appended"
    return {"path": "CLAUDE.md", "action": action}


# ---------------------------------------------------------------------------
# .claude/settings.json merge
# ---------------------------------------------------------------------------

def merge_marketplace(existing: dict, marketplace: Optional[Path]) -> None:
    source = str(marketplace) if marketplace else "REPLACE_ME_ABSOLUTE_PATH_TO_berlioz-claude-code-skill"
    marketplaces = existing.get("marketplaces") or []
    if not any(isinstance(m, dict) and m.get("source") == source for m in marketplaces):
        marketplaces.append({"source": source})
    existing["marketplaces"] = marketplaces


def merge_session_start_hook(existing: dict, within_days: int) -> bool:
    """Append the due_soon.py SessionStart hook unless already present.
    Returns True if it was added."""
    hooks = existing.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    for entry in session_start:
        for h in entry.get("hooks", []):
            if any("due_soon.py" in str(a) for a in h.get("args", [])):
                return False  # already wired up
    session_start.append({
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "command": "python3",
                "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/due_soon.py", "--within-days", str(within_days)],
                "timeout": 10,
                "statusMessage": "Loading tasks due today...",
            }
        ],
    })
    return True


def merge_settings(target: Path, marketplace: Optional[Path], within_days: int) -> dict:
    settings_path = target / ".claude" / "settings.json"
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # Resetting to {} here would silently discard every other key
            # (hooks, permissions, env) in a hand-edited but slightly
            # malformed file. Leave it untouched instead.
            return {
                "path": ".claude/settings.json",
                "hook_added": False,
                "note": (
                    f"existing .claude/settings.json is not valid JSON ({e}) — "
                    "left untouched. Add the marketplace entry and SessionStart "
                    "hook manually."
                ),
            }

    merge_marketplace(existing, marketplace)
    hook_added = merge_session_start_hook(existing, within_days)

    write_file(settings_path, json.dumps(existing, indent=2) + "\n")
    return {
        "path": ".claude/settings.json",
        "hook_added": hook_added,
        "note": "hook already present" if not hook_added else "hook added",
    }


# ---------------------------------------------------------------------------
# Git: always ensure a repo, scoped scaffold commit
# ---------------------------------------------------------------------------

def ensure_git(target: Path) -> dict:
    if not git_available():
        return {"initialized": False, "note": "git not on PATH -- skipping git entirely"}
    if is_git_repo(target):
        return {"initialized": False, "note": "target is already a git repo"}
    subprocess.run(["git", "-C", str(target), "init", "-q"], check=True)
    return {"initialized": True, "note": "git init"}


def scaffold_commit(target: Path, touched_paths: list[Path]) -> dict:
    if not touched_paths:
        return {"committed": False, "note": "nothing new to commit"}
    if not git_available() or not is_git_repo(target):
        return {"committed": False, "note": "no git repo available -- skipped"}
    rel_paths = [str(p) for p in touched_paths]
    subprocess.run(["git", "-C", str(target), "add", *rel_paths], check=True)
    result = subprocess.run(
        ["git", "-C", str(target), "commit", "-q", "-m", "chore: bootstrap secretary agent"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"committed": False, "note": result.stdout.strip() or result.stderr.strip()}
    rev = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    return {"committed": True, "note": "scaffold commit created", "rev": rev.stdout.strip()}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _bootstrap(target: Path, title: str, marketplace: Optional[Path], force: bool, within_days: int) -> dict:
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []

    dir_created = ensure_task_dirs(target)

    context = {"title": title, "within_days": within_days}
    block_tpl = TEMPLATE_DIR / "claude-block.md"
    if not block_tpl.exists():
        warnings.append(f"missing template: {block_tpl}")
        claude_result = {"path": "CLAUDE.md", "action": "skipped (template missing)"}
    else:
        rendered_block = render_template(block_tpl, context)
        claude_result = merge_claude_md(target, rendered_block, force)

    settings_result = merge_settings(target, marketplace, within_days)

    git_result = ensure_git(target)

    # Scoped commit: only the exact paths this run created/modified -- never
    # the whole `secretary/` tree, so a pre-existing uncommitted task file
    # (e.g. one the user hand-created before ever running this) isn't swept
    # into a "bootstrap" commit it had nothing to do with.
    touched: list[Path] = [target / rel for rel in dir_created]
    if claude_result["action"] != "skipped (template missing)":
        touched.append(target / "CLAUDE.md")
    touched.append(target / ".claude" / "settings.json")
    commit_result = scaffold_commit(target, touched)

    marketplace_str = str(marketplace) if marketplace else "REPLACE_ME"
    next_steps = [
        f"cd {target}",
        "Review `git log -1 -p` to see exactly what was scaffolded/merged.",
        f"If the plugin is not installed yet: /plugin marketplace add {marketplace_str} "
        "then /plugin install secretary@berlioz-claude-code-skill",
        "Try: add a task, then ask \"what's on my list?\"",
    ]

    return {
        "target": str(target),
        "title": title,
        "marketplace": str(marketplace) if marketplace else None,
        "created": dir_created,
        "claude_md": claude_result,
        "settings": settings_result,
        "git": git_result,
        "commit": commit_result,
        "warnings": warnings,
        "next_steps": next_steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Turn a project into a secretary agent")
    parser.add_argument("--target", type=Path, default=Path.cwd())
    parser.add_argument("--title", default=None)
    parser.add_argument("--marketplace", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="replace CLAUDE.md's managed block wholesale instead of merging")
    parser.add_argument("--within-days", type=int, default=3)
    args = parser.parse_args()

    title = args.title or Path(args.target).expanduser().resolve().name or "My Project"
    marketplace = resolve_marketplace(args.marketplace)
    summary = _bootstrap(
        target=args.target,
        title=title,
        marketplace=marketplace,
        force=args.force,
        within_days=args.within_days,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

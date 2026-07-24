#!/usr/bin/env python3
"""Bootstrap a new LLM wiki: folder layout, templates, git init, marketplace pinning.

Also performs first-time setup end-to-end so `/create-wiki` "just works":
creates a ready-to-edit `.wikirc.json` from the example, then checks the plugin's
Python dependencies via `check-setup.sh` and installs them via `install.sh` only
if something is missing (idempotent). Both scripts are resolved relative to this
file (`__file__`-relative plugin root), so this works on any machine / install
path with no hardcoded paths.

The Python side is stdlib only; it shells out to the plugin's bash setup scripts
(same as it already shells out to `git`).

Usage:
    python3 bootstrap.py --target /path/to/new/wiki [--title "My Wiki"]
    python3 bootstrap.py --target ... --force        # overwrite non-empty
    python3 bootstrap.py --target ... --no-git       # skip git init
    python3 bootstrap.py --target ... --skip-deps     # skip dep install/verify (CI/air-gapped)
    python3 bootstrap.py --target ... --marketplace /path/to/berlioz-claude-code-skill
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates"


TEMPLATE_MAP: dict[str, str] = {
    "CLAUDE.md": "CLAUDE.md",
    "index.md": "wiki/index.md",
    "log.md": "wiki/log.md",
    "page.md": "templates/page.md",
    "wikirc.example.json": ".wikirc.example.json",
    "gitignore": ".gitignore",
    "claude-settings.json": ".claude/settings.json",
}


def resolve_marketplace(explicit: Optional[Path]) -> Optional[Path]:
    if explicit is not None:
        return explicit.resolve()
    # Walk up from this script's __file__ looking for .claude-plugin/marketplace.json
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


def target_is_empty(target: Path) -> bool:
    if not target.exists():
        return True
    if not target.is_dir():
        return False
    return not any(target.iterdir())


def render_template(template_path: Path, context: dict) -> str:
    text = template_path.read_text(encoding="utf-8")
    for key, value in context.items():
        text = text.replace("{{ " + key + " }}", str(value))
    return text


def merge_claude_settings(existing: dict, marketplace: Optional[Path]) -> dict:
    if marketplace is None:
        source = "REPLACE_ME_ABSOLUTE_PATH_TO_berlioz-claude-code-skill"
    else:
        source = str(marketplace)

    marketplaces = existing.get("marketplaces") or []
    if not any(
        (isinstance(m, dict) and m.get("source") == source) for m in marketplaces
    ):
        marketplaces.append({"source": source})
    existing["marketplaces"] = marketplaces
    return existing


def write_file(dest: Path, content: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


def is_git_repo(path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def plugin_root() -> Path:
    """Installed plugin root (contains install.sh / check-setup.sh).

    Resolved relative to this file so it holds on any machine / install path:
    scripts/ -> create-wiki/ -> skills/ -> <plugin root>.
    """
    return SCRIPT_DIR.parents[2]


def _run_bash(script: Path, *args: str) -> int:
    """Run a bash script, piping its stdout to OUR stderr so `bootstrap.py`'s
    stdout stays a clean JSON summary. Returns the exit code."""
    proc = subprocess.run(
        ["bash", str(script), *args],
        stdout=sys.stderr,  # live progress visible, but off our stdout
        stderr=sys.stderr,
    )
    return proc.returncode


def _run_deps_setup() -> dict:
    """Check dependencies via check-setup.sh; if something is missing, install
    via install.sh (which verifies them itself at the end). Non-fatal: a failure
    here never fails the bootstrap (the wiki is already scaffolded).

    Returns a status dict: {ran, installed, ok, note}.
    """
    root = plugin_root()
    check_sh = root / "check-setup.sh"
    install_sh = root / "install.sh"
    setup_md = "skills/ingest/references/setup.md"

    if shutil.which("bash") is None:
        return {
            "ran": False,
            "installed": False,
            "ok": False,
            "note": (
                "bash not on PATH — skipped automatic dependency setup. "
                f"Install deps manually: bash {install_sh}"
            ),
        }
    if not check_sh.exists() or not install_sh.exists():
        return {
            "ran": False,
            "installed": False,
            "ok": False,
            "note": (
                f"setup scripts not found next to bootstrap.py (looked in {root}) — "
                "skipped automatic dependency setup"
            ),
        }

    print("==> Checking Python dependencies (check-setup.sh)", file=sys.stderr)
    if _run_bash(check_sh) == 0:
        return {
            "ran": True,
            "installed": False,
            "ok": True,
            "note": "dependencies already present",
        }

    print(
        "==> Missing dependencies — installing (install.sh)",
        file=sys.stderr,
    )
    # install.sh runs check-setup.sh itself at the end (under `set -e`), using
    # the exact interpreter it installed into — a venv when it falls back to
    # one. So its exit code already means "installed AND verified." Trust it
    # rather than re-running check-setup.sh here with our own environment, which
    # could resolve a different interpreter (e.g. an explicit PYTHON= pointing
    # at a deps-less system python) and report a false failure.
    install_ok = _run_bash(install_sh) == 0

    if install_ok:
        return {
            "ran": True,
            "installed": True,
            "ok": True,
            "note": "dependencies installed and verified",
        }

    return {
        "ran": True,
        "installed": True,
        "ok": False,
        "note": (
            "dependency install did not fully succeed (e.g. Python 3.10+ not "
            "available, or no PyPI access). The wiki is scaffolded; fix the "
            f"environment then re-run: bash {install_sh}. "
            f"For offline/mirrored networks see {setup_md}."
        ),
    }


def _bootstrap(
    target: Path,
    title: str,
    marketplace: Optional[Path],
    force: bool,
    no_git: bool,
    skip_deps: bool,
) -> dict:
    target = target.expanduser().resolve()

    if target.exists() and not target_is_empty(target) and not force:
        raise SystemExit(
            f"ERROR: target {target} is not empty. "
            "Pass --force to bootstrap on top of existing files."
        )

    target.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    # Ensure top-level directories. `wiki/archive/` is the archival namespace:
    # /lint moves retired pages there (they leave active lint scope but stay
    # linkable). Seed a .gitkeep so the empty dir is tracked from the start.
    for d in ("raw", "wiki", "wiki/archive", "templates", ".claude"):
        (target / d).mkdir(parents=True, exist_ok=True)
        created.append(d + "/")
    gitkeep = target / "wiki" / "archive" / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")

    context = {
        "title": title,
        "marketplace_path": str(marketplace) if marketplace else "REPLACE_ME",
    }

    # Write templates
    for template_name, dest_rel in TEMPLATE_MAP.items():
        tpl = TEMPLATE_DIR / template_name
        if not tpl.exists():
            warnings.append(f"missing template: {tpl}")
            continue
        dest = target / dest_rel

        if template_name == "claude-settings.json":
            existing: dict = {}
            if dest.exists():
                try:
                    with dest.open("r", encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    existing = {}
            merged = merge_claude_settings(existing, marketplace)
            write_file(dest, json.dumps(merged, indent=2) + "\n")
            created.append(dest_rel)
            continue

        rendered = render_template(tpl, context)

        if dest.exists() and not force:
            skipped.append(dest_rel)
            continue

        write_file(dest, rendered)
        created.append(dest_rel)

    # Auto-create a ready-to-edit .wikirc.json from the example (placeholders
    # intact). It is git-ignored by the template .gitignore, so it never lands
    # in the initial commit. Never clobber an existing one (may hold real creds).
    wikirc = target / ".wikirc.json"
    example_tpl = TEMPLATE_DIR / "wikirc.example.json"
    config_status: dict = {}
    if wikirc.exists():
        config_status = {
            "created": False,
            "path": str(wikirc),
            "note": ".wikirc.json already exists — left untouched",
        }
    elif not example_tpl.exists():
        config_status = {
            "created": False,
            "path": str(wikirc),
            "note": "missing wikirc.example.json template — could not create .wikirc.json",
        }
    else:
        write_file(wikirc, render_template(example_tpl, context))
        config_status = {
            "created": True,
            "path": str(wikirc),
            "note": "created from example (placeholders) — fill in the integrations you want",
        }

    # Git init if requested
    git_status: dict = {}
    if not no_git:
        if is_git_repo(target):
            git_status = {"initialized": False, "note": "target is already a git repo"}
        elif shutil.which("git") is None:
            warnings.append("git not on PATH — skipping git init")
            git_status = {"initialized": False, "note": "git not on PATH"}
        else:
            subprocess.run(["git", "-C", str(target), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-q", "-m", "chore: bootstrap llm-wiki"],
                check=False,
            )
            git_status = {"initialized": True, "note": "initial commit created"}
    else:
        git_status = {"initialized": False, "note": "--no-git passed"}

    # Automatic dependency setup (check → install-if-missing → re-verify).
    # Non-fatal: the wiki is already scaffolded regardless of the outcome.
    if skip_deps:
        deps_status = {
            "ran": False,
            "installed": False,
            "ok": None,
            "note": "--skip-deps passed",
        }
    else:
        deps_status = _run_deps_setup()

    marketplace_str = str(marketplace) if marketplace else "REPLACE_ME"

    next_steps = [
        f"cd {target}",
        "Edit .wikirc.json — fill in the integrations you want (Confluence/Jira "
        "URLs + PATs, nano-banana vision endpoint + key, Slack token). Each is "
        "optional; leaving one empty just skips that source type.",
        f"If the plugin is not installed yet: /plugin marketplace add {marketplace_str} "
        "then /plugin install llm-wiki@berlioz-claude-code-skill",
        "Try: /ingest <URL-or-file>",
    ]

    return {
        "target": str(target),
        "title": title,
        "marketplace": str(marketplace) if marketplace else None,
        "python_version": python_version(),
        "git": git_status,
        "config": config_status,
        "deps": deps_status,
        "created": sorted(set(created)),
        "skipped": sorted(set(skipped)),
        "warnings": warnings,
        "next_steps": next_steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a new LLM wiki")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--title", default="My LLM Wiki")
    parser.add_argument("--marketplace", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="skip automatic dependency install/verify (CI / air-gapped)",
    )
    args = parser.parse_args()

    marketplace = resolve_marketplace(args.marketplace)
    summary = _bootstrap(
        target=args.target,
        title=args.title,
        marketplace=marketplace,
        force=args.force,
        no_git=args.no_git,
        skip_deps=args.skip_deps,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

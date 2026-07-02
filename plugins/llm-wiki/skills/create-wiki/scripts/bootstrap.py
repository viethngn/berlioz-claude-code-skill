#!/usr/bin/env python3
"""Bootstrap a new LLM wiki: folder layout, templates, git init, marketplace pinning.

Stdlib only.

Usage:
    python3 bootstrap.py --target /path/to/new/wiki [--title "My Wiki"]
    python3 bootstrap.py --target ... --force        # overwrite non-empty
    python3 bootstrap.py --target ... --no-git       # skip git init
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


def uv_available() -> bool:
    return shutil.which("uv") is not None


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _bootstrap(
    target: Path,
    title: str,
    marketplace: Optional[Path],
    force: bool,
    no_git: bool,
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

    # Ensure top-level directories
    for d in ("raw", "wiki", "templates", ".claude"):
        (target / d).mkdir(parents=True, exist_ok=True)
        created.append(d + "/")

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

    # Check uv availability (informational only)
    warnings_uv = []
    if not uv_available():
        warnings_uv.append(
            "uv not on PATH — install.sh will fall back to pip. "
            "Optional: install uv from https://astral.sh/uv"
        )

    marketplace_str = str(marketplace) if marketplace else "REPLACE_ME"

    next_steps = [
        f"cd {target}",
        "cp .wikirc.example.json .wikirc.json",
        "Edit .wikirc.json with your Confluence, Jira, and nano-banana-pro endpoints and PATs.",
        f"bash {marketplace_str}/plugins/llm-wiki/install.sh  (one-time, if not done yet)",
        f"bash {marketplace_str}/plugins/llm-wiki/check-setup.sh {target}/.wikirc.json",
        f"/plugin marketplace add {marketplace_str}",
        "/plugin install llm-wiki@berlioz-claude-code-skill",
        "Try: /ingest <URL-or-file>",
    ]

    return {
        "target": str(target),
        "title": title,
        "marketplace": str(marketplace) if marketplace else None,
        "python_version": python_version(),
        "git": git_status,
        "created": sorted(set(created)),
        "skipped": sorted(set(skipped)),
        "warnings": warnings + warnings_uv,
        "next_steps": next_steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a new LLM wiki")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--title", default="My LLM Wiki")
    parser.add_argument("--marketplace", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    args = parser.parse_args()

    marketplace = resolve_marketplace(args.marketplace)
    summary = _bootstrap(
        target=args.target,
        title=args.title,
        marketplace=marketplace,
        force=args.force,
        no_git=args.no_git,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

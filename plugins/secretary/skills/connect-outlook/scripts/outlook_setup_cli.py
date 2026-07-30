#!/usr/bin/env python3
"""One-time Outlook setup CLI for the `connect-outlook` skill. Stdlib only.

Wraps ../../../scripts/outlook_setup.py the same way tasks_cli.py wraps
task_store.py -- shared logic lives in one module, this is just the thin
argparse layer. The interactive device-code sign-in itself is NOT here (it
requires calling a live MCP tool, which only the agent in chat can do --
see SKILL.md); this CLI only handles the scriptable parts: checking status,
installing the Go binary, and recording/clearing the completion marker.

Usage:
    python3 outlook_setup_cli.py status
    python3 outlook_setup_cli.py install-go
    python3 outlook_setup_cli.py mark-complete [--verified-via "account.status"]
    python3 outlook_setup_cli.py clear
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import outlook_setup  # noqa: E402

GO_INSTALL_TARGET = "github.com/desek/outlook-local-mcp/cmd/outlook-local-mcp@latest"


def cmd_status(_args) -> dict:
    return outlook_setup.status()


def cmd_install_go(_args) -> dict:
    if shutil.which("go") is None:
        return {
            "installed": False,
            "error": "go is not on PATH -- install it first (https://go.dev/dl/ or "
                     "`brew install go`), then re-run install-go.",
        }
    proc = subprocess.run(
        ["go", "install", GO_INSTALL_TARGET],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {
            "installed": False,
            "error": proc.stderr.strip() or proc.stdout.strip() or "go install failed",
        }
    found = outlook_setup.find_binary()
    return {
        "installed": found["path"] is not None,
        "binary_path": str(found["path"]) if found["path"] else None,
        "stdout": proc.stdout.strip(),
    }


def cmd_mark_complete(args) -> dict:
    path = outlook_setup.write_marker(
        completed_at=datetime.now().isoformat(timespec="seconds"),
        verified_via=args.verified_via or "manual",
    )
    return {"marked": True, "marker_path": str(path)}


def cmd_clear(_args) -> dict:
    removed = outlook_setup.clear_marker()
    return {"cleared": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time Outlook setup CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("install-go").set_defaults(func=cmd_install_go)

    p_mark = sub.add_parser("mark-complete")
    p_mark.add_argument("--verified-via", default=None)
    p_mark.set_defaults(func=cmd_mark_complete)

    sub.add_parser("clear").set_defaults(func=cmd_clear)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

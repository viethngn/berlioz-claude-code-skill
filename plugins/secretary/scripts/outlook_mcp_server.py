#!/usr/bin/env python3
"""`.mcp.json` launch target for the `outlook` stdio MCP server.

Deliberately dumb: at launch time this only resolves the already-installed
`outlook-local-mcp` binary (see outlook_setup.py for the exact order --
env override, PATH, the documented `go install` default) and `execve`s
straight into it. No discovery-time network calls, no installation logic,
no R-Musubi anywhere -- that all lives in the one-time
`skills/connect-outlook` setup skill. This script's only job is to be fast
and correct every time Claude Code spawns the server.

`os.execve` (not subprocess.run/Popen) is deliberate: it replaces this
process image in place, so the real binary inherits stdin/stdout directly
with nothing buffering or interfering with the MCP JSON-RPC framing.

Usage:
    python3 outlook_mcp_server.py            # normal MCP launch (never returns on success)
    python3 outlook_mcp_server.py --check    # report discovery status as JSON, no MCP handshake
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import outlook_setup  # noqa: E402


def main() -> int:
    if "--check" in sys.argv[1:]:
        info = outlook_setup.status()
        print(json.dumps(info, indent=2))
        return 0 if info["binary_found"] else 1

    found = outlook_setup.find_binary()
    binary_path = found["path"]
    if binary_path is None:
        print(
            "outlook-local-mcp not found (checked $OUTLOOK_MCP_BIN, PATH, ~/go/bin). "
            "Run /connect-outlook to install it.",
            file=sys.stderr,
        )
        return 1

    outlook_setup.ensure_accounts_dir()
    env = os.environ.copy()
    env.update(outlook_setup.security_env_overrides())

    os.execve(str(binary_path), [str(binary_path)], env)
    return 1  # unreachable if execve succeeds


if __name__ == "__main__":
    sys.exit(main())

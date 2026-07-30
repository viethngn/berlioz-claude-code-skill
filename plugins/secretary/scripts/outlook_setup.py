#!/usr/bin/env python3
"""Shared stdlib helpers for the Outlook MCP integration.

Imported by both `outlook_mcp_server.py` (the `.mcp.json` launch wrapper,
read-only/fast path) and `skills/connect-outlook/scripts/outlook_setup_cli.py`
(the one-time interactive setup skill) so binary discovery, fixed paths, and
security defaults can't drift between the two.

`outlook-local-mcp` (github.com/desek/outlook-local-mcp) is installed
directly from upstream via `go install ...@latest` -- there is no bundling,
extraction, or R-Musubi involvement anywhere in this design. Updating is the
user deliberately re-running that same install command; nothing here
auto-updates.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Optional

BINARY_NAME = "outlook-local-mcp"
ENV_OVERRIDE_VAR = "OUTLOOK_MCP_BIN"
GO_BIN_DEFAULT = Path.home() / "go" / "bin" / BINARY_NAME

SECRETARY_HOME = Path.home() / ".secretary" / "outlook"
ACCOUNTS_PATH = SECRETARY_HOME / "accounts.json"
MARKER_PATH = SECRETARY_HOME / "setup.json"


def find_binary() -> dict:
    """Resolve the `outlook-local-mcp` binary. Order: an explicit env
    override (for power users / testing) -> PATH -> the documented
    `go install` default location. Returns {"path": Path|None, "source":
    str|None} -- never raises, never guesses beyond these three checks."""
    override = os.environ.get(ENV_OVERRIDE_VAR)
    if override:
        p = Path(override).expanduser()
        if p.exists():
            return {"path": p, "source": "env"}

    on_path = shutil.which(BINARY_NAME)
    if on_path:
        return {"path": Path(on_path), "source": "path"}

    if GO_BIN_DEFAULT.exists():
        return {"path": GO_BIN_DEFAULT, "source": "go-bin-default"}

    return {"path": None, "source": None}


def ensure_accounts_dir() -> Path:
    """Create ~/.secretary/outlook/ if missing and restrict it to the
    owner. The token cache file's own permissions are outlook-local-mcp's
    responsibility, not something this plugin controls."""
    SECRETARY_HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETARY_HOME, stat.S_IRWXU)  # 0700 -- owner rwx, nobody else
    return SECRETARY_HOME


def security_env_overrides() -> dict:
    """The ONLY env vars this plugin forces onto the subprocess -- read-only
    by design (Decision 5): mail reading is the point of this feature,
    mail/calendar management is not. Deliberately does NOT set
    OUTLOOK_MCP_CLIENT_ID/TENANT_ID (see outlook_mcp_server.py) so a power
    user's own pre-set values pass through untouched when the caller does
    `os.environ.copy()` then `.update()` with this dict."""
    return {
        "OUTLOOK_MCP_AUTH_METHOD": "device_code",
        "OUTLOOK_MCP_ACCOUNTS_PATH": str(ACCOUNTS_PATH),
        "OUTLOOK_MCP_MAIL_ENABLED": "true",
        "OUTLOOK_MCP_MAIL_MANAGE_ENABLED": "false",
        "OUTLOOK_MCP_READ_ONLY": "true",
        "OUTLOOK_MCP_LOG_LEVEL": "info",
    }


def _accounts_present(path: Optional[Path] = None) -> bool:
    """True only if accounts.json exists, parses, and has real content --
    not just an empty/zero-byte file. Deliberately shallow: the file's
    internal schema belongs to outlook-local-mcp, not to this plugin."""
    path = path or ACCOUNTS_PATH
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if isinstance(data, dict):
        return len(data) > 0
    if isinstance(data, list):
        return len(data) > 0
    return bool(data)


def is_setup_complete(accounts_path: Optional[Path] = None, marker_path: Optional[Path] = None) -> bool:
    """Both the marker `connect-outlook` writes on success AND a non-trivial
    accounts.json must be present -- catches "user deleted the token cache"
    without trusting a stale boolean written at setup time. Optional path
    overrides let connectors/outlook.py stay testable against a tmp dir
    without monkeypatching Path.home()."""
    marker_path = marker_path or MARKER_PATH
    return marker_path.exists() and _accounts_present(accounts_path)


def write_marker(**fields) -> Path:
    ensure_accounts_dir()
    MARKER_PATH.write_text(json.dumps(fields, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return MARKER_PATH


def clear_marker() -> bool:
    if MARKER_PATH.exists():
        MARKER_PATH.unlink()
        return True
    return False


def status() -> dict:
    """Aggregate status used by both `outlook_mcp_server.py --check` and
    `outlook_setup_cli.py status`."""
    found = find_binary()
    return {
        "binary_found": found["path"] is not None,
        "binary_path": str(found["path"]) if found["path"] else None,
        "binary_source": found["source"],
        "go_on_path": shutil.which("go") is not None,
        "accounts_present": _accounts_present(),
        "marker_present": MARKER_PATH.exists(),
        "setup_complete": is_setup_complete(),
        "accounts_path": str(ACCOUNTS_PATH),
        "marker_path": str(MARKER_PATH),
    }

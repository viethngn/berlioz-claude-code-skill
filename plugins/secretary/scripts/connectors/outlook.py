#!/usr/bin/env python3
"""Outlook connector -- R-Musubi-aware stub.

Investigation finding: R-Musubi (a separate macOS app) does not hold a
private/keychain-locked Outlook session -- it connects via a plain
`{url, token}` pair per service, the same pattern it uses for Confluence/
Jira/Figma, stored in its own settings file:

    ~/Library/Application Support/R-Musubi/settings.json
    -> {"services": {"outlook": {"url": "...", "token": "...", "enabled": true}}}

That means reuse is possible in principle: if the user has actually filled in
Outlook's url+token there, this connector can call that same endpoint with no
new auth code. As of this writing that entry is enabled but url/token are
BOTH EMPTY (toggled on, never configured) -- so today this always reports
"not_configured". The moment the user fills it in (in R-Musubi, or via
`secretary/sync.json` override), this connector activates without any code
change.

Full Outlook/Graph OAuth (device-code flow, MSAL, token storage) remains
explicitly out of scope -- that's the future `connect-outlook` skill. This
stub is the seam it plugs into.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DEFAULT_RMUSUBI_SETTINGS = Path.home() / "Library" / "Application Support" / "R-Musubi" / "settings.json"


def _read_rmusubi_outlook(settings_path: Path) -> Optional[dict]:
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return (data.get("services") or {}).get("outlook")


def plan(
    project_root: Path,
    *,
    within_days: int = 3,
    url: Optional[str] = None,
    token: Optional[str] = None,
    rmusubi_settings_path: Optional[Path] = None,
) -> dict:
    """`url`/`token` let `secretary/sync.json` override R-Musubi's settings
    directly (e.g. for users without R-Musubi installed). Never returns
    credentials in the plan -- only whether they're present."""
    if url and token:
        return {
            "source": "outlook",
            "status": "delegate",
            "mode": "agent",
            "instruction": (
                f"Fetch Outlook mail/calendar items due or received in the last {within_days} "
                f"day(s) from {url} using the configured token, extract candidate todos, and "
                "upsert them (source=outlook, source_ref=<message-or-event-id>)."
            ),
            "note": "using url/token from secretary/sync.json",
        }

    settings_path = rmusubi_settings_path or DEFAULT_RMUSUBI_SETTINGS
    outlook_cfg = _read_rmusubi_outlook(settings_path)

    if outlook_cfg is None:
        return {
            "source": "outlook",
            "status": "not_configured",
            "mode": "n/a",
            "note": f"R-Musubi settings not found at {settings_path}, and no url/token override "
                    "in secretary/sync.json -- run connect-outlook (future) or configure Outlook "
                    "in R-Musubi / secretary/sync.json.",
        }

    rm_url = outlook_cfg.get("url") or ""
    rm_token = outlook_cfg.get("token") or ""
    if not rm_url or not rm_token:
        return {
            "source": "outlook",
            "status": "not_configured",
            "mode": "n/a",
            "note": "Outlook is enabled in R-Musubi but url/token are not filled in there -- "
                    "configure it in R-Musubi's settings, or run connect-outlook (future).",
        }

    return {
        "source": "outlook",
        "status": "delegate",
        "mode": "agent",
        "instruction": (
            f"Fetch Outlook mail/calendar items due or received in the last {within_days} day(s) "
            f"from {rm_url} (reusing R-Musubi's configured Outlook endpoint), extract candidate "
            "todos, and upsert them (source=outlook, source_ref=<message-or-event-id>)."
        ),
        "note": "reusing R-Musubi's configured Outlook url/token",
    }

"""Load and validate the per-wiki .wikirc.json config.

Stdlib only. Every fetch/describe script starts with:

    from config import load_config
    cfg = load_config(wiki_root)

Prints a redacted view when run directly:

    python3 config.py --wiki-root /path/to/wiki
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


DEFAULT_WEB_USER_AGENT = "Mozilla/5.0 (compatible; llm-wiki-ingest/1.0)"

DEFAULT_CONFIG = {
    "wiki_root": ".",
    "raw_dir": "raw",
    "wiki_dir": "wiki",
    "auto_commit": True,
    "auto_push": False,
    "git": {
        "remote": "origin",
        "branch": "",
    },
    # Other llm-wiki instances this wiki can cross-link into (via [[label/slug]])
    # and optionally cascade-ingest matching sources into from /ingest. Empty by
    # default — opt-in per wiki, no effect unless populated.
    "linked_wikis": [],
    "atlassian": {
        "confluence_base_url": "",
        "jira_base_url": "",
        "confluence_pat": "",
        "jira_pat": "",
        "verify_ssl": True,
        "rate_limit_rps": 2,
        "burst": 5,
        "max_retries": 5,
        "retry_base_delay_seconds": 2,
    },
    "nano_banana": {
        "base_url": "",
        "api_key": "",
        "vision_model": "gemini-3-pro",
        "verify_ssl": True,
        "rate_limit_rps": 1,
        "burst": 2,
        "max_retries": 3,
        "retry_base_delay_seconds": 2,
    },
    "slack": {
        "token": "",
        "verify_ssl": True,
        "rate_limit_rps": 1,
        "burst": 3,
        "max_retries": 5,
        "retry_base_delay_seconds": 2,
    },
    "web": {
        "user_agent": DEFAULT_WEB_USER_AGENT,
        "verify_ssl": True,
        "rate_limit_rps": 1,
        "burst": 2,
        "max_retries": 3,
        "retry_base_delay_seconds": 2,
        "timeout_seconds": 30,
        "respect_robots": True,
        "min_image_bytes": 8192,
        "extra_headers": {},
    },
}


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, data: dict, path: Path, wiki_root: Path):
        self.data = data
        self.path = path
        self.wiki_root = wiki_root

    @property
    def raw_dir(self) -> Path:
        return self.wiki_root / self.data.get("raw_dir", "raw")

    @property
    def wiki_dir(self) -> Path:
        return self.wiki_root / self.data.get("wiki_dir", "wiki")

    @property
    def auto_commit(self) -> bool:
        return bool(self.data.get("auto_commit", True))

    @property
    def auto_push(self) -> bool:
        return bool(self.data.get("auto_push", False))

    def git_remote(self) -> str:
        return (self.data.get("git") or {}).get("remote", "origin")

    def git_branch(self) -> str:
        return (self.data.get("git") or {}).get("branch", "")

    def linked_wikis(self) -> list[dict]:
        """Other llm-wiki instances this wiki can cross-link into or cascade-ingest into.

        Each entry: label, path, role ("who"|"what"|"generic"), purpose,
        cascade_ingest (bool, default False). Malformed (non-dict) entries are
        dropped rather than raised on, since this list is hand-edited JSON.
        """
        raw = self.data.get("linked_wikis") or []
        return [dict(item) for item in raw if isinstance(item, dict)]

    def linked_wiki(self, label: str) -> Optional[dict]:
        return next((e for e in self.linked_wikis() if e.get("label") == label), None)

    def linked_wikis_by_role(self, role: str) -> list[dict]:
        return [e for e in self.linked_wikis() if e.get("role") == role]

    @property
    def atlassian(self) -> dict:
        return dict(self.data.get("atlassian") or {})

    @property
    def nano_banana(self) -> dict:
        return dict(self.data.get("nano_banana") or {})

    def confluence_base_url(self) -> str:
        return (self.atlassian.get("confluence_base_url") or "").rstrip("/")

    def jira_base_url(self) -> str:
        return (self.atlassian.get("jira_base_url") or "").rstrip("/")

    def confluence_pat(self) -> str:
        return self.atlassian.get("confluence_pat") or ""

    def jira_pat(self) -> str:
        return self.atlassian.get("jira_pat") or ""

    def atlassian_verify_ssl(self) -> bool:
        return bool(self.atlassian.get("verify_ssl", True))

    def nano_banana_base_url(self) -> str:
        return self.nano_banana.get("base_url") or ""

    def nano_banana_key(self) -> str:
        return self.nano_banana.get("api_key") or ""

    def nano_banana_model(self) -> str:
        return self.nano_banana.get("vision_model") or "gemini-3-pro"

    def nano_banana_verify_ssl(self) -> bool:
        return bool(self.nano_banana.get("verify_ssl", True))

    @property
    def slack(self) -> dict:
        return dict(self.data.get("slack") or {})

    def slack_token(self) -> str:
        return self.slack.get("token") or ""

    def slack_verify_ssl(self) -> bool:
        return bool(self.slack.get("verify_ssl", True))

    @property
    def web(self) -> dict:
        return dict(self.data.get("web") or {})

    def web_user_agent(self) -> str:
        return self.web.get("user_agent") or DEFAULT_WEB_USER_AGENT

    def web_verify_ssl(self) -> bool:
        return bool(self.web.get("verify_ssl", True))

    def web_timeout(self) -> int:
        return int(self.web.get("timeout_seconds") or 30)

    def web_respect_robots(self) -> bool:
        return bool(self.web.get("respect_robots", True))

    def web_min_image_bytes(self) -> int:
        return int(self.web.get("min_image_bytes") or 0)

    def web_extra_headers(self) -> dict:
        extra = self.web.get("extra_headers") or {}
        if not isinstance(extra, dict):
            return {}
        return {str(k): str(v) for k, v in extra.items()}

    def web_headers(self) -> dict:
        """User-Agent plus any configured extra headers (cookies, auth, …)."""
        return {"User-Agent": self.web_user_agent(), **self.web_extra_headers()}

    def redacted(self) -> dict:
        clone = json.loads(json.dumps(self.data))
        # linked_wikis carries no secrets (label/path/role/purpose/cascade_ingest
        # only) — passed through as-is, same as web.extra_headers' header names.

        def redact(section: dict, keys: list[str]) -> None:
            for k in keys:
                v = section.get(k)
                if v:
                    section[k] = f"<redacted {len(v)} chars>"
                else:
                    section[k] = ""

        atl = clone.setdefault("atlassian", {})
        redact(atl, ["confluence_pat", "jira_pat"])
        nb = clone.setdefault("nano_banana", {})
        redact(nb, ["api_key"])
        sl = clone.setdefault("slack", {})
        redact(sl, ["token"])
        # web.extra_headers is where a Cookie / Authorization header would live —
        # redact the values but keep the header names visible for debugging.
        web = clone.setdefault("web", {})
        extra = web.get("extra_headers")
        if isinstance(extra, dict):
            web["extra_headers"] = {
                k: (f"<redacted {len(str(v))} chars>" if v else "") for k, v in extra.items()
            }
        clone["_config_path"] = str(self.path)
        clone["_wiki_root"] = str(self.wiki_root)
        return clone


def _merge_defaults(user: dict) -> dict:
    def merge(base: dict, over: dict) -> dict:
        out = dict(base)
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out

    return merge(DEFAULT_CONFIG, user or {})


def find_config(wiki_root: Optional[Path]) -> Path:
    if wiki_root is None:
        wiki_root = Path.cwd()
    wiki_root = wiki_root.resolve()
    candidate = wiki_root / ".wikirc.json"
    if candidate.exists():
        return candidate

    # Walk up looking for a .wikirc.json (max 5 levels)
    current = wiki_root
    for _ in range(5):
        parent = current.parent
        if parent == current:
            break
        c = parent / ".wikirc.json"
        if c.exists():
            return c
        current = parent

    raise ConfigError(
        f".wikirc.json not found at {candidate} or any parent up to 5 levels. "
        "Create one with `/create-wiki` or copy .wikirc.example.json."
    )


def load_config(wiki_root: Optional[Path] = None) -> Config:
    config_path = find_config(wiki_root)
    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{config_path} is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno})"
        )

    merged = _merge_defaults(raw)
    resolved_root = (
        config_path.parent / merged.get("wiki_root", ".")
    ).resolve()

    return Config(merged, config_path, resolved_root)


def apply_ssl_env(section: str, verify: bool) -> None:
    """Toggle SSL verification env vars for one section's HTTP calls.

    Called by scripts before making a request to a section whose
    verify_ssl is False. This mirrors the pattern in nano-banana-pro's
    image.py — necessary on some corporate networks that intercept TLS.
    """
    if not verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Load and print a .wikirc.json config")
    parser.add_argument("--wiki-root", type=Path, default=None)
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(json.dumps(cfg.redacted(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

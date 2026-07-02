#!/usr/bin/env python3
"""Fetch one Jira issue → raw Markdown + source metadata.

Usage:
    python3 fetch_jira.py --wiki-root /path/to/wiki --key PROJ-123
    python3 fetch_jira.py --wiki-root /path/to/wiki --url https://jira.example.com/browse/PROJ-123

Writes:
    raw/<KEY>-<slug>.md          - Markdown-formatted ticket
    raw/<KEY>-<slug>.source.json - source metadata
    raw/images/<slug>/<n>.<ext>  - attachments (via extract_images.py)

Emits a JSON summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from _deps import require

require(["requests", "markdownify", "bs4"])

import requests
from markdownify import markdownify

from config import ConfigError, apply_ssl_env, load_config
from raw_store import write_raw_if_changed


ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_slug_re = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(title: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    dashed = _slug_re.sub("-", lowered).strip("-")
    return dashed or "untitled"


def extract_issue_key(url_or_key: str) -> Optional[str]:
    if ISSUE_KEY_RE.fullmatch(url_or_key):
        return url_or_key
    parsed = urlparse(url_or_key)
    if parsed.path:
        m = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", parsed.path)
        if m:
            return m.group(1)
    m = ISSUE_KEY_RE.search(url_or_key)
    return m.group(1) if m else None


def fetch_issue(base_url: str, pat: str, key: str, verify: bool) -> dict:
    url = f"{base_url}/rest/api/2/issue/{key}"
    params = {"expand": "renderedFields"}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, params=params, verify=verify, timeout=60)
    if resp.status_code == 401:
        raise SystemExit(
            "ERROR: 401 Unauthorized. Check atlassian.jira_pat in .wikirc.json."
        )
    if resp.status_code == 403:
        raise SystemExit(
            f"ERROR: 403 Forbidden — the PAT has no permission for issue {key}."
        )
    if resp.status_code == 404:
        raise SystemExit(
            f"ERROR: 404 Not Found — issue {key} does not exist at {base_url}."
        )
    resp.raise_for_status()
    return resp.json()


def _get(d: dict, path: list, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _fmt_list(items: Optional[list], key: str = "name") -> str:
    if not items:
        return ""
    parts = []
    for item in items:
        if isinstance(item, dict):
            parts.append(str(item.get(key, "")).strip())
        else:
            parts.append(str(item).strip())
    return ", ".join(p for p in parts if p)


def _description_markdown(fields: dict, rendered: dict) -> str:
    html = rendered.get("description") if rendered else None
    if html:
        return markdownify(html, heading_style="ATX", bullets="-").strip()
    plain = fields.get("description")
    if not plain:
        return "*(no description)*"
    if isinstance(plain, dict):
        return json.dumps(plain, indent=2, ensure_ascii=False)
    return str(plain).strip()


def _comment_markdown(comment: dict, rendered_comments: list) -> str:
    author = _get(comment, ["author", "displayName"], "unknown")
    created = comment.get("created", "")
    body = comment.get("body", "")
    # Prefer the rendered HTML if present
    rendered_body = ""
    cid = str(comment.get("id"))
    for rc in rendered_comments or []:
        if str(rc.get("id")) == cid:
            rendered_body = rc.get("body") or ""
            break
    if rendered_body:
        body = markdownify(rendered_body, heading_style="ATX", bullets="-").strip()
    if isinstance(body, dict):
        body = json.dumps(body, indent=2, ensure_ascii=False)
    return f"### {author} — {created}\n\n{body}\n"


def build_markdown(issue: dict) -> tuple[str, str, str]:
    """Return (title_for_h1, filename_slug, markdown_body)."""
    fields = issue.get("fields") or {}
    rendered = issue.get("renderedFields") or {}
    key = issue.get("key") or ""
    summary = fields.get("summary") or ""
    title_for_h1 = f"{key} — {summary}" if summary else key

    slug_base = slugify(summary) if summary else "issue"
    filename_slug = f"{key}-{slug_base}"

    lines: list[str] = []
    lines.append(f"# {title_for_h1}\n")
    lines.append("")

    meta = [
        ("Type", _get(fields, ["issuetype", "name"], "")),
        ("Status", _get(fields, ["status", "name"], "")),
        ("Priority", _get(fields, ["priority", "name"], "")),
        ("Reporter", _get(fields, ["reporter", "displayName"], "")),
        ("Assignee", _get(fields, ["assignee", "displayName"], "") or "Unassigned"),
        ("Created", fields.get("created", "")),
        ("Updated", fields.get("updated", "")),
        ("Fix versions", _fmt_list(fields.get("fixVersions"))),
        ("Labels", ", ".join(fields.get("labels") or [])),
        ("Components", _fmt_list(fields.get("components"))),
    ]
    for label, value in meta:
        if value:
            lines.append(f"**{label}:** {value}")
    lines.append("")
    lines.append("## Description")
    lines.append("")
    lines.append(_description_markdown(fields, rendered))
    lines.append("")

    comments = _get(fields, ["comment", "comments"], []) or []
    rendered_comments = _get(rendered, ["comment", "comments"], []) or []
    if comments:
        lines.append("## Comments")
        lines.append("")
        for c in comments:
            lines.append(_comment_markdown(c, rendered_comments))
    return title_for_h1, filename_slug, "\n".join(lines).strip() + "\n"


def attachment_hints(issue: dict) -> list[dict]:
    hints: list[dict] = []
    attachments = _get(issue, ["fields", "attachment"], []) or []
    for att in attachments:
        content = att.get("content") or ""
        filename = att.get("filename") or Path(content).name
        mime = att.get("mimeType", "")
        if mime.startswith("image/") or Path(filename).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
        }:
            hints.append(
                {"url": content, "filename": filename, "kind": "attachment"}
            )
    return hints


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Jira issue into raw/")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--key", help="Jira issue key like PROJ-123")
    parser.add_argument("--url", help="Jira issue URL")
    args = parser.parse_args()

    if not args.key and not args.url:
        parser.error("provide --key or --url")

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    base_url = cfg.jira_base_url()
    pat = cfg.jira_pat()
    if not base_url:
        print("ERROR: atlassian.jira_base_url is empty in .wikirc.json.", file=sys.stderr)
        return 1
    if not pat:
        print("ERROR: atlassian.jira_pat is empty in .wikirc.json.", file=sys.stderr)
        return 1

    apply_ssl_env("atlassian", cfg.atlassian_verify_ssl())

    key = args.key or extract_issue_key(args.url or "")
    if not key:
        print(f"ERROR: could not extract issue key from {args.url!r}.", file=sys.stderr)
        return 1

    verify = cfg.atlassian_verify_ssl()
    issue = fetch_issue(base_url, pat, key, verify)

    title, filename_slug, markdown = build_markdown(issue)
    hints = attachment_hints(issue)

    metadata = {
        "type": "jira",
        "key": key,
        "url": args.url or f"{base_url}/browse/{key}",
        "title": title,
        "issue_type": _get(issue, ["fields", "issuetype", "name"], ""),
        "status_name": _get(issue, ["fields", "status", "name"], ""),
        "updated_at": _get(issue, ["fields", "updated"], ""),
        "image_hints": hints,
    }

    result = write_raw_if_changed(cfg.raw_dir, filename_slug, markdown, metadata)

    print(
        json.dumps(
            {
                "slug": filename_slug,
                "title": title,
                "raw_md": result["raw_md"],
                "source_json": result["source_json"],
                "image_hints": hints,
                "status": result["status"],
                "content_sha256": result["content_sha256"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

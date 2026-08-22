#!/usr/bin/env python3
"""Fetch Slack channel messages, threads, or search results into raw/.

Calls the Slack Web API directly using a User OAuth token from .wikirc.json.
No Claude token usage at fetch time.

Usage:
    python3 fetch_slack.py --wiki-root PATH --channel CHANNEL [options]
    python3 fetch_slack.py --wiki-root PATH --channel CHANNEL --thread-ts TS
    python3 fetch_slack.py --wiki-root PATH --search "QUERY"

Diff strategy: each run produces a slug encoding the exact message date range
(e.g. slack-general-20260701-20260720). Re-running with the same bounds hits the
content-diff gate in raw_store and returns "unchanged" if nothing changed.
Without --after/--before, the script auto-increments from the last fetched_until
timestamp, so repeated plain runs only ingest new messages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _deps import require
require(["requests"])

import requests as _requests  # noqa: E402

from config import ConfigError, load_config
from rate_limiter import RateLimitFailure, get_limiter
from raw_store import wiki_state_dir, write_fetch_history, write_raw_if_changed

SLACK_API = "https://slack.com/api"

_PLACEHOLDER_RE = re.compile(r"REPLACE_ME|xoxp-REPLACE|xoxb-REPLACE", re.IGNORECASE)
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    return _SLUG_UNSAFE_RE.sub("-", text.lower()).strip("-")[:max_len]


def _ts_to_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _ts_to_display(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _date_to_ts(date_str: str) -> float:
    """Convert YYYY-MM-DD to a Unix timestamp (start of that day UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _slack_get(session: _requests.Session, limiter, method: str, params: dict) -> dict:
    # Route through limiter.request so HTTP 429/503 responses honor Retry-After
    # and back off, exactly like the Atlassian fetchers. throttle() alone only
    # paces outgoing calls; it does not retry a rate-limited response.
    url = f"{SLACK_API}/{method}"
    try:
        resp = limiter.request("GET", url, session=session, params=params)
    except RateLimitFailure as e:
        raise SystemExit(f"ERROR: {e}")
    try:
        resp.raise_for_status()
        data = resp.json()
    except (_requests.exceptions.HTTPError, ValueError) as e:
        raise SystemExit(
            f"ERROR: unexpected response from Slack {method} "
            f"(HTTP {resp.status_code}): {e}"
        )
    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        # Slack also signals throttling with ok:false error:ratelimited on some
        # endpoints; surface it clearly rather than as a generic failure.
        if error == "ratelimited":
            raise SystemExit(
                f"ERROR: Slack API {method} is rate-limited (ok:false ratelimited). "
                "Lower slack.rate_limit_rps in .wikirc.json and retry."
            )
        raise SystemExit(f"ERROR: Slack API {method} failed: {error}")
    return data


def _resolve_channel_id(session: _requests.Session, limiter, channel: str) -> tuple[str, str]:
    """Return (channel_id, channel_name). Accepts both IDs (C…) and names."""
    if re.match(r"^[A-Z0-9]{8,}$", channel.upper()):
        # Looks like a channel ID already
        data = _slack_get(session, limiter, "conversations.info", {"channel": channel})
        ch = data["channel"]
        return ch["id"], ch.get("name", channel)

    # Search by name (paginated)
    cursor: Optional[str] = None
    while True:
        params: dict = {"limit": 200, "types": "public_channel,private_channel"}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get(session, limiter, "conversations.list", params)
        for ch in data.get("channels") or []:
            if ch.get("name") == channel.lstrip("#"):
                return ch["id"], ch["name"]
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    raise SystemExit(
        f"ERROR: Slack channel '{channel}' not found. "
        "Check the name (without #) and make sure your token has channels:read / groups:read scope."
    )


def _fetch_channel_messages(
    session: _requests.Session,
    limiter,
    channel_id: str,
    oldest_ts: Optional[float],
    latest_ts: Optional[float],
    limit: int,
) -> tuple[list[dict], bool]:
    """Fetch channel history in the given window.

    limit <= 0 means "no cap" — paginate the whole window. Returns
    (messages_in_chronological_order, truncated) where truncated is True only
    when an explicit positive limit stopped us before the window was exhausted.
    """
    capped = limit > 0
    messages: list[dict] = []
    cursor: Optional[str] = None
    truncated = False

    while True:
        page_size = 200
        if capped:
            page_size = min(200, limit - len(messages))
            if page_size <= 0:
                truncated = True
                break

        params: dict = {"channel": channel_id, "limit": page_size}
        if oldest_ts is not None:
            params["oldest"] = f"{oldest_ts:.6f}"
        if latest_ts is not None:
            params["latest"] = f"{latest_ts:.6f}"
        if cursor:
            params["cursor"] = cursor

        data = _slack_get(session, limiter, "conversations.history", params)
        batch = data.get("messages") or []
        messages.extend(batch)

        if not data.get("has_more") or not batch:
            break
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    # API returns newest-first; reverse to chronological order
    messages.reverse()
    if capped and len(messages) > limit:
        # We kept the newest `limit`; older messages in the window were dropped.
        messages = messages[-limit:]
        truncated = True
    return messages, truncated


def _fetch_thread(
    session: _requests.Session,
    limiter,
    channel_id: str,
    thread_ts: str,
    limit: int,
) -> list[dict]:
    """Fetch a whole thread. limit <= 0 means no cap."""
    capped = limit > 0
    messages: list[dict] = []
    cursor: Optional[str] = None

    while True:
        page_size = 200
        if capped:
            page_size = min(200, limit - len(messages))
            if page_size <= 0:
                break

        params: dict = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": page_size,
        }
        if cursor:
            params["cursor"] = cursor

        data = _slack_get(session, limiter, "conversations.replies", params)
        batch = data.get("messages") or []
        messages.extend(batch)

        if not data.get("has_more") or not batch:
            break
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    return messages[:limit] if capped else messages


def _fetch_search(
    session: _requests.Session,
    limiter,
    query: str,
    oldest_ts: Optional[float],
    latest_ts: Optional[float],
    limit: int,
) -> list[dict]:
    """Paginate search.messages. limit <= 0 means no cap (bounded by the query
    and Slack's own search pagination limit)."""
    capped = limit > 0
    messages: list[dict] = []
    page = 1

    while True:
        if capped and len(messages) >= limit:
            break
        params: dict = {
            "query": query,
            "sort": "timestamp",
            "sort_dir": "asc",
            "count": 100,
            "page": page,
        }
        data = _slack_get(session, limiter, "search.messages", params)
        batch = (data.get("messages") or {}).get("matches") or []
        if not batch:
            break

        for msg in batch:
            ts = float(msg.get("ts", 0))
            if oldest_ts is not None and ts < oldest_ts:
                continue
            if latest_ts is not None and ts > latest_ts:
                continue
            messages.append(msg)
            if capped and len(messages) >= limit:
                break

        paging = (data.get("messages") or {}).get("paging") or {}
        if page >= paging.get("pages", 1):
            break
        page += 1

    return messages


def _resolve_users(
    session: _requests.Session, limiter, user_ids: set[str]
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for uid in user_ids:
        try:
            data = _slack_get(session, limiter, "users.info", {"user": uid})
            profile = data.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or data["user"].get("name")
                or uid
            )
            resolved[uid] = name
        except SystemExit:
            resolved[uid] = uid
    return resolved


def _format_markdown(
    messages: list[dict],
    channel_name: str,
    users: dict[str, str],
    date_range: str,
    source_label: str,
    is_thread: bool = False,
    is_search: bool = False,
) -> str:
    if is_thread:
        heading = f"Slack thread in #{channel_name} — {date_range}"
    elif is_search:
        heading = f"Slack search results — {date_range}"
    else:
        heading = f"#{channel_name} — {date_range}"

    kind = "Thread messages" if is_thread else ("Results" if is_search else "Messages")
    lines = [
        f"# {heading}",
        "",
        f"**Source**: {source_label}",
        f"**Date range**: {date_range}",
        f"**{kind}**: {len(messages)}",
        "",
        "---",
        "",
    ]

    parent_ts = messages[0].get("ts") if (is_thread and messages) else None

    for msg in messages:
        user_id = msg.get("user") or msg.get("username") or "unknown"
        display_name = users.get(user_id, user_id)
        ts = float(msg.get("ts", 0))
        ts_display = _ts_to_display(ts)
        text = (msg.get("text") or "").strip()

        # Resolve user mentions in message text
        def _replace_uid(m: re.Match) -> str:
            uid = m.group(1)
            return f"@{users.get(uid, uid)}"

        text = re.sub(r"<@([A-Z0-9]+)>", _replace_uid, text)

        # Thread reply indicator
        is_reply = is_thread and parent_ts and msg.get("ts") != parent_ts
        prefix = "↳ " if is_reply else ""

        lines.append(f"**{prefix}@{display_name}** ({ts_display}):")
        for line in text.splitlines():
            lines.append(f"> {line}" if line.strip() else ">")
        lines.append("")

        # Surface attachment URLs as plain links
        for att in msg.get("attachments") or []:
            att_text = att.get("text") or att.get("fallback") or ""
            if att_text:
                lines.append(f"> *[attachment]: {att_text.strip()}*")
                lines.append("")

        for fblock in msg.get("files") or []:
            fname = fblock.get("name") or "file"
            furl = fblock.get("url_private") or fblock.get("permalink") or ""
            if furl:
                lines.append(f"> *[file: {fname}]({furl})*")
            else:
                lines.append(f"> *[file: {fname}]*")
            lines.append("")

    return "\n".join(lines)


def _read_fetched_until(wiki_root: Path, channel_key: str) -> Optional[float]:
    path = wiki_state_dir(wiki_root) / "last-fetched.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    entry = data.get(channel_key) or {}
    fu = entry.get("fetched_until")
    if fu is None:
        return None
    return float(fu)


def _write_fetched_until(wiki_root: Path, channel_key: str, fetched_until: float) -> None:
    state_dir = wiki_state_dir(wiki_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "last-fetched.json"

    data: dict = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    entry = data.get(channel_key) or {}
    entry["fetched_until"] = fetched_until
    entry["at"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data[channel_key] = entry

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Slack messages into the llm-wiki raw/ directory"
    )
    parser.add_argument("--wiki-root", type=Path, required=True)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--channel", help="Channel name or ID (without #)")
    source_group.add_argument("--search", help="Search query (search.messages)")
    parser.add_argument("--thread-ts", help="Fetch a single thread (requires --channel)")
    parser.add_argument("--after", help="Start of date window YYYY-MM-DD")
    parser.add_argument("--before", help="End of date window YYYY-MM-DD")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Safety cap on messages fetched (0 = no cap, fetch the whole window). "
            "When a positive cap is hit, the newest N are kept and a truncation "
            "warning is emitted."
        ),
    )
    parser.add_argument("--force", action="store_true", help="Bypass content-diff gate")
    args = parser.parse_args()

    if args.thread_ts and not args.channel:
        parser.error("--thread-ts requires --channel")

    wiki_root = args.wiki_root.resolve()
    try:
        cfg = load_config(wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    token = cfg.slack_token()
    if not token or _PLACEHOLDER_RE.search(token):
        print(
            "ERROR: slack.token is missing or looks like a placeholder in .wikirc.json.\n"
            "See the README for instructions on obtaining a Slack User OAuth token (xoxp-…).",
            file=sys.stderr,
        )
        return 1

    limiter = get_limiter("slack", cfg.slack)
    session = _requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    now_ts = time.time()
    oldest_ts: Optional[float] = None
    latest_ts: Optional[float] = None

    if args.before:
        latest_ts = _date_to_ts(args.before)

    is_thread = bool(args.thread_ts)
    is_search = bool(args.search)

    # --- Resolve channel ---
    if args.channel:
        channel_id, channel_name = _resolve_channel_id(session, limiter, args.channel)
        channel_key = f"slack-channel-{channel_id}"
    else:
        channel_id = ""
        channel_name = ""
        channel_key = f"slack-search-{_slugify(args.search or '')}"

    # --- Determine oldest_ts ---
    if args.after:
        oldest_ts = _date_to_ts(args.after)
    elif not args.force:
        prior_until = _read_fetched_until(wiki_root, channel_key)
        if prior_until is not None:
            oldest_ts = prior_until

    # --- Fetch ---
    truncated = False
    if is_search:
        messages = _fetch_search(session, limiter, args.search or "", oldest_ts, latest_ts, args.limit)
    elif is_thread:
        messages = _fetch_thread(session, limiter, channel_id, args.thread_ts or "", args.limit)
    else:
        messages, truncated = _fetch_channel_messages(
            session, limiter, channel_id, oldest_ts, latest_ts, args.limit
        )

    if not messages:
        # No messages fetched — advance the watermark only as far as we actually
        # scanned (the requested window end, or now if the window was open-ended)
        # so we never skip a range we didn't read.
        new_fetched_until = (latest_ts or now_ts) + 0.000001
        _write_fetched_until(wiki_root, channel_key, new_fetched_until)
        write_fetch_history(wiki_root, channel_key, "unchanged", channel_key)
        print(json.dumps({
            "slug": channel_key,
            "status": "unchanged",
            "message_count": 0,
            "note": "No new messages since last fetch. Pass --force or --after to override.",
        }, indent=2, ensure_ascii=False))
        return 0

    # --- Compute actual date range from messages ---
    timestamps = [float(m.get("ts", 0)) for m in messages]
    actual_oldest = min(timestamps)
    actual_latest = max(timestamps)

    # Advance the incremental watermark to the newest message we actually
    # fetched (not wall-clock now, which could skip messages posted during the
    # scan). Next run picks up strictly after this timestamp.
    new_fetched_until = actual_latest + 0.000001
    _write_fetched_until(wiki_root, channel_key, new_fetched_until)
    oldest_date = _ts_to_date(actual_oldest)
    latest_date = _ts_to_date(actual_latest)
    date_range_display = (
        oldest_date[:4] + "-" + oldest_date[4:6] + "-" + oldest_date[6:]
        if oldest_date == latest_date
        else (oldest_date[:4] + "-" + oldest_date[4:6] + "-" + oldest_date[6:]
              + " to "
              + latest_date[:4] + "-" + latest_date[4:6] + "-" + latest_date[6:])
    )

    # --- Compute slug ---
    if is_search:
        slug = f"slack-search-{_slugify(args.search or '')}-{latest_date}"
        source_label = f"Slack search: {args.search}"
        title = f"Slack search: {args.search} — {date_range_display}"
    elif is_thread:
        # oldest_date alone collides whenever two threads in the same channel
        # start on the same calendar day — routine in any active channel.
        # thread_ts uniquely identifies the thread, so salt the slug with a
        # short hash of it (also recorded in metadata below, so a collision
        # is at least detectable after the fact).
        thread_hash = hashlib.sha256((args.thread_ts or "").encode("utf-8")).hexdigest()[:8]
        slug = f"slack-{channel_name}-thread-{oldest_date}-{thread_hash}"
        source_label = f"Slack thread in #{channel_name}"
        title = f"#{channel_name} thread — {date_range_display}"
    else:
        slug_suffix = oldest_date if oldest_date == latest_date else f"{oldest_date}-{latest_date}"
        slug = f"slack-{channel_name}-{slug_suffix}"
        source_label = f"Slack channel #{channel_name}"
        title = f"#{channel_name} — {date_range_display}"

    # --- Resolve user IDs ---
    user_ids = {
        m.get("user") for m in messages if m.get("user")
    }
    users = _resolve_users(session, limiter, user_ids)

    # --- Format Markdown ---
    markdown = _format_markdown(
        messages,
        channel_name=channel_name or (args.search or "search"),
        users=users,
        date_range=date_range_display,
        source_label=source_label,
        is_thread=is_thread,
        is_search=is_search,
    )

    # --- Write to raw/ ---
    source_dict = {
        "type": "slack",
        "channel": channel_name,
        "channel_id": channel_id,
        "oldest_ts": f"{actual_oldest:.6f}",
        "latest_ts": f"{actual_latest:.6f}",
        "fetched_until": f"{new_fetched_until:.6f}",
        "message_count": len(messages),
        "title": title,
    }
    if is_search:
        source_dict["search_query"] = args.search or ""
    if is_thread:
        source_dict["thread_ts"] = args.thread_ts or ""

    # Loudly flag partial coverage: an explicit --limit dropped older messages
    # in the window. Without this the user could believe the whole channel was
    # ingested when only the newest slice was.
    truncation_note: Optional[str] = None
    if truncated:
        truncation_note = (
            f"TRUNCATED: --limit {args.limit} was hit; only the newest {len(messages)} "
            "messages in the window were kept. Older messages were NOT ingested. "
            "Re-run with a higher --limit (or --limit 0 for no cap) and an explicit "
            "--before to backfill the earlier range."
        )
        print(f"WARNING: {truncation_note}", file=sys.stderr)

    result = write_raw_if_changed(cfg.raw_dir, slug, markdown, source_dict)

    if result["status"] == "unchanged" and not args.force:
        write_fetch_history(wiki_root, slug, "unchanged", channel_key)
        out = {
            "slug": slug,
            "status": "unchanged",
            "title": title,
            "message_count": len(messages),
            "date_range": date_range_display,
            "note": "Content unchanged since last ingest. Pass --force to re-write.",
        }
        if truncation_note:
            out["truncated"] = True
            out["truncation_note"] = truncation_note
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    write_fetch_history(wiki_root, slug, result["status"], channel_key)
    out = {
        "slug": slug,
        "status": result["status"],
        "title": title,
        "message_count": len(messages),
        "date_range": date_range_display,
        "oldest_ts": f"{actual_oldest:.6f}",
        "latest_ts": f"{actual_latest:.6f}",
        "raw_md": result["raw_md"],
        "source_json": result["source_json"],
    }
    if truncation_note:
        out["truncated"] = True
        out["truncation_note"] = truncation_note
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

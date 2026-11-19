#!/usr/bin/env python3
"""Smoke tests for skills/lint/scripts/wiki_tools.py.

Run:
    python3 plugins/llm-wiki/tests/smoke_wiki_tools.py

Each test_* gets a fresh temp dir. Exits 0 on success, non-zero on failure.
Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_lint import _assert, collision_fixture, make_wiki, write_page, write_raw  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "lint" / "scripts" / "wiki_tools.py"


def run_tool(root: Path, *args: str):
    out = subprocess.run(
        [sys.executable, str(TOOL), args[0], "--wiki-root", str(root), *args[1:]],
        capture_output=True, text=True,
    )
    _assert(out.returncode == 0, f"wiki_tools.py exit {out.returncode}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def test_locate_and_citing(tmp):
    root = collision_fixture(tmp)
    write_page(root, "2026-01-02", "## 09:00 — rate limit note\n\nrate limit rate limit\n")
    hits = run_tool(root, "locate", "rate limit")
    _assert({h["page"] for h in hits[:2]} == {"api-rate-limit", "rate-limits"}, f"locate: {hits}")
    _assert(all(h["page"] != "2026-01-02" for h in hits), f"journal skipped: {hits}")
    _assert(set(hits[0]) == {"page", "summary", "score"} and hits[0]["summary"] == "s", f"shape: {hits[0]}")
    _assert(run_tool(root, "locate", "zzzznothing") == [], "no hits → empty")
    _assert(run_tool(root, "citing", "a") == ["api-rate-limit", "rate-limits", "unrelated"], "citing")


def test_locate_weights(tmp):
    root = make_wiki(tmp)
    write_page(root, "widget", "nothing here. " * 5)
    write_page(root, "other", "nothing here. " * 5, summary="All about widget tuning.")
    write_page(root, "third", "A widget appears in the body. " * 3)
    hits = run_tool(root, "locate", "WIDGET")
    _assert([h["page"] for h in hits] == ["widget", "other", "third"], f"weights: {hits}")


def test_index_sync(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    for s in ("alpha", "beta", "gamma", "delta"):
        write_page(root, s, f"{s} body text. " * 6, summary=f"About {s}.")
    write_page(root, "archive/old", "old body text. " * 6)   # write_page creates wiki/archive/ as needed
    index = ("# Wiki Index\n\n## Concepts\n* [[alpha]]: hand written\n\n"
             "| page | note |\n|---|---|\n| [[beta|Beta]] | in a table |\n\n"   # Review Focus 5
             "- [[deleted]] — no longer exists\n- [[old]] — moved to archive\n")
    (root / "wiki/index.md").write_text(index)
    out = run_tool(root, "index-sync")
    _assert(out == {"added": ["delta", "gamma"], "removed": ["deleted"], "archived": ["old"]}, f"sync: {out}")
    text = (root / "wiki/index.md").read_text()
    _assert("* [[alpha]]: hand written" in text and "| [[beta|Beta]] | in a table |" in text, "existing lines untouched")
    _assert(text.count("[[alpha]]") == 1 and text.count("[[beta") == 1, "no duplicates")
    _assert("## Uncategorized\n- [[delta]] — About delta.\n- [[gamma]] — About gamma." in text, "added with summary")
    _assert("[[deleted]]" not in text and "[[archive/old]]" in text.split("## Archive")[1], "removed / archived")
    _assert(run_tool(root, "index-sync") == {"added": [], "removed": [], "archived": []}, "idempotent")


def test_index_sync_journal(tmp):
    root = make_wiki(tmp)
    write_page(root, "alpha", "alpha body text. " * 6, summary="About alpha.")
    write_page(root, "2026-01-02", "## 09:00 — Note\n\nday text.\n")
    write_page(root, "2026-01-03", "## 09:00 — Note\n\nday text.\n")
    write_page(root, "2026-01-index", "- [[2026-01-02]]\n")
    write_page(root, "archive/old", "old body text. " * 6)
    diary = "## Diary\n- [[2026-01-02]] — day\n- [[2025-12-31]] — gone day\n- [[old]] — odd entry\n"
    (root / "wiki/index.md").write_text(f"# Wiki Index\n\n{diary}\n## Concepts\n- [[alpha]]\n")
    out = run_tool(root, "index-sync")
    _assert(out == {"added": [], "removed": [], "archived": []}, f"journal sync: {out}")
    text = (root / "wiki/index.md").read_text()
    _assert(diary in text, "Diary section untouched")
    _assert("2026-01-03" not in text and "2026-01-index" not in text, "journal pages never added")


def test_log(tmp):
    root = make_wiki(tmp)
    (root / "wiki/log.md").write_text("# Wiki Log\n\n## 2026-01-01\n- old entry\n")
    run_tool(root, "log", "--op", "lint", "--title", "Cleaned 2 pages", "--bullet", "Merged [[a]] into [[b]]")
    lines = (root / "wiki/log.md").read_text().splitlines()
    _assert(lines[0] == "# Wiki Log" and lines[2].endswith("(lint)") and lines[3] == "- Cleaned 2 pages", f"log: {lines[:6]}")
    _assert(lines[4] == "- Merged [[a]] into [[b]]", f"bullet: {lines[:6]}")
    _assert("## 2026-01-01" in lines and lines.index("## 2026-01-01") > 4, "old entries kept below")
    run_tool(root, "log", "--op", "log", "--title", "Diary entry")
    lines = (root / "wiki/log.md").read_text().splitlines()
    _assert(lines[2].endswith("(log)") and lines[3] == "- Diary entry", f"log op: {lines[:6]}")


def test_log_creates_file(tmp):
    root = make_wiki(tmp)
    run_tool(root, "log", "--op", "ingest", "--title", "Ingested x")
    lines = (root / "wiki/log.md").read_text().splitlines()
    _assert(lines[0] == "# Wiki Log" and lines[2].startswith("## 20") and "(" not in lines[2]
            and lines[3] == "- Ingested x", f"new log: {lines}")


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                t(Path(td))
                print(f"OK   {t.__name__}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

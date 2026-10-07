#!/usr/bin/env python3
"""Smoke test for the bulk ingest pipeline.

Spins up a local HTTP server that mimics a Confluence Server REST API and:

1. Verifies `ingest.detect_bulk_from_url` for representative URL shapes
2. Runs `discover.py --space FOO` against the mock server, expecting the
   queue.json to be created with the correct set of items.
3. Runs `prefetch.py --job-id <id>` against the mock server; the server
   returns HTTP 429 with `Retry-After: 1` for the first request to each
   page ID to verify the rate limiter's retry path. Verifies every item
   ends up `raw_status == done` and content lands in `raw/<slug>.md`.
4. Simulates a Ctrl-C mid-run: uses `--max-items 2` on a queue of 4
   pages, then a follow-up `--resume` run to verify checkpointing +
   resume picks up the remaining items.
5. Runs `queue_admin.py list` / `show` for basic sanity.

Run:
    ~/.llm-wiki-venv/bin/python3 plugins/llm-wiki/tests/smoke_bulk_ingest.py

(The plugin's deps live in that venv, so plain `python3` fails on
`markdownify` / `beautifulsoup4`.)

Exits 0 on success, non-zero on failure. Uses only stdlib for the driver.
Requires `requests`, `markdownify`, `beautifulsoup4` installed for the
fetchers (same as production).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "ingest" / "scripts"


# ------------------------------- Mock server --------------------------------


class MockConfluenceHandler(http.server.BaseHTTPRequestHandler):
    """Minimal Confluence Server REST API replica.

    - GET /rest/api/content?spaceKey=FOO&type=page&limit=N&start=S
        → paginated {"results":[{"id":..,"title":..}], "size": N}
    - GET /rest/api/content/<pageId>?expand=body.storage,...
        → {"id":..,"title":..,"body":{"storage":{"value":"..."}}, ...}

    The first request for each pageId returns HTTP 429 with Retry-After: 1
    so we exercise the rate limiter's backoff path. Subsequent requests
    for the same pageId succeed.

    The server keeps a hit counter per pageId to make this deterministic.
    """

    hits: dict = {}
    requests: list = []  # (pageId, expand) for every content fetch, in order
    lock = threading.Lock()
    pages: dict = {}  # pageId -> {title, body}

    def log_message(self, format, *args):  # noqa: A002
        return  # keep test output clean

    def _send_json(self, status: int, payload: dict, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/rest/api/content":
            space = (qs.get("spaceKey") or [""])[0]
            start = int((qs.get("start") or ["0"])[0])
            limit = int((qs.get("limit") or ["50"])[0])
            all_ids = sorted(self.pages.keys())
            slice_ = all_ids[start : start + limit]
            self._send_json(
                200,
                {
                    "results": [
                        {"id": pid, "title": self.pages[pid]["title"]}
                        for pid in slice_
                    ],
                    "size": len(slice_),
                    "start": start,
                    "limit": limit,
                },
            )
            return

        # Content fetch: /rest/api/content/<pageId>
        prefix = "/rest/api/content/"
        if path.startswith(prefix):
            page_id = path[len(prefix) :]
            if page_id not in self.pages:
                self._send_json(404, {"message": "not found"})
                return
            with self.lock:
                self.hits[page_id] = self.hits.get(page_id, 0) + 1
                hits = self.hits[page_id]
                self.requests.append((page_id, (qs.get("expand") or [""])[0]))
            if hits == 1:
                # First hit → 429 with Retry-After: 1
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            page = self.pages[page_id]
            self._send_json(
                200,
                {
                    "id": page_id,
                    "title": page["title"],
                    "version": {"number": page.get("version", 1)},
                    "space": {"key": "FOO"},
                    "body": {
                        "storage": {
                            "value": (
                                f"<p>{page['body']}</p>"
                                "<p>Some more content for hashing.</p>"
                            )
                        }
                    },
                },
            )
            return

        self._send_json(404, {"message": "unknown path", "path": path})


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(pages: dict) -> tuple[http.server.HTTPServer, int]:
    port = _find_free_port()
    MockConfluenceHandler.hits = {}
    MockConfluenceHandler.requests = []
    MockConfluenceHandler.pages = pages
    srv = http.server.HTTPServer(("127.0.0.1", port), MockConfluenceHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, port


# ------------------------------- Assertions ---------------------------------


def _run_script(script: str, args: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None, env=env
    )


def _write_wikirc(wiki_root: Path, port: int) -> None:
    (wiki_root / ".wikirc.json").write_text(
        json.dumps(
            {
                "wiki_root": ".",
                "raw_dir": "raw",
                "wiki_dir": "wiki",
                "auto_commit": False,
                "atlassian": {
                    "confluence_base_url": f"http://127.0.0.1:{port}",
                    "jira_base_url": f"http://127.0.0.1:{port}",
                    "confluence_pat": "test-pat",
                    "jira_pat": "test-pat",
                    "verify_ssl": True,
                    "rate_limit_rps": 20,
                    "burst": 5,
                    "max_retries": 3,
                    "retry_base_delay_seconds": 0.2,
                },
                "nano_banana": {
                    "base_url": "",
                    "api_key": "",
                    "vision_model": "gemini-3-pro",
                    "verify_ssl": True,
                },
            },
            indent=2,
        )
    )
    (wiki_root / "raw").mkdir(exist_ok=True)
    (wiki_root / "wiki").mkdir(exist_ok=True)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ------------------------------- Test steps ---------------------------------


def test_detect_bulk_from_url() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from ingest import detect_bulk_from_url  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    # Single Confluence page — must return None
    _assert(
        detect_bulk_from_url("https://c.example.com/spaces/FOO/pages/12345/Feature") is None,
        "URL with /pages/N should be single, not bulk",
    )
    _assert(
        detect_bulk_from_url("https://c.example.com/pages/viewpage.action?pageId=98765") is None,
        "URL with pageId= should be single, not bulk",
    )

    # Confluence space overview — must return ('confluence_space', 'FOO')
    result = detect_bulk_from_url("https://c.example.com/spaces/FOO")
    _assert(result == ("confluence_space", "FOO"), f"expected space FOO, got {result}")
    result = detect_bulk_from_url("https://c.example.com/spaces/FOO/")
    _assert(result == ("confluence_space", "FOO"), f"expected space FOO, got {result}")

    # /display/KEY — bulk
    result = detect_bulk_from_url("https://c.example.com/display/FOO")
    _assert(result == ("confluence_space", "FOO"), f"expected space FOO, got {result}")

    # ?spaceKey=FOO without pageId — bulk
    result = detect_bulk_from_url("https://c.example.com/some/path?spaceKey=FOO")
    _assert(result == ("confluence_space", "FOO"), f"expected space FOO, got {result}")

    # Not a URL — None
    _assert(detect_bulk_from_url("PROJ-123") is None, "Jira key should not be a bulk URL")

    print("[OK] detect_bulk_from_url")


def test_discover_and_prefetch(pages: dict, wiki_root: Path, port: int) -> str:
    """Runs discover.py and prefetch.py end-to-end. Returns the job id."""
    r = _run_script(
        "discover.py",
        ["--wiki-root", str(wiki_root), "--space", "FOO"],
    )
    _assert(r.returncode == 0, f"discover.py failed: {r.stderr}\n{r.stdout}")
    payload = json.loads(r.stdout.strip())
    job_id = payload["job_id"]
    counts = payload["counts"]
    _assert(counts["total"] == len(pages), f"expected {len(pages)} items, got {counts}")

    # First prefetch pass — only 2 items to simulate an interrupted run
    r = _run_script(
        "prefetch.py",
        [
            "--wiki-root",
            str(wiki_root),
            "--job-id",
            job_id,
            "--max-items",
            "2",
            "--skip-images",
        ],
    )
    _assert(r.returncode == 0, f"prefetch (partial) failed: {r.stderr}\n{r.stdout}")

    # Inspect the queue — 2 items should be done, 2 pending
    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id])
    _assert(r.returncode == 0, f"queue show failed: {r.stderr}")
    q = json.loads(r.stdout.strip())
    counts = q["counts"]
    _assert(counts["raw_done"] == 2, f"expected 2 done after partial run, got {counts}")
    _assert(counts["pending_raw"] == len(pages) - 2, f"unexpected pending: {counts}")

    # Second pass — --resume should complete the remaining items
    r = _run_script(
        "prefetch.py",
        [
            "--wiki-root",
            str(wiki_root),
            "--job-id",
            job_id,
            "--skip-images",
        ],
    )
    _assert(r.returncode == 0, f"prefetch (resume) failed: {r.stderr}\n{r.stdout}")

    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id])
    q = json.loads(r.stdout.strip())
    counts = q["counts"]
    _assert(
        counts["raw_done"] == len(pages),
        f"expected all {len(pages)} done after resume, got {counts}",
    )
    _assert(counts["failed"] == 0, f"unexpected failures after resume: {q}")

    # Every page should have a raw/<slug>.md
    for item in q["items"]:
        slug = item["slug"]
        _assert(slug, f"item {item['ref']} has no slug")
        md_path = wiki_root / "raw" / f"{slug}.md"
        _assert(md_path.exists(), f"raw file missing: {md_path}")

    # Confirm the rate limiter retried at least once for each page (server
    # returned 429 on hit #1, 200 on hit #2)
    _assert(
        all(v >= 2 for v in MockConfluenceHandler.hits.values()),
        f"expected >=2 hits per page (429 then 200), got {MockConfluenceHandler.hits}",
    )
    print("[OK] discover + prefetch + resume + rate-limit retry")
    return job_id


def test_ingest_auto_detect_bulk(wiki_root: Path, port: int) -> None:
    """End-to-end: ingest.py <spaces URL> must route to bulk mode."""
    r = _run_script(
        "ingest.py",
        [
            "--wiki-root",
            str(wiki_root),
            "--source",
            f"http://127.0.0.1:{port}/spaces/FOO",
            "--discover-only",
            "--replace",
        ],
    )
    _assert(
        r.returncode == 0,
        f"ingest.py auto-detect bulk failed: {r.stderr}\n{r.stdout}",
    )
    _assert(
        '"mode": "bulk"' in r.stdout,
        f"expected bulk mode in output, got: {r.stdout}",
    )
    _assert(
        '"phase": "discover"' in r.stdout,
        f"expected discover phase, got: {r.stdout}",
    )
    print("[OK] ingest.py auto-detects /spaces/ URL as bulk")


def test_confluence_slug_collision(wiki_root: Path, port: int) -> None:
    """Two different pages sharing a title must not collide onto one slug.

    Both mock pages report version 1 (MockConfluenceHandler always does) —
    exactly the "freshly created pages at the same version number" scenario
    that caused the pre-fix version-cache check to report page B's fetch as
    "unchanged" using page A's stale, cached content.
    """
    MockConfluenceHandler.pages["20001"] = {"title": "Duplicate Title", "body": "first page body"}
    MockConfluenceHandler.pages["20002"] = {"title": "Duplicate Title", "body": "second page body"}

    r1 = _run_script(
        "fetch_confluence.py", ["--wiki-root", str(wiki_root), "--page-id", "20001"]
    )
    _assert(r1.returncode == 0, f"first page fetch failed: {r1.stderr}\n{r1.stdout}")
    s1 = json.loads(r1.stdout.strip())

    r2 = _run_script(
        "fetch_confluence.py", ["--wiki-root", str(wiki_root), "--page-id", "20002"]
    )
    _assert(r2.returncode == 0, f"second page fetch failed: {r2.stderr}\n{r2.stdout}")
    s2 = json.loads(r2.stdout.strip())

    _assert(
        s1["slug"] != s2["slug"],
        f"colliding pages got the same slug ({s1['slug']}) — one overwrote the other",
    )
    _assert(
        "already used by page" in r2.stderr,
        f"expected a collision WARNING on stderr, got: {r2.stderr!r}",
    )
    md1 = (wiki_root / "raw" / f"{s1['slug']}.md").read_text(encoding="utf-8")
    md2 = (wiki_root / "raw" / f"{s2['slug']}.md").read_text(encoding="utf-8")
    _assert("first page body" in md1, f"page 1 content wrong:\n{md1}")
    _assert("second page body" in md2, f"page 2 content wrong:\n{md2}")

    # Re-fetching page 20002 must match itself (page_id-scoped), not fall
    # back to page 20001's cached version/content just because both are v1
    # and share a title.
    r3 = _run_script(
        "fetch_confluence.py", ["--wiki-root", str(wiki_root), "--page-id", "20002"]
    )
    _assert(r3.returncode == 0, f"re-fetch of page 20002 failed: {r3.stderr}")
    s3 = json.loads(r3.stdout.strip())
    _assert(
        s3["slug"] == s2["slug"] and s3["status"] == "unchanged",
        f"re-fetch of the disambiguated page is not stable: {s3}",
    )
    print("[OK] Confluence same-title pages resolve to distinct, stable slugs")


def test_queue_utilities(wiki_root: Path, job_id: str) -> None:
    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "list"])
    _assert(r.returncode == 0, f"queue list failed: {r.stderr}")
    payload = json.loads(r.stdout.strip())
    _assert(any(j["id"] == job_id for j in payload["jobs"]), f"job {job_id} not listed")

    # Reset one item to pending, then verify
    q = json.loads(
        _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id]).stdout
    )
    victim = q["items"][0]["ref"]
    r = _run_script(
        "queue_admin.py",
        [
            "--wiki-root",
            str(wiki_root),
            "mark",
            job_id,
            "--ref",
            victim,
            "--raw-status",
            "pending",
        ],
    )
    _assert(r.returncode == 0, f"queue mark failed: {r.stderr}")

    q = json.loads(
        _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id]).stdout
    )
    item = next(i for i in q["items"] if i["ref"] == victim)
    _assert(item["raw_status"] == "pending", f"expected pending, got {item}")
    print("[OK] queue_admin.py list / show / mark")


def test_job_id_path_traversal_rejected(wiki_root: Path) -> None:
    """A crafted job_id must never be joined into a filesystem path."""
    r = _run_script(
        "queue_admin.py", ["--wiki-root", str(wiki_root), "delete", "../../.git", "--force"]
    )
    _assert(r.returncode != 0, "path-traversal job_id should be rejected")
    _assert(
        "invalid job_id" in r.stderr,
        f"expected a clean invalid-job_id error, got: {r.stderr!r}",
    )
    print("[OK] queue_admin.py rejects a path-traversal job_id")


def test_renamed_raw_wiki_dirs(port: int) -> None:
    """git_commit() must use the configured raw_dir/wiki_dir, not literals."""
    with tempfile.TemporaryDirectory() as td:
        wiki_root = Path(td) / "wiki"
        wiki_root.mkdir()
        (wiki_root / ".wikirc.json").write_text(
            json.dumps(
                {
                    "wiki_root": ".",
                    "raw_dir": "sources",
                    "wiki_dir": "knowledge",
                    "auto_commit": True,
                    "atlassian": {
                        "confluence_base_url": f"http://127.0.0.1:{port}",
                        "jira_base_url": f"http://127.0.0.1:{port}",
                        "confluence_pat": "test-pat",
                        "jira_pat": "test-pat",
                    },
                    "nano_banana": {"base_url": "", "api_key": ""},
                },
                indent=2,
            )
        )
        (wiki_root / "sources").mkdir()
        (wiki_root / "knowledge").mkdir()
        subprocess.run(["git", "init", "-q", str(wiki_root)], check=True)
        subprocess.run(["git", "-C", str(wiki_root), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(wiki_root), "config", "user.name", "t"], check=True)

        r = _run_script("fetch_confluence.py", ["--wiki-root", str(wiki_root), "--page-id", "10001"])
        _assert(r.returncode == 0, f"fetch failed: {r.stderr}\n{r.stdout}")
        slug = json.loads(r.stdout.strip())["slug"]

        r = _run_script(
            "ingest.py", ["--wiki-root", str(wiki_root), "--commit-only", "--slug", slug]
        )
        _assert(
            r.returncode == 0,
            f"commit with renamed raw_dir/wiki_dir should not crash: {r.stderr}\n{r.stdout}",
        )
        # git's own "[main abc123] message" line (inherited stdout from the
        # uncaptured `git commit` subprocess inside git_commit()) precedes
        # ingest.py's own JSON print on the same stream — take the trailing
        # object, not the whole thing.
        stdout = r.stdout.strip()
        commit = json.loads(stdout[stdout.rfind("\n{") + 1 :])
        _assert(commit.get("commit"), f"expected a real commit, got: {commit}")
    print("[OK] git_commit() honors renamed raw_dir/wiki_dir instead of crashing")


# ------------------------------- Entrypoint ---------------------------------


# ------------------- re-running a bulk source: resume vs refresh -------------


def _ingest(wiki_root: Path, *flags: str) -> subprocess.CompletedProcess:
    return _run_script("ingest.py", ["--wiki-root", str(wiki_root), *flags])


def _first_json(stdout: str) -> dict:
    """The first pretty-printed JSON object in ingest.py's streamed stdout (discover report)."""
    start = stdout.index("{")
    return json.JSONDecoder().raw_decode(stdout[start:])[0]


def _queue(wiki_root: Path, job_id: str) -> dict:
    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id])
    _assert(r.returncode == 0, f"queue show failed: {r.stderr}")
    return json.loads(r.stdout)


def _states(wiki_root: Path, job_id: str) -> dict:
    return {
        i["ref"]: (i["raw_status"], i["wiki_status"]) for i in _queue(wiki_root, job_id)["items"]
    }


def _only_job(wiki_root: Path) -> str:
    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "list"])
    jobs = json.loads(r.stdout)["jobs"]
    _assert(len(jobs) == 1, f"expected exactly one job, got {jobs}")
    return jobs[0]["id"]


def _mark_wiki_done(wiki_root: Path, job_id: str, *refs: str) -> None:
    for ref in refs:
        r = _run_script(
            "queue_admin.py",
            ["--wiki-root", str(wiki_root), "mark", job_id, "--ref", ref, "--wiki-done"],
        )
        _assert(r.returncode == 0, f"mark failed: {r.stderr}")


def test_rerun_is_resume_and_replace_is_refresh() -> None:
    """A plain re-run of a bulk source only resumes (and says so); --replace refreshes."""
    pages = {
        "10001": {"title": "Onboarding", "body": "how to onboard"},
        "10002": {"title": "Runbook", "body": "alerts steps"},
        "10003": {"title": "Handbook", "body": "team handbook"},
    }
    srv, port = _start_server(pages)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki = Path(td) / "wiki"
            wiki.mkdir()
            _write_wikirc(wiki, port)
            flags = ["--space", "FOO", "--skip-images"]

            # Baseline: fetch everything, then pretend the wiki pages were synthesized.
            r = _ingest(wiki, *flags)
            _assert(r.returncode == 0, f"baseline failed: {r.stderr}\n{r.stdout}")
            job = _only_job(wiki)
            _mark_wiki_done(wiki, job, *pages)

            # The page set changes upstream: one page edited, one added.
            pages["10002"].update(body="alerts steps v2", version=2)
            pages["10004"] = {"title": "New page", "body": "brand new"}

            # Plain re-run: reuses the queue, re-checks nothing, and says so.
            seen = len(MockConfluenceHandler.requests)
            r = _ingest(wiki, *flags)
            _assert(r.returncode == 0, f"plain re-run failed: {r.stderr}\n{r.stdout}")
            _assert(len(MockConfluenceHandler.requests) == seen, "a plain re-run must not re-fetch")
            report = _first_json(r.stdout)
            _assert(report["reused"] is True and report["rechecked"] is False, report)
            _assert("NOT picked up" in report["note"] and "--replace" in report["note"], report)
            _assert(
                set(_states(wiki, job).values()) == {("done", "done")},
                "stale queue is untouched by a plain re-run",
            )
            _assert(len(_queue(wiki, job)["items"]) == 3, "new page is not discovered by a re-run")

            # --replace: re-enumerates, re-checks every page, keeps unchanged pages' wiki status.
            seen = len(MockConfluenceHandler.requests)
            r = _ingest(wiki, *flags, "--replace")
            _assert(r.returncode == 0, f"--replace failed: {r.stderr}\n{r.stdout}")
            new = MockConfluenceHandler.requests[seen:]
            for pid in pages:
                _assert(any(p == pid and e == "version" for p, e in new), f"{pid} not re-checked: {new}")
            st = _states(wiki, job)
            _assert(st["10002"] == ("done", "pending"), f"edited page must be re-synthesized: {st}")
            _assert(st["10004"] == ("done", "pending"), f"new page must be found: {st}")
            _assert(st["10001"] == ("unchanged", "done"), f"unchanged keeps wiki status: {st}")
            _assert(st["10003"] == ("unchanged", "done"), f"unchanged keeps wiki status: {st}")
            _assert(_only_job(wiki) == job, "--replace reuses the job id")
            _mark_wiki_done(wiki, job, "10002")

            # 10004 is still unsynthesized. A refresh with nothing new must not forget it.
            r = _ingest(wiki, *flags, "--replace")
            _assert(r.returncode == 0, f"second --replace failed: {r.stderr}")
            st = _states(wiki, job)
            _assert(st["10004"] == ("unchanged", "pending"), f"unfinished page must stay pending: {st}")
            _assert(
                all(st[k] == ("unchanged", "done") for k in ("10001", "10002", "10003")), st
            )

            # An unfinished fetch is reported as a continue, not as "nothing re-checked".
            _run_script(
                "queue_admin.py",
                ["--wiki-root", str(wiki), "mark", job, "--ref", "10003", "--raw-status", "pending"],
            )
            r = _ingest(wiki, *flags)
            report = _first_json(r.stdout)
            _assert(report["reused"] is True and "still to fetch" in report["note"], report)

            # --force is not a refresh: it re-fetches AND re-synthesizes everything.
            r = _ingest(wiki, *flags, "--replace", "--force")
            _assert(r.returncode == 0, f"--replace --force failed: {r.stderr}\n{r.stdout}")
            _assert(
                set(_states(wiki, job).values()) == {("done", "pending")},
                f"--force re-runs synthesis for every page: {_states(wiki, job)}",
            )
    finally:
        srv.shutdown()
    print("[OK] plain re-run = resume (says so); --replace = refresh (new + edited found, unchanged kept)")


def main() -> int:
    test_detect_bulk_from_url()
    test_rerun_is_resume_and_replace_is_refresh()

    pages = {
        "10001": {"title": "Onboarding", "body": "how to onboard"},
        "10002": {"title": "Runbook: Alerts", "body": "alerts steps"},
        "10003": {"title": "Team Handbook", "body": "team handbook"},
        "10004": {"title": "Release Process", "body": "release notes"},
    }
    srv, port = _start_server(pages)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port)
            job_id = test_discover_and_prefetch(pages, wiki_root, port)
            test_queue_utilities(wiki_root, job_id)
            test_ingest_auto_detect_bulk(wiki_root, port)
            test_confluence_slug_collision(wiki_root, port)
            test_job_id_path_traversal_rejected(wiki_root)
            test_renamed_raw_wiki_dirs(port)
    finally:
        srv.shutdown()

    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

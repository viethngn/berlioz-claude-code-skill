#!/usr/bin/env python3
"""Smoke test for refresh-all (`/ingest` with no source, or `--refresh-all`).

Refresh-all is modelled as one more bulk queue kind: `discover.py --refresh`
builds a single queue covering every source the wiki knows about, and
`prefetch.py` really fetches each one and diff-gates it against raw/. These
tests cover that pipeline plus the failure modes an audit found in its first
implementation:

1. `list_sources.py` enumeration: numeric (not lexicographic) version dedup,
   local-missing-original routed to `skipped`, Slack channels collapsed and
   threads kept while ad hoc searches are dropped, bulk-query discovery flags
   rebuilt including the `--site` vs `--sitemap` heuristic, and the coverage
   invariant that EVERY raw/*.source.json lands in exactly one bucket.
2. Registry robustness: a malformed `raw/.bulk-queries.json` is a hard error
   that leaves the file untouched (it used to be silently overwritten, losing
   every registered query), a non-dict one doesn't crash, a plain reuse
   registers a missing query without churning the file, and queries that exist
   only as a local job queue get backfilled.
3. The `wiki_status` carry-over: an unchanged refetch must not force
   re-synthesis, a changed one must — including after an image-step failure and
   a `--retry-failed` retry, which used to mark a changed page as done without
   ever synthesizing it.
4. End-to-end refresh against mock Confluence + web servers: unchanged,
   changed, added-upstream and removed-upstream pages all classified correctly,
   with only the genuinely new/changed ones left pending synthesis.
5. Slack's committed watermark: with `.wiki-state/` absent, the window floor
   comes from the committed `fetched_until` at full precision.
6. `ingest.py` dispatch and the arguments that must be rejected.

Run (the plugin's deps live in a venv, so plain `python3` will not do):
    ~/.llm-wiki-venv/bin/python3 plugins/llm-wiki/tests/smoke_refresh_all.py

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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "ingest" / "scripts"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _run_script(script: str, args: list) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


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
                "web": {"rate_limit_rps": 20, "burst": 5, "max_retries": 2},
                "slack": {"token": "xoxp-test-token", "rate_limit_rps": 20, "burst": 5},
                "nano_banana": {"base_url": "", "api_key": "", "vision_model": "gemini-3-pro"},
            },
            indent=2,
        )
    )
    (wiki_root / "raw").mkdir(exist_ok=True)
    (wiki_root / "wiki").mkdir(exist_ok=True)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(handler_cls) -> tuple[http.server.HTTPServer, int]:
    port = _find_free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


REGISTRY_NAME = ".bulk-queries.json"


def _registry(raw: Path, queries: list) -> None:
    (raw / REGISTRY_NAME).write_text(json.dumps({"version": 1, "queries": queries}, indent=2))


# --------------------------- Part 1: enumerator -----------------------------


def test_enumerator() -> None:
    with tempfile.TemporaryDirectory() as td:
        wiki_root = Path(td) / "wiki"
        wiki_root.mkdir()
        _write_wikirc(wiki_root, port=1)  # port unused by list_sources.py
        raw = wiki_root / "raw"

        # Confluence: duplicate page_id at versions 9 and 10. String comparison
        # would rank "9" above "10" and keep the stale copy.
        (raw / "conf-old.source.json").write_text(
            json.dumps({"type": "confluence", "page_id": "111", "url": "https://c/pages/111/Old",
                        "version_number": 9, "content_sha256": "x"})
        )
        (raw / "conf-new.source.json").write_text(
            json.dumps({"type": "confluence", "page_id": "111", "url": "https://c/pages/111/New",
                        "version_number": 10, "content_sha256": "y"})
        )
        # Confluence with no page_id: nothing stable to refresh by.
        (raw / "conf-broken.source.json").write_text(
            json.dumps({"type": "confluence", "url": "https://c/pages/999/x", "content_sha256": "q"})
        )
        # A type no fetcher handles.
        (raw / "mystery.source.json").write_text(
            json.dumps({"type": "teams", "url": "https://teams/x", "content_sha256": "t"})
        )
        # Not JSON at all.
        (raw / "corrupt.source.json").write_text("{not json")

        # Jira: duplicate key, older + newer updated_at.
        (raw / "PROJ-1-a.source.json").write_text(
            json.dumps({"type": "jira", "key": "PROJ-1", "url": "https://j/browse/PROJ-1",
                        "updated_at": "2026-01-01T00:00:00Z", "content_sha256": "a"})
        )
        (raw / "PROJ-1-b.source.json").write_text(
            json.dumps({"type": "jira", "key": "PROJ-1", "url": "https://j/browse/PROJ-1",
                        "updated_at": "2026-02-01T00:00:00Z", "content_sha256": "b"})
        )

        (raw / "web-example-com.source.json").write_text(
            json.dumps({"type": "web", "url": "https://example.com/docs", "content_sha256": "z"})
        )

        existing_file = Path(td) / "existing-file.txt"
        existing_file.write_text("hello")
        (raw / "local-existing.source.json").write_text(
            json.dumps({"type": "local", "path": "raw/local-existing.txt",
                        "original_path": str(existing_file), "content_sha256": "m"})
        )
        (raw / "local-missing.source.json").write_text(
            json.dumps({"type": "local", "path": "raw/local-missing.txt",
                        "original_path": "/nonexistent/does-not-exist.txt", "content_sha256": "n"})
        )

        # Slack: two shards of one channel + a thread + a search.
        (raw / "slack-general-a.source.json").write_text(
            json.dumps({"type": "slack", "channel": "general", "channel_id": "C123",
                        "fetched_until": "1735776000.000001", "content_sha256": "s1"})
        )
        (raw / "slack-general-b.source.json").write_text(
            json.dumps({"type": "slack", "channel": "general", "channel_id": "C123",
                        "fetched_until": "1736553600.000001", "content_sha256": "s2"})
        )
        (raw / "slack-general-thread.source.json").write_text(
            json.dumps({"type": "slack", "channel": "general", "channel_id": "C123",
                        "thread_ts": "123.456", "content_sha256": "s3"})
        )
        (raw / "slack-search.source.json").write_text(
            json.dumps({"type": "slack", "search_query": "foo", "channel_id": "C123",
                        "content_sha256": "s4"})
        )

        _registry(
            raw,
            [
                {"kind": "confluence_space", "query": "FOO", "options": {"limit": 0},
                 "first_job_id": "j1", "first_seen_at": "t", "last_job_id": "j1", "last_run_at": "t"},
                {"kind": "web_sitemap", "query": "https://example.com",
                 "options": {"limit": 0, "include": ["/docs/"], "exclude": [], "since": None,
                             "ignore_robots": False, "depth": None, "max_pages": None},
                 "first_job_id": "j2", "first_seen_at": "t", "last_job_id": "j2", "last_run_at": "t"},
                {"kind": "web_sitemap", "query": "https://example.com/custom-sitemap.xml.gz",
                 "options": {"limit": 5, "include": [], "exclude": [], "since": "2026-01-01",
                             "ignore_robots": False, "depth": None, "max_pages": None},
                 "first_job_id": "j3", "first_seen_at": "t", "last_job_id": "j3", "last_run_at": "t"},
            ],
        )

        r = _run_script("list_sources.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"list_sources.py failed: {r.stderr}\n{r.stdout}")
        manifest = json.loads(r.stdout)
        by_kind = {}
        for s in manifest["sources"]:
            by_kind.setdefault(s["source_kind"], []).append(s)

        _assert(
            [s["ref"] for s in by_kind["confluence"]] == ["111"],
            f"expected one Confluence target keyed by page_id: {by_kind.get('confluence')}",
        )
        _assert(
            by_kind["confluence"][0]["slug"] == "conf-new",
            f"version 10 must beat version 9 (numeric, not string, compare): {by_kind['confluence']}",
        )
        _assert([s["ref"] for s in by_kind["jira"]] == ["PROJ-1"], f"jira: {by_kind.get('jira')}")
        _assert(
            by_kind["jira"][0]["slug"] == "PROJ-1-b",
            f"newer updated_at must win: {by_kind['jira']}",
        )
        _assert(
            [s["ref"] for s in by_kind["web"]] == ["https://example.com/docs"],
            f"web: {by_kind.get('web')}",
        )
        _assert(
            [s["ref"] for s in by_kind["local"]] == [str(existing_file)],
            f"local: {by_kind.get('local')}",
        )
        _assert(
            [s["ref"] for s in by_kind["slack_channel"]] == ["C123"],
            f"two shards of one channel collapse to one target: {by_kind.get('slack_channel')}",
        )
        _assert(
            not any("--after" in json.dumps(s) for s in by_kind["slack_channel"]),
            "slack channel targets must carry no date window — the fetcher's own "
            "watermark is exact where a date is not",
        )
        _assert(
            [(s["ref"], s.get("thread_ts")) for s in by_kind["slack_thread"]] == [("C123", "123.456")],
            f"threads are refreshable by (channel_id, thread_ts): {by_kind.get('slack_thread')}",
        )

        skipped = manifest["skipped"]
        _assert(
            {d["slug"] for d in skipped["dropped_duplicates"]} == {"conf-old", "PROJ-1-a"},
            f"unexpected dedup drops: {skipped['dropped_duplicates']}",
        )
        _assert(
            {d["slug"] for d in skipped["unusable_source_json"]} == {"conf-broken"},
            f"a source with no stable id must be reported: {skipped['unusable_source_json']}",
        )
        _assert(
            {d["slug"] for d in skipped["local_missing_original"]} == {"local-missing"},
            f"a local original that is gone must be reported separately: {skipped['local_missing_original']}",
        )
        _assert(
            {d["slug"] for d in skipped["unhandled_type"]} == {"mystery"},
            f"an unknown type must be reported, not dropped: {skipped['unhandled_type']}",
        )
        _assert(
            {d["slug"] for d in skipped["unreadable_source_json"]} == {"corrupt.source.json"},
            f"unparseable source.json must be reported: {skipped['unreadable_source_json']}",
        )
        _assert(
            {d["slug"] for d in skipped["slack_searches"]} == {"slack-search"},
            f"the ad hoc search must be reported as skipped: {skipped['slack_searches']}",
        )

        # Coverage invariant: every raw/*.source.json is accounted for exactly
        # once. This is the guard against silently dropping sources.
        on_disk = {p.name[: -len(".source.json")] for p in raw.glob("*.source.json")}
        accounted: list = []
        for s in manifest["sources"]:
            # A Slack channel target stands in for every shard of that channel,
            # so it accounts for all of them, not just its representative slug.
            accounted.extend(s.get("covers_slugs") or [s["slug"]])
        for bucket in skipped.values():
            for entry in bucket:
                slug = entry.get("slug")
                if slug:
                    accounted.append(slug[: -len(".source.json")] if slug.endswith(".source.json") else slug)
        _assert(
            len(accounted) == len(set(accounted)),
            f"a source.json was counted twice: {sorted(accounted)}",
        )
        _assert(
            set(accounted) == on_disk,
            f"every source.json must appear exactly once.\n"
            f"missing: {sorted(on_disk - set(accounted))}\n"
            f"unexpected: {sorted(set(accounted) - on_disk)}",
        )

        by_query = {q["query"]: q for q in manifest["bulk_queries"]}
        _assert(
            by_query["FOO"]["discover_args"] == ["--space", "FOO"],
            f"space replay flags (and no --replace): {by_query['FOO']}",
        )
        _assert(
            by_query["https://example.com"]["discover_args"][:2] == ["--site", "https://example.com"],
            f"bare site URL replays via --site: {by_query['https://example.com']}",
        )
        _assert(
            "--include" in by_query["https://example.com"]["discover_args"],
            f"--include must carry over: {by_query['https://example.com']}",
        )
        sitemap_args = by_query["https://example.com/custom-sitemap.xml.gz"]["discover_args"]
        _assert(
            sitemap_args[:2] == ["--sitemap", "https://example.com/custom-sitemap.xml.gz"],
            f"exact sitemap URL replays via --sitemap: {sitemap_args}",
        )
        _assert("--since" in sitemap_args and "2026-01-01" in sitemap_args, f"--since: {sitemap_args}")
        _assert(
            "--replace" not in json.dumps(manifest["bulk_queries"]),
            "refresh merges into one queue, so no query is rebuilt with --replace",
        )

        print("[OK] list_sources.py: numeric dedup, full coverage, slack collapse, replay flags")


# --------------------------- Part 2: registry -------------------------------


def test_registry_robustness() -> None:
    with tempfile.TemporaryDirectory() as td:
        wiki_root = Path(td) / "wiki"
        wiki_root.mkdir()
        _write_wikirc(wiki_root, port=1)
        raw = wiki_root / "raw"
        registry = raw / REGISTRY_NAME

        # Truncated JSON holding three real queries: must be a hard error and
        # must NOT be rewritten — silently replacing it loses all three.
        truncated = '{"version":1,"queries":[{"kind":"confluence_space","query":"A"},' \
                    '{"kind":"jira_jql","query":"B"},{"kind":"confluence_space","query":"C"}'
        registry.write_text(truncated)
        r = _run_script("list_sources.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"a broken registry must not sink the whole manifest: {r.stderr}")
        manifest = json.loads(r.stdout)
        _assert("registry_error" in manifest, f"expected registry_error in the manifest: {manifest}")
        _assert(registry.read_text() == truncated, "a malformed registry must be left untouched")

        # A JSON list instead of an object used to raise AttributeError.
        registry.write_text("[]")
        r = _run_script("list_sources.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"non-dict registry must be handled: {r.stderr}")
        _assert("registry_error" in json.loads(r.stdout), "non-dict registry must be reported")
        _assert("AttributeError" not in r.stderr, f"must not crash: {r.stderr}")

        # An unknown kind is refused rather than silently skipped.
        _registry(raw, [{"kind": "bogus_kind", "query": "X", "options": {}}])
        r = _run_script("list_sources.py", ["--wiki-root", str(wiki_root)])
        _assert("registry_error" in json.loads(r.stdout), "unknown kind must be reported")

        print("[OK] registry: malformed is a reported error, never a silent wipe")


def test_registry_write_timing_and_backfill() -> None:
    _VersionedConfluenceHandler.pages = {"1": {"title": "Alpha", "body": "alpha body", "version": 1}}
    srv, port = _serve(_VersionedConfluenceHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port)
            registry = wiki_root / "raw" / REGISTRY_NAME

            _assert(not registry.exists(), "registry must not exist before any discovery")

            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover (new query) failed: {r.stderr}\n{r.stdout}")
            _assert(registry.exists(), "first discovery of a new query registers it")
            entry = json.loads(registry.read_text())["queries"][0]
            _assert(entry["kind"] == "confluence_space" and entry["query"] == "FOO", f"entry: {entry}")

            # Reuse: already registered, so the committed file must not churn.
            before = registry.read_text()
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover (reuse) failed: {r.stderr}\n{r.stdout}")
            _assert(registry.read_text() == before, "reuse of a registered query must not rewrite the file")

            # Reuse when NOT yet registered must register it — this is what
            # makes refresh work on wikis whose queries predate the registry.
            registry.unlink()
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover (reuse, unregistered) failed: {r.stderr}\n{r.stdout}")
            _assert(
                registry.exists() and json.loads(registry.read_text())["queries"][0]["query"] == "FOO",
                "a reused-but-unregistered query must be registered on the reuse path",
            )

            # --replace upserts rather than duplicating.
            first_seen = json.loads(registry.read_text())["queries"][0]["first_seen_at"]
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO", "--replace"])
            _assert(r.returncode == 0, f"discover (--replace) failed: {r.stderr}\n{r.stdout}")
            queries = json.loads(registry.read_text())["queries"]
            _assert(len(queries) == 1, f"--replace must upsert, not duplicate: {queries}")
            _assert(queries[0]["first_seen_at"] == first_seen, f"first_seen_at must survive: {queries[0]}")

            # Backfill: registry wiped but the local job queue survives.
            registry.unlink()
            r = _run_script("list_sources.py", ["--wiki-root", str(wiki_root)])
            _assert(r.returncode == 0, f"list_sources failed: {r.stderr}")
            manifest = json.loads(r.stdout)
            _assert(
                [q["query"] for q in manifest["bulk_queries"]] == ["FOO"],
                f"a query known only from .wiki-state must be backfilled: {manifest}",
            )
            _assert(
                manifest.get("backfilled_bulk_queries"),
                f"the backfill must be reported: {manifest.get('backfilled_bulk_queries')}",
            )
            _assert(registry.exists(), "backfill persists the recovered query")

            print("[OK] registry: registered on reuse, upserted on --replace, backfilled from .wiki-state")
    finally:
        srv.shutdown()


# ------------------------- Part 3: wiki_status carry-over -------------------


class _VersionedConfluenceHandler(http.server.BaseHTTPRequestHandler):
    """Minimal Confluence replica with a *configurable* per-page version.

    smoke_bulk_ingest.py's mock hardcodes version=1 for every page, which
    can't exercise fetch_confluence.py's version pre-check reporting a real
    content change. This one keeps version in `pages[page_id]["version"]` so
    a test can bump it between rounds.
    """

    pages: dict = {}

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/rest/api/content":
            start = int((qs.get("start") or ["0"])[0])
            limit = int((qs.get("limit") or ["50"])[0])
            all_ids = sorted(self.pages.keys())
            slice_ = all_ids[start : start + limit]
            self._send_json(
                200,
                {
                    "results": [{"id": pid, "title": self.pages[pid]["title"]} for pid in slice_],
                    "size": len(slice_),
                    "start": start,
                    "limit": limit,
                },
            )
            return

        prefix = "/rest/api/content/"
        if path.startswith(prefix):
            page_id = path[len(prefix):]
            if page_id not in self.pages:
                self._send_json(404, {"message": "not found"})
                return
            page = self.pages[page_id]
            self._send_json(
                200,
                {
                    "id": page_id,
                    "title": page["title"],
                    "version": {"number": page.get("version", 1)},
                    "space": {"key": "FOO"},
                    "body": {"storage": {"value": f"<p>{page['body']}</p>"}},
                },
            )
            return

        self._send_json(404, {"message": "unknown path", "path": path})


def _mark_wiki_done(wiki_root: Path, job_id: str, ref: str) -> None:
    r = _run_script(
        "queue_admin.py",
        ["--wiki-root", str(wiki_root), "mark", job_id, "--ref", ref, "--wiki-done"],
    )
    _assert(r.returncode == 0, f"mark --wiki-done failed for {ref}: {r.stderr}")


def _queue(wiki_root: Path, job_id: str) -> dict:
    r = _run_script("queue_admin.py", ["--wiki-root", str(wiki_root), "show", job_id])
    _assert(r.returncode == 0, f"queue show failed: {r.stderr}")
    return json.loads(r.stdout)


def test_wiki_status_carryover() -> None:
    _VersionedConfluenceHandler.pages = {
        "1": {"title": "Alpha", "body": "alpha body", "version": 1},
        "2": {"title": "Beta", "body": "beta body", "version": 1},
    }
    srv, port = _serve(_VersionedConfluenceHandler)

    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port)

            # Round 1: discover + prefetch both pages, mark both wiki-done —
            # simulates a fully-synthesized, previously-completed bulk job.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover round 1 failed: {r.stderr}\n{r.stdout}")
            job_id = json.loads(r.stdout)["job_id"]

            r = _run_script(
                "prefetch.py",
                ["--wiki-root", str(wiki_root), "--job-id", job_id, "--skip-images"],
            )
            _assert(r.returncode == 0, f"prefetch round 1 failed: {r.stderr}\n{r.stdout}")

            _mark_wiki_done(wiki_root, job_id, "1")
            _mark_wiki_done(wiki_root, job_id, "2")

            # Mutate state before round 2: page 1 unchanged, page 2's content
            # AND version bump (a real change — matches how Confluence itself
            # would report it), plus a brand-new page 3.
            _VersionedConfluenceHandler.pages["2"] = {"title": "Beta", "body": "beta body v2", "version": 2}
            _VersionedConfluenceHandler.pages["3"] = {"title": "Gamma", "body": "gamma body", "version": 1}

            # Round 2: --replace re-enumerates (page 1, 2, 3) and must stamp
            # prior_wiki_status="done" onto refs 1 and 2 (both were wiki-done
            # before) but NOT onto ref 3 (brand new).
            r = _run_script(
                "discover.py",
                ["--wiki-root", str(wiki_root), "--space", "FOO", "--replace"],
            )
            _assert(r.returncode == 0, f"discover round 2 (--replace) failed: {r.stderr}\n{r.stdout}")

            q = _queue(wiki_root, job_id)
            prior_by_ref = {i["ref"]: i.get("prior_wiki_status") for i in q["items"]}
            _assert(prior_by_ref.get("1") == "done", f"ref 1 should carry prior_wiki_status=done: {prior_by_ref}")
            _assert(prior_by_ref.get("2") == "done", f"ref 2 should carry prior_wiki_status=done: {prior_by_ref}")
            _assert(prior_by_ref.get("3") is None, f"brand-new ref 3 must not carry a prior status: {prior_by_ref}")

            # Prefetch round 2: page 1 -> unchanged (version pre-check hits),
            # page 2 -> done (version bumped, real refetch), page 3 -> done (new).
            r = _run_script(
                "prefetch.py",
                ["--wiki-root", str(wiki_root), "--job-id", job_id, "--skip-images"],
            )
            _assert(r.returncode == 0, f"prefetch round 2 failed: {r.stderr}\n{r.stdout}")

            by_ref = {i["ref"]: i for i in _queue(wiki_root, job_id)["items"]}

            _assert(by_ref["1"]["raw_status"] == "unchanged", f"ref 1 should be unchanged: {by_ref['1']}")
            _assert(
                by_ref["1"]["wiki_status"] == "done",
                f"ref 1 (unchanged, previously wiki-done) must KEEP wiki_status=done, not reset to pending: {by_ref['1']}",
            )

            _assert(by_ref["2"]["raw_status"] == "done", f"ref 2 (content+version changed) should be raw_status=done: {by_ref['2']}")
            _assert(
                by_ref["2"]["wiki_status"] == "pending",
                f"ref 2 actually changed — must be re-queued for synthesis (wiki_status=pending): {by_ref['2']}",
            )
            _assert(
                by_ref["2"].get("prior_wiki_status") is None,
                f"a changed fetch must retire the carry-over hint: {by_ref['2']}",
            )

            _assert(by_ref["3"]["raw_status"] == "done", f"ref 3 (brand new) should be raw_status=done: {by_ref['3']}")
            _assert(
                by_ref["3"]["wiki_status"] == "pending",
                f"ref 3 (brand new) must need synthesis (wiki_status=pending): {by_ref['3']}",
            )

            pending_refs = {i["ref"] for i in _queue(wiki_root, job_id)["items"]
                            if i["raw_status"] in {"done", "unchanged"} and i["wiki_status"] == "pending"}
            _assert(pending_refs == {"2", "3"}, f"pending_wiki() should only select the genuinely new/changed refs: {pending_refs}")

            print("[OK] wiki_status carry-over: unchanged stays done, changed/new stay pending")
    finally:
        srv.shutdown()


def test_carryover_survives_retry_after_failure() -> None:
    """A changed page must stay pending synthesis even if a retry sees "unchanged".

    prefetch marks an item failed *after* the fetcher has already written the
    new raw bytes (image download / description failure). `--resume` implies
    --retry-failed, so the retry refetches, the content now matches upstream,
    and the fetcher reports "unchanged" — which used to restore
    wiki_status="done" for a page whose wiki side was never re-synthesized.
    """
    _VersionedConfluenceHandler.pages = {"1": {"title": "Alpha", "body": "v1 body", "version": 1}}
    srv, port = _serve(_VersionedConfluenceHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port)

            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover failed: {r.stderr}\n{r.stdout}")
            job_id = json.loads(r.stdout)["job_id"]
            _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", job_id, "--skip-images"])
            _mark_wiki_done(wiki_root, job_id, "1")

            # Page 1 genuinely changes upstream.
            _VersionedConfluenceHandler.pages["1"] = {"title": "Alpha", "body": "v2 CHANGED body", "version": 2}
            _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO", "--replace"])
            _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", job_id, "--skip-images"])

            _assert(
                "v2 CHANGED" in (wiki_root / "raw" / "alpha.md").read_text(),
                "the refetch should have written the new content to raw/",
            )

            # Simulate the post-write failure: raw is current, item is failed.
            qp = wiki_root / ".wiki-state" / "bulk-jobs" / job_id / "queue.json"
            data = json.loads(qp.read_text())
            data["items"][0]["raw_status"] = "failed"
            data["items"][0]["last_error"] = "extract_images failed: boom"
            qp.write_text(json.dumps(data, indent=2))

            r = _run_script(
                "prefetch.py",
                ["--wiki-root", str(wiki_root), "--job-id", job_id, "--skip-images", "--retry-failed"],
            )
            _assert(r.returncode == 0, f"retry failed: {r.stderr}\n{r.stdout}")
            item = json.loads(qp.read_text())["items"][0]
            _assert(
                item["wiki_status"] == "pending",
                "a page whose raw content changed must stay queued for synthesis after a "
                f"retry reports unchanged, got {item}",
            )

            print("[OK] carry-over is retired by a changed fetch (no silent wiki-done on retry)")
    finally:
        srv.shutdown()


# ------------------------- Part 4: end-to-end refresh -----------------------


class _WebHandler(http.server.BaseHTTPRequestHandler):
    """Serves one page whose body the test can mutate between rounds."""

    body = "<html><head><title>Doc</title></head><body><p>original web body</p></body></html>"

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            payload = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = type(self).body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_refresh_end_to_end() -> None:
    """The real thing: fetch every known source, diff it, queue only changes."""
    _VersionedConfluenceHandler.pages = {
        "1": {"title": "Alpha", "body": "alpha body", "version": 1},
        "2": {"title": "Beta", "body": "beta body", "version": 1},
        "4": {"title": "Delta", "body": "delta body", "version": 1},
    }
    _WebHandler.body = "<html><head><title>Doc</title></head><body><p>original web body</p></body></html>"
    conf_srv, conf_port = _serve(_VersionedConfluenceHandler)
    web_srv, web_port = _serve(_WebHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, conf_port)
            web_url = f"http://127.0.0.1:{web_port}/docs/page"

            # --- Set the wiki up: a space ingested in bulk, plus one
            # individually-ingested web page and one local file.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"initial discover failed: {r.stderr}\n{r.stdout}")
            bulk_job = json.loads(r.stdout)["job_id"]
            r = _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", bulk_job, "--skip-images"])
            _assert(r.returncode == 0, f"initial prefetch failed: {r.stderr}\n{r.stdout}")

            r = _run_script("fetch_web.py", ["--wiki-root", str(wiki_root), "--url", web_url])
            _assert(r.returncode == 0, f"initial web fetch failed: {r.stderr}\n{r.stdout}")

            # .resolve() because fetch_local.py records a resolved absolute
            # path, and on macOS /var is a symlink to /private/var.
            local_file = (Path(td) / "notes.md").resolve()
            local_file.write_text("# Notes\n\noriginal local body\n")
            r = _run_script("fetch_local.py", ["--wiki-root", str(wiki_root), "--path", str(local_file)])
            _assert(r.returncode == 0, f"initial local fetch failed: {r.stderr}\n{r.stdout}")

            # --- Change the world: page 2 changes, page 3 appears, page 4
            # disappears, the web page changes, the local file changes.
            _VersionedConfluenceHandler.pages["2"] = {"title": "Beta", "body": "beta body v2", "version": 2}
            _VersionedConfluenceHandler.pages["3"] = {"title": "Gamma", "body": "gamma body", "version": 1}
            del _VersionedConfluenceHandler.pages["4"]
            _WebHandler.body = "<html><head><title>Doc</title></head><body><p>UPDATED web body</p></body></html>"
            local_file.write_text("# Notes\n\nUPDATED local body\n")

            # --- Phase A: refresh discovery.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh"])
            _assert(r.returncode == 0, f"refresh discovery failed: {r.stderr}\n{r.stdout}")
            disco = json.loads(r.stdout)
            _assert(disco["status"] == "ready", f"expected status=ready: {disco}")
            _assert(disco["job_id"] == "refresh", f"refresh uses a fixed job id: {disco}")

            refs = {(i["source_kind"], i["ref"]) for i in _queue(wiki_root, "refresh")["items"]}
            _assert(("confluence", "1") in refs and ("confluence", "2") in refs, f"known pages: {refs}")
            _assert(
                ("confluence", "3") in refs,
                f"a page ADDED upstream must be discovered by the re-enumeration: {refs}",
            )
            _assert(("web", web_url) in refs, f"the individually-ingested web page: {refs}")
            _assert(("local", str(local_file)) in refs, f"the local file: {refs}")
            _assert(
                sum(1 for k, _ in refs if k == "confluence") == 4,
                f"pages 1,2,3 plus the now-deleted 4 (still in raw/): {refs}",
            )
            _assert(
                [g["ref"] for g in disco["disappeared_upstream"]] == ["4"],
                f"page 4 vanished upstream and must be reported: {disco['disappeared_upstream']}",
            )

            # --- Phase B: really fetch everything and diff it.
            r = _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", "refresh", "--skip-images"])
            _assert(r.returncode == 0, f"refresh prefetch failed: {r.stderr}\n{r.stdout}")
            items = {i["ref"]: i for i in _queue(wiki_root, "refresh")["items"]}

            _assert(items["1"]["raw_status"] == "unchanged", f"page 1 did not change: {items['1']}")
            _assert(items["1"]["wiki_status"] == "done", f"page 1 needs no synthesis: {items['1']}")
            _assert(items["2"]["raw_status"] == "done", f"page 2 changed: {items['2']}")
            _assert(items["2"]["wiki_status"] == "pending", f"page 2 needs synthesis: {items['2']}")
            _assert(items["3"]["raw_status"] == "done", f"page 3 is new: {items['3']}")
            _assert(items["3"]["wiki_status"] == "pending", f"page 3 needs synthesis: {items['3']}")
            _assert(items["4"]["raw_status"] == "failed", f"page 4 is gone upstream (404): {items['4']}")

            _assert(
                items[web_url]["raw_status"] == "done" and items[web_url]["wiki_status"] == "pending",
                f"the changed web page must be refetched and queued: {items[web_url]}",
            )
            _assert(
                "UPDATED web body" in (wiki_root / "raw" / f"{items[web_url]['slug']}.md").read_text(),
                "the web page's raw copy must hold the new content",
            )
            local_item = items[str(local_file)]
            _assert(
                local_item["raw_status"] == "done" and local_item["wiki_status"] == "pending",
                f"the changed local file must be re-ingested and queued: {local_item}",
            )

            pending = {ref for ref, i in items.items()
                       if i["raw_status"] in {"done", "unchanged"} and i["wiki_status"] == "pending"}
            _assert(
                pending == {"2", "3", web_url, str(local_file)},
                f"only genuinely changed/new sources reach synthesis: {sorted(pending)}",
            )

            # --- A second refresh with nothing changed upstream is a no-op.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh", "--replace"])
            _assert(r.returncode == 0, f"second refresh discovery failed: {r.stderr}\n{r.stdout}")
            r = _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id", "refresh", "--skip-images"])
            _assert(r.returncode == 0, f"second refresh prefetch failed: {r.stderr}\n{r.stdout}")
            items2 = {i["ref"]: i for i in _queue(wiki_root, "refresh")["items"]}
            still_pending = {ref for ref, i in items2.items()
                             if i["raw_status"] in {"done", "unchanged"} and i["wiki_status"] == "pending"}
            _assert(
                not still_pending,
                f"a refresh with no upstream changes must queue nothing: {sorted(still_pending)}",
            )

            print("[OK] end-to-end refresh: unchanged / changed / added / removed all classified")
    finally:
        conf_srv.shutdown()
        web_srv.shutdown()


def test_refresh_resumable_and_confirmation() -> None:
    _VersionedConfluenceHandler.pages = {
        str(i): {"title": f"P{i}", "body": f"body {i}", "version": 1} for i in range(1, 6)
    }
    srv, port = _serve(_VersionedConfluenceHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port)

            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--space", "FOO"])
            _assert(r.returncode == 0, f"discover failed: {r.stderr}\n{r.stdout}")
            _run_script("prefetch.py", ["--wiki-root", str(wiki_root), "--job-id",
                                        json.loads(r.stdout)["job_id"], "--skip-images"])

            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh"])
            _assert(json.loads(r.stdout)["status"] == "ready", f"first refresh: {r.stdout}")

            # Leave it half-done, then re-run: it must resume, not re-enumerate.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh"])
            payload = json.loads(r.stdout)
            _assert(
                payload["status"] == "resumable" and payload["job_id"] == "refresh",
                f"an unfinished refresh must be continued, not rebuilt: {payload}",
            )

            # --replace forces a rebuild.
            r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh", "--replace"])
            _assert(json.loads(r.stdout)["status"] == "ready", f"--replace must rebuild: {r.stdout}")

            print("[OK] refresh: an unfinished run resumes instead of re-enumerating")
    finally:
        srv.shutdown()


def test_confirmation_threshold() -> None:
    """Past the threshold, discovery stops and hands the decision back."""
    with tempfile.TemporaryDirectory() as td:
        wiki_root = Path(td) / "wiki"
        wiki_root.mkdir()
        _write_wikirc(wiki_root, port=1)
        raw = wiki_root / "raw"
        for i in range(205):
            (raw / f"web-{i}.source.json").write_text(
                json.dumps({"type": "web", "url": f"https://example.com/{i}", "content_sha256": f"h{i}"})
            )

        # A bare run enumerates, then stops before fetching anything.
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"bare ingest failed: {r.stderr}\n{r.stdout}")
        _assert(
            "needs_confirmation" in r.stdout,
            f"205 sources must trip the confirmation gate: {r.stdout[:800]}",
        )
        _assert(
            '"phase": "prefetch"' not in r.stdout,
            f"ingest.py must not start fetching before confirmation: {r.stdout[:800]}",
        )

        # Running it again must ask again. The queue is now on disk and
        # resumable, and without the gate applying there too a second bare run
        # would silently start the sweep the user never approved.
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"second bare ingest failed: {r.stderr}\n{r.stdout}")
        _assert(
            "needs_confirmation" in r.stdout and '"phase": "prefetch"' not in r.stdout,
            f"a repeat bare run must still ask, not silently proceed: {r.stdout[:800]}",
        )

        # discover.py reports it the same way on its own.
        r = _run_script("discover.py", ["--wiki-root", str(wiki_root), "--refresh", "--replace"])
        _assert(
            json.loads(r.stdout)["status"] == "needs_confirmation",
            f"discover.py --refresh must gate too: {r.stdout[:400]}",
        )
        print("[OK] refresh: large wikis stop for confirmation before any fetching")


# ------------------------- Part 5: Slack watermark --------------------------


def test_slack_committed_watermark() -> None:
    """The window floor survives a fresh clone, at full precision.

    Slack's API base URL is a module constant, so this drives fetch_slack.main()
    in-process with the network calls stubbed — that still exercises the real
    precedence chain (--oldest-ts → --after → .wiki-state → committed
    fetched_until) rather than just the lookup helper in isolation.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import fetch_slack
    finally:
        sys.path.pop(0)

    watermark = 1736553600.000123
    captured: dict = {}

    def _fake_resolve(session, limiter, channel):
        return "C123", "general"

    def _fake_channel_messages(session, limiter, channel_id, oldest_ts, latest_ts, limit):
        captured["oldest_ts"] = oldest_ts
        return [], False

    original = (fetch_slack._resolve_channel_id, fetch_slack._fetch_channel_messages)
    fetch_slack._resolve_channel_id = _fake_resolve
    fetch_slack._fetch_channel_messages = _fake_channel_messages
    try:
        with tempfile.TemporaryDirectory() as td:
            wiki_root = Path(td) / "wiki"
            wiki_root.mkdir()
            _write_wikirc(wiki_root, port=1)
            (wiki_root / "raw" / "slack-general-x.source.json").write_text(
                json.dumps({"type": "slack", "channel": "general", "channel_id": "C123",
                            "fetched_until": f"{watermark:.6f}", "content_sha256": "s"})
            )
            # No .wiki-state/ at all — the fresh-clone case.
            _assert(not (wiki_root / ".wiki-state").exists(), "test setup: no local watermark")

            argv = sys.argv
            sys.argv = ["fetch_slack.py", "--wiki-root", str(wiki_root), "--channel", "C123"]
            try:
                rc = fetch_slack.main()
            finally:
                sys.argv = argv
            _assert(rc == 0, f"fetch_slack.main() returned {rc}")
            _assert(
                captured.get("oldest_ts") == watermark,
                "with no local watermark the floor must come from the committed "
                f"fetched_until at full precision, got {captured.get('oldest_ts')!r}",
            )

            # A local watermark, when present, still wins (it's newer).
            newer = watermark + 500
            state = wiki_root / ".wiki-state"
            state.mkdir(exist_ok=True)
            (state / "last-fetched.json").write_text(
                json.dumps({"slack-channel-C123": {"fetched_until": newer}})
            )
            captured.clear()
            sys.argv = ["fetch_slack.py", "--wiki-root", str(wiki_root), "--channel", "C123"]
            try:
                fetch_slack.main()
            finally:
                sys.argv = argv
            _assert(
                captured.get("oldest_ts") == newer,
                f"the local watermark must take precedence, got {captured.get('oldest_ts')!r}",
            )

            # An explicit --oldest-ts beats both.
            captured.clear()
            sys.argv = [
                "fetch_slack.py", "--wiki-root", str(wiki_root),
                "--channel", "C123", "--oldest-ts", "1700000000.5",
            ]
            try:
                fetch_slack.main()
            finally:
                sys.argv = argv
            _assert(
                captured.get("oldest_ts") == 1700000000.5,
                f"--oldest-ts must win, got {captured.get('oldest_ts')!r}",
            )

            print("[OK] slack: committed fetched_until is the fresh-clone watermark")
    finally:
        fetch_slack._resolve_channel_id, fetch_slack._fetch_channel_messages = original


# --------------------------- Part 6: dispatch -------------------------------


def test_ingest_dispatch() -> None:
    with tempfile.TemporaryDirectory() as td:
        wiki_root = Path(td) / "wiki"
        wiki_root.mkdir()
        _write_wikirc(wiki_root, port=1)

        # Empty wiki: refresh finds nothing and says so instead of erroring.
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root)])
        _assert(r.returncode == 0, f"bare ingest.py should dispatch to refresh: {r.stderr}\n{r.stdout}")
        _assert('"mode": "refresh"' in r.stdout, f"expected mode=refresh, got: {r.stdout[:400]}")
        _assert('"status": "empty"' in r.stdout, f"expected status=empty on a fresh wiki: {r.stdout[:400]}")

        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--refresh-all"])
        _assert(r.returncode == 0, f"--refresh-all should behave like a bare run: {r.stderr}\n{r.stdout}")
        _assert('"mode": "refresh"' in r.stdout, "expected mode=refresh")

        for extra, why in (
            (["--source", "https://example.com/x"], "--source"),
            (["--space", "FOO"], "a bulk flag"),
            (["--resume", "some-job"], "--resume"),
            (["--commit-only", "--slug", "x"], "--commit-only"),
            (["--push-only"], "--push-only"),
        ):
            r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--refresh-all", *extra])
            _assert(r.returncode != 0, f"--refresh-all with {why} must be rejected: {r.stdout}")
            _assert("cannot be combined" in r.stderr, f"expected a clear conflict error for {why}: {r.stderr!r}")

        # An empty --source is a caller bug, not "refresh everything".
        r = _run_script("ingest.py", ["--wiki-root", str(wiki_root), "--source", ""])
        _assert(r.returncode != 0, "an empty --source must be rejected, not treated as a refresh")
        _assert("empty" in r.stderr, f"expected an explicit empty-source error: {r.stderr!r}")

        print("[OK] ingest.py dispatch: bare == --refresh-all, conflicting flags rejected")


# ------------------------------- Entrypoint ---------------------------------


def main() -> int:
    test_enumerator()
    test_registry_robustness()
    test_registry_write_timing_and_backfill()
    test_wiki_status_carryover()
    test_carryover_survives_retry_after_failure()
    test_refresh_end_to_end()
    test_refresh_resumable_and_confirmation()
    test_confirmation_threshold()
    test_slack_committed_watermark()
    test_ingest_dispatch()
    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

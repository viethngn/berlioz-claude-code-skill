#!/usr/bin/env python3
"""Smoke test for single-item ingest: synthesis context, source_date, images.

`context` tells the session which wiki pages a source relates to and, for a
changed source, which pages cite it and the raw diff. `source_date` never
turns an unchanged source into `changed`.

Images are off by default and no script calls a description endpoint. When
enabled, `do_image_diff_loop` only reports which images still need a
description (`pending_images`); the session writes `<stem>.md` and records it
with `image_manifest.py mark-described`.

Run:
    ~/.llm-wiki-venv/bin/python3 plugins/llm-wiki/tests/smoke_ingest.py

Exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "ingest" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
import ingest  # noqa: E402

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def make_wiki(tmp: Path, wikirc: dict | None = None) -> Path:
    """A git-initialised wiki with a minimal .wikirc.json (extra keys merged in)."""
    root = tmp / "wiki-root"
    (root / "raw").mkdir(parents=True)
    (root / "wiki").mkdir()
    rc = {"wiki_root": ".", "raw_dir": "raw", "wiki_dir": "wiki", "auto_commit": False}
    rc.update(wikirc or {})
    (root / ".wikirc.json").write_text(json.dumps(rc, indent=2))
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.invalid"],
        ["config", "user.name", "smoke"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)
    return root


def run_script(script: str, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *args], capture_output=True, text=True
    )


def serve(files: dict) -> tuple[http.server.HTTPServer, str]:
    """Serve `files` ({path: bytes}, mutable) on 127.0.0.1; returns (server, base URL)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: ARG002
            pass

        def do_GET(self):  # noqa: N802
            body = files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def write_page(root: Path, slug: str, body: str, sources=("raw/a.md",), summary="s", title=None) -> Path:
    path = root / "wiki" / f"{slug}.md"
    src = "\n".join(f"- `{s}`" for s in sources)
    path.write_text(
        f"# {title or slug}\n\n**Summary**: {summary}\n\n**Sources**:\n{src}\n\n"
        f"**Last updated**: 2026-01-01\n\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def run_ingest(root: Path, *flags: str) -> dict:
    proc = run_script("ingest.py", ["--wiki-root", str(root), *flags])
    _assert(proc.returncode == 0, f"ingest.py failed: {proc.stderr}")
    return json.loads(proc.stdout)


def test_default_off_and_legacy_config(tmp: Path) -> None:
    root = make_wiki(tmp, wikirc={"nano_banana": {"base_url": "x", "api_key": "sk-legacy-secret"}})
    cfg = config.load_config(root)
    _assert(cfg.describe_images is False, "off by default; legacy nano_banana block tolerated")
    _assert(not hasattr(cfg, "nano_banana_key"), "nano_banana accessors removed")
    _assert("sk-legacy-secret" not in json.dumps(cfg.redacted()), "legacy key never printed")
    _assert(not (SCRIPTS_DIR / "describe_image.py").exists(), "describe_image.py deleted")
    reqs = (PLUGIN_ROOT / "requirements.txt").read_text()
    _assert("google-genai" not in reqs and "pillow" not in reqs, "deps removed")
    print("[OK] images off by default, legacy nano_banana tolerated, endpoint removed")


def test_pending_then_marked(tmp: Path) -> None:
    root = make_wiki(tmp, wikirc={"images": {"describe": True}})
    img_dir = root / "raw/images/doc"
    img_dir.mkdir(parents=True)
    (img_dir / "0.png").write_bytes(PNG_1X1)
    out = ingest.do_image_diff_loop(root, "doc")
    _assert([p["status"] for p in out["pending_images"]] == ["new"], f"pending: {out}")
    _assert(out["pending_images"][0]["description"].endswith("raw/images/doc/0.md"), "description path")
    _assert(ingest.do_image_diff_loop(root, "doc")["pending_images"], "still pending until marked")
    mark = ["--raw-dir", str(root / "raw"), "--slug", "doc", "mark-described", "0.png"]
    rc = run_script("image_manifest.py", mark)
    _assert(rc.returncode == 1, "mark-described refuses when description file is missing")
    (img_dir / "0.md").write_text("# Image description — 0.png\n\ntext\n")
    rc = run_script("image_manifest.py", mark)
    _assert(
        rc.returncode == 0 and ingest.do_image_diff_loop(root, "doc")["pending_images"] == [],
        "marked -> not pending",
    )
    _assert(json.loads(rc.stdout) == {"marked": ["0.png"]}, f"mark output: {rc.stdout}")
    (img_dir / "0.png").write_bytes(PNG_1X1 + b"x")
    _assert(
        ingest.do_image_diff_loop(root, "doc")["pending_images"][0]["status"] == "changed",
        "changed bytes -> pending again",
    )
    print("[OK] pending list until mark-described; changed bytes re-pend")


def test_local_ingest_images_disabled(tmp: Path) -> None:
    root = make_wiki(tmp)
    src = tmp / "note.md"
    src.write_text("# Note\n\nSome body text for the local file ingest path.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["images"] == {"enabled": False}, f"images summary: {out['images']}")
    # Flag enables images for the run; --skip-images wins over it (R7).
    src.write_text("# Note\n\nEdited body.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit", "--describe-images")
    _assert(out["images"].get("enabled") is True, f"--describe-images: {out['images']}")
    src.write_text("# Note\n\nEdited again.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit", "--describe-images", "--skip-images")
    _assert(out["images"] == {"enabled": False}, f"--skip-images wins: {out['images']}")
    # Unchanged source keeps its early return; disabled -> no image state.
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["fetch"].get("status") == "unchanged", f"unchanged: {out['fetch']}")
    _assert(out["images"] == {"enabled": False}, f"unchanged images: {out['images']}")
    # Enabled -> images left pending by an earlier run are listed again without --force.
    img_dir = root / "raw/images" / out["fetch"]["slug"]
    img_dir.mkdir(parents=True)
    (img_dir / "0.png").write_bytes(PNG_1X1)
    out = run_ingest(root, "--source", str(src), "--no-commit", "--describe-images")
    _assert(out["fetch"].get("status") == "unchanged", f"unchanged: {out['fetch']}")
    _assert(
        [p["status"] for p in out["images"].get("pending_images", [])] == ["new"],
        f"unchanged + enabled lists pending: {out['images']}",
    )
    print("[OK] local ingest: images disabled by default, flag, --skip-images and unchanged path honored")


def test_mark_described_rejects_paths(tmp: Path) -> None:
    root = make_wiki(tmp)
    img_dir = root / "raw/images/doc"
    img_dir.mkdir(parents=True)
    (root / "raw/images/x.png").write_bytes(PNG_1X1)
    (root / "raw/images/x.md").write_text("desc\n")
    for bad in ("../x.png", "sub/0.png", "..\\x.png"):
        rc = run_script("image_manifest.py", ["--raw-dir", str(root / "raw"), "--slug", "doc", "mark-described", bad])
        _assert(rc.returncode == 1, f"mark-described rejects {bad!r}")
    _assert(not (img_dir / ".manifest.json").exists(), "nothing saved")
    print("[OK] mark-described rejects path-like image names")


def test_remote_overwrite_becomes_changed(tmp: Path) -> None:
    """A re-downloaded image with new bytes (extract_images overwrite branch) is pending again."""
    files = {"/img.png": PNG_1X1}
    server, base = serve(files)
    try:
        root = make_wiki(tmp, wikirc={"web": {"min_image_bytes": 0}})
        source_json = root / "raw/doc.source.json"
        source_json.write_text(json.dumps({
            "type": "web", "url": f"{base}/page", "image_hints": [{"url": f"{base}/img.png"}],
        }))
        extract = ["--wiki-root", str(root), "--source-json", str(source_json), "--slug", "doc"]
        raw = ["--raw-dir", str(root / "raw"), "--slug", "doc"]

        rc = run_script("extract_images.py", extract)
        _assert(rc.returncode == 0 and json.loads(rc.stdout)["counts"]["downloaded_new"] == 1, rc.stdout + rc.stderr)
        (root / "raw/images/doc/0.md").write_text("old description\n")
        _assert(run_script("image_manifest.py", [*raw, "mark-described", "0.png"]).returncode == 0, "mark")
        _assert(ingest.do_image_diff_loop(root, "doc")["pending_images"] == [], "described -> not pending")

        files["/img.png"] = PNG_1X1 + b"updated diagram"
        rc = run_script("extract_images.py", extract)
        _assert(json.loads(rc.stdout)["counts"]["overwritten"] == 1, rc.stdout + rc.stderr)
        pending = ingest.do_image_diff_loop(root, "doc")["pending_images"]
        _assert([p["status"] for p in pending] == ["changed"], f"overwrite -> changed: {pending}")

        status = json.loads(run_script("image_manifest.py", [*raw, "status"]).stdout)
        _assert(status["images"]["0.png"]["status"] == "changed", f"status: {status}")
        _assert(status["summary"] == {"new": 0, "changed": 1, "unchanged": 0}, f"status: {status}")

        _assert(run_script("image_manifest.py", [*raw, "mark-described", "0.png"]).returncode == 0, "re-mark")
        _assert(ingest.do_image_diff_loop(root, "doc")["pending_images"] == [], "re-described -> not pending")
        rc = run_script("extract_images.py", extract)
        _assert(json.loads(rc.stdout)["counts"]["skipped_unchanged"] == 1, rc.stdout)
    finally:
        server.shutdown()
    print("[OK] remote image overwrite stays pending until mark-described")


def test_context_new_and_changed(tmp: Path) -> None:
    root = make_wiki(tmp)
    write_page(root, "rate-limits", "The API rate limit is 1000 per minute (source: raw/api-guide.md). " * 2,
               sources=("raw/api-guide.md",), summary="API rate limits.")
    src = tmp / "api-guide.md"
    src.write_text("# API Guide\n\n## Rate limits\n\nThe limit is 1000 per minute.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit")
    ctx = out["context"]
    _assert(ctx["status"] == "new" and ctx["related_pages"][0]["page"] == "rate-limits", f"ctx: {ctx}")
    _assert(ctx["citing_pages"] == ["rate-limits"] and ctx["raw_diff"] == "", f"citing always filled, no diff: {ctx}")
    meta = json.loads((root / "raw/api-guide.source.json").read_text())
    _assert(re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["source_date"]), "source_date present")
    git(root, "add", "-A"); git(root, "commit", "-m", "ingest: api-guide")
    src.write_text("# API Guide\n\n## Rate limits\n\nThe limit is 5000 per minute.\n")
    ctx = run_ingest(root, "--source", str(src), "--no-commit")["context"]
    _assert(ctx["status"] == "changed" and ctx["citing_pages"] == ["rate-limits"], f"changed ctx: {ctx}")
    _assert("-The limit is 1000" in ctx["raw_diff"] and "+The limit is 5000" in ctx["raw_diff"], "diff shown")
    _assert(ctx["raw_diff_truncated"] is False, "short diff not truncated")
    git(root, "add", "-A"); git(root, "commit", "-m", "ingest: api-guide v2")
    bulk = run_ingest(root, "--context", "--slug", "api-guide")
    _assert(bulk["status"] == "changed" and "+The limit is 5000" in bulk["raw_diff"], "bulk context from git history")
    _assert(bulk["citing_pages"] == ["rate-limits"], f"bulk citing: {bulk}")
    _assert(not (root / ".wiki-state/bulk-jobs").exists(), "--context never creates a queue")
    # Unchanged single ingest keeps its early return without context.
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["fetch"]["status"] == "unchanged" and "context" not in out, f"unchanged: {out}")
    print("[OK] context: related pages, citing pages and raw diff for new / changed / bulk")


def test_bulk_context_new_and_unchanged(tmp: Path) -> None:
    root = make_wiki(tmp)
    src = tmp / "notes.md"
    src.write_text("# Notes\n\n## Deploy\n\nDeploy on Fridays never, only on weekdays.\n")
    run_ingest(root, "--source", str(src), "--no-commit")
    ctx = run_ingest(root, "--context", "--slug", "notes")
    _assert(ctx["status"] == "new" and ctx["raw_diff"] == "", f"uncommitted first version: {ctx}")
    git(root, "add", "-A"); git(root, "commit", "-m", "v1")
    _assert(run_ingest(root, "--context", "--slug", "notes")["status"] == "new", "one clean commit -> new")
    src.write_text("# Notes\n\n## Deploy\n\nDeploy on Mondays.\n" + "x" * 9000 + "\n")
    run_ingest(root, "--source", str(src), "--no-commit")
    ctx = run_ingest(root, "--context", "--slug", "notes")
    _assert(ctx["status"] == "changed" and "+Deploy on Mondays." in ctx["raw_diff"], f"dirty: {ctx}")
    _assert(ctx["raw_diff_truncated"] is True and len(ctx["raw_diff"]) == ingest.RAW_DIFF_MAX_CHARS, "truncated")
    # A bulk item whose raw_status is unchanged: no diff, citing pages still listed.
    write_page(root, "deploys", "Deploys happen on weekdays (source: raw/notes.md).", sources=("raw/notes.md",))
    ctx = ingest.build_context(root, "notes", "unchanged")
    _assert(ctx["status"] == "unchanged" and ctx["raw_diff"] == "" and ctx["citing_pages"] == ["deploys"], f"{ctx}")
    print("[OK] bulk context: new / changed from git history, truncation, unchanged items")


def test_context_committed_and_cited_is_unchanged(tmp: Path) -> None:
    root = make_wiki(tmp)
    src = tmp / "notes.md"
    src.write_text("# Notes\n\n## Deploy\n\nDeploy only on weekdays.\n")
    run_ingest(root, "--source", str(src), "--no-commit")
    write_page(root, "deploys", "Deploys happen on weekdays (source: raw/notes.md).", sources=("raw/notes.md",))
    git(root, "add", "-A"); git(root, "commit", "-m", "ingest: notes")
    ctx = run_ingest(root, "--context", "--slug", "notes")
    _assert(ctx["status"] == "unchanged" and ctx["citing_pages"] == ["deploys"] and ctx["raw_diff"] == "", f"{ctx}")
    print("[OK] context without --job-id: committed, clean, cited raw -> unchanged with citing_pages")


def _related(root: Path, slug: str) -> list:
    return [h["page"] for h in ingest.build_context(root, slug, "new")["related_pages"]]


def test_related_pages_signal(tmp: Path) -> None:
    root = make_wiki(tmp)
    write_page(root, "login-timeouts", "Sessions end after inactivity.", title="Login Timeouts",
               summary="Login sessions time out after inactivity.")
    write_page(root, "code-review", "Every PR needs a description and all comments addressed.",
               title="Code Review", summary="How code review works.")
    write_page(root, "release-process", "Release notes list each description and comments.",
               title="Release Process", summary="How releases ship.")
    write_page(root, "page-1", "Front matter.", title="Page 1")
    write_page(root, "deploy-script", "Runs the deploy.", title="Deploy Script")
    raw = root / "raw"
    # Jira-shaped raw: key-prefixed H1, generated section headings.
    (raw / "PROJ-12.md").write_text(
        "# PROJ-12 — Fix login timeout\n\n**Type:** Bug\n**Status:** Open\n\n## Description\n\n"
        "Users are logged out after 5 minutes; the login timeout is too short.\n\n"
        "## Comments\n\n### Ann — 2026-01-01\n\nAgreed.\n"
    )
    (raw / "PROJ-12.source.json").write_text(json.dumps({"type": "jira", "title": "PROJ-12 — Fix login timeout"}))
    hits = _related(root, "PROJ-12")
    _assert(hits[:1] == ["login-timeouts"], f"jira: {hits}")
    _assert("code-review" not in hits and "release-process" not in hits, f"jira boilerplate matched: {hits}")
    # 3-char acronym page found through the key-stripped Jira title.
    write_page(root, "sso", "Single sign-on setup.", title="SSO")
    (raw / "PROJ-481.md").write_text("# PROJ-481 — SSO login fails for Okta users\n\n## Description\n\nOkta users cannot sign in.\n")
    (raw / "PROJ-481.source.json").write_text(json.dumps({"type": "jira", "title": "PROJ-481 — SSO login fails for Okta users"}))
    hits = _related(root, "PROJ-481")
    _assert(hits[:1] == ["sso"], f"jira sso: {hits}")
    # PDF-shaped raw: 60 generated page headings, no real mentions.
    (raw / "report.md").write_text(
        "# Quarterly Report\n\n" + "".join(f"## Page {i}\n\nLorem ipsum dolor sit amet.\n\n" for i in range(1, 61))
    )
    _assert(_related(root, "report") == [], f"pdf: {_related(root, 'report')}")
    # A `# comment` inside a fenced block is code, not a mention.
    (raw / "setup.md").write_text("# Setup\n\nInstall things.\n\n```bash\n# deploy script here\n./run\n```\n")
    _assert(_related(root, "setup") == [], f"fenced: {_related(root, 'setup')}")
    print("[OK] related_pages: Jira / PDF boilerplate and fenced code ignored")


def test_context_cli_errors(tmp: Path) -> None:
    root = make_wiki(tmp)
    for slug, msg in (("missing", "no raw file"), ("../x", "invalid slug"), ("a/b", "invalid slug")):
        proc = run_script("ingest.py", ["--wiki-root", str(root), "--context", "--slug", slug])
        _assert(proc.returncode == 1 and msg in proc.stderr and not proc.stdout, f"{slug}: {proc.stderr}")
    for flag in ("--push-only", "--commit-only"):
        proc = run_script("ingest.py", ["--wiki-root", str(root), "--context", "--slug", "x", flag])
        _assert(proc.returncode == 2 and "--context cannot be combined" in proc.stderr, f"{flag}: {proc.stderr}")
    _assert(not (root / ".wiki-state/bulk-jobs").exists(), "no queue created")
    print("[OK] --context rejects unknown / path-like slugs and commit/push modes")


def test_source_date_never_triggers_changed(tmp: Path) -> None:
    root = make_wiki(tmp)
    src = tmp / "guide.md"
    src.write_text("# Guide\n\nA guide with enough body text to matter here.\n")
    run_ingest(root, "--source", str(src), "--no-commit")
    meta_path = root / "raw/guide.source.json"
    meta = json.loads(meta_path.read_text())
    del meta["source_date"]  # a wiki ingested before source_date existed
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    git(root, "add", "-A"); git(root, "commit", "-m", "pre-change ingest")
    before = meta_path.read_text()
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["fetch"]["status"] == "unchanged", f"legacy source.json: {out['fetch']}")
    _assert(meta_path.read_text() == before, "unchanged source.json left alone")
    os.utime(src, (1_000_000_000, 1_000_000_000))
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["fetch"]["status"] == "unchanged" and meta_path.read_text() == before, "touch changes nothing")
    _assert(git(root, "status", "--porcelain", "--", "raw") == "", "raw/ clean")

    # raw_store level: same content, different / missing source_date -> unchanged.
    import raw_store
    raw = root / "raw"
    first = raw_store.write_raw_if_changed(raw, "x", "# X\n\nbody\n", {"type": "t"}, source_date="2026-01-01")
    _assert(first["status"] == "new", f"{first}")
    _assert(raw_store.write_raw_if_changed(raw, "x", "# X\n\nbody\n", {"type": "t"}, source_date="2026-02-02")["status"] == "unchanged", "date differs")
    _assert(raw_store.write_raw_if_changed(raw, "x", "# X\n\nbody\n", {"type": "t"})["status"] == "unchanged", "date missing")
    _assert(json.loads((raw / "x.source.json").read_text())["source_date"] == "2026-01-01", "kept first date")
    _assert(raw_store.write_raw_if_changed(raw, "x", "# X\n\nnew\n", {"type": "t"}, source_date="2026-03-03")["status"] == "changed", "content")
    _assert(json.loads((raw / "x.source.json").read_text())["source_date"] == "2026-03-03", "date refreshed on change")
    print("[OK] source_date: legacy source.json and touch stay unchanged")


def main() -> int:
    for test in (
        test_context_new_and_changed,
        test_bulk_context_new_and_unchanged,
        test_context_committed_and_cited_is_unchanged,
        test_related_pages_signal,
        test_context_cli_errors,
        test_source_date_never_triggers_changed,
        test_default_off_and_legacy_config,
        test_pending_then_marked,
        test_local_ingest_images_disabled,
        test_mark_described_rejects_paths,
        test_remote_overwrite_becomes_changed,
    ):
        with tempfile.TemporaryDirectory() as d:
            test(Path(d))
    print("ALL INGEST SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

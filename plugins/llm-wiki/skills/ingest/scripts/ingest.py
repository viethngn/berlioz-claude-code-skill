#!/usr/bin/env python3
"""Orchestrate an ingest — single item OR bulk (Confluence space / CQL / JQL / website).

Single-item usage:
    python3 ingest.py --wiki-root PATH --source <URL-or-key-or-path>
    python3 ingest.py --wiki-root PATH --source ... --no-commit --force

Bulk usage:
    python3 ingest.py --wiki-root PATH --space FOO
    python3 ingest.py --wiki-root PATH --cql "space=FOO AND label=onboarding"
    python3 ingest.py --wiki-root PATH --jql "project=PROJ AND updated > -30d"
    python3 ingest.py --wiki-root PATH --sitemap https://example.com/sitemap.xml
    python3 ingest.py --wiki-root PATH --site https://example.com
    python3 ingest.py --wiki-root PATH --crawl https://example.com --depth 2 --max-pages 100
    python3 ingest.py --wiki-root PATH --resume <job-id>

Auto-detection: `--source` alone will pick single vs bulk based on URL
shape. `.../pages/N` or `.../browse/KEY` → single Confluence/Jira;
`.../spaces/KEY` without a page → bulk Confluence space; a sitemap or
robots.txt URL → bulk website; any other http(s) URL → single web page.

Prints a JSON summary to stdout. Bulk mode also produces per-item JSON
lines during the prefetch phase.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

# The orchestrator is stdlib-only so it doesn't need require([...]).
# config, raw_store, and bulk_queue are stdlib-only too.
from config import ConfigError, load_config
from raw_store import write_fetch_history


SCRIPT_DIR = Path(__file__).resolve().parent
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
CONFLUENCE_SPACE_URL_RE = re.compile(r"/spaces/([A-Za-z0-9._~-]+)(?:/|$)")
# sitemap.xml, sitemap_index.xml, sitemap-1.xml.gz, /sitemaps/pages.xml, robots.txt
SITEMAP_URL_RE = re.compile(r"(^|/)(robots\.txt|sitemap[^/]*\.xml(\.gz)?|[^/]*sitemap[^/]*\.xml(\.gz)?)$", re.I)


def detect_bulk_from_url(source: str) -> Optional[Tuple[str, str]]:
    """Classify a URL as a bulk source, or return None if it's a single item.

    Returns ('confluence_space', spaceKey) for a whole Confluence space, or
    ('web_sitemap', url) for a sitemap / robots.txt URL.

    URLs with `/pages/N` or `pageId=` are always single — this function
    returns None for them. A *bare* site URL is also single (one page); bulk
    website ingest needs a sitemap-shaped URL or an explicit
    --sitemap/--site/--crawl flag, so `/ingest https://example.com` never
    accidentally enumerates a whole domain.
    """
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        return None
    path = parsed.path or ""
    query = parsed.query or ""

    if re.search(r"/pages/\d+", path) or "pageId=" in query:
        return None

    if SITEMAP_URL_RE.search(path):
        return "web_sitemap", source

    m = CONFLUENCE_SPACE_URL_RE.search(path)
    if m:
        return "confluence_space", m.group(1)

    # /display/KEY without additional path or with only "?spaceKey=" query.
    display_match = re.search(r"/display/([A-Za-z0-9._~-]+)/?$", path)
    if display_match:
        return "confluence_space", display_match.group(1)

    qs = parse_qs(query)
    if "spaceKey" in qs and not qs.get("pageId"):
        return "confluence_space", qs["spaceKey"][0]

    return None


def _host_matches_configured(netloc: str, cfg) -> bool:
    """True when a URL's host is the configured Confluence or Jira host."""
    if cfg is None:
        return False
    netloc = netloc.lower()
    for base in (cfg.confluence_base_url(), cfg.jira_base_url()):
        if base and urlparse(base).netloc.lower() == netloc:
            return True
    return False


def detect_source_type(source: str, wiki_root: Path, cfg=None) -> str:
    """Return 'confluence' | 'jira' | 'web' | 'local' for a single-item source.

    Callers should first check `detect_bulk_from_url` and any explicit bulk
    flags; this function assumes the source is single-item.
    """
    if JIRA_KEY_RE.match(source):
        return "jira"

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path or ""
        query = parsed.query or ""
        if re.search(r"/pages/\d+", path) or "pageId=" in query:
            return "confluence"
        if "/browse/" in path or "selectedIssue=" in query:
            return "jira"
        # Fallback: prefer confluence for wiki-like paths, jira for jira-like hosts
        if "confluence" in parsed.netloc.lower():
            return "confluence"
        if "jira" in parsed.netloc.lower():
            return "jira"
        # A URL on the *configured* Atlassian host that we couldn't parse is a
        # malformed Atlassian URL, not a website. Scraping it anonymously would
        # silently capture a login page, so keep erroring out.
        if _host_matches_configured(parsed.netloc, cfg):
            raise SystemExit(
                f"ERROR: {source} is on your configured Atlassian host but has no "
                "page ID or issue key. Pass an exact URL like "
                "https://.../pages/12345/ or .../browse/PROJ-1."
            )
        # Any other http(s) URL is an ordinary web page.
        return "web"

    # Not a URL — treat as a local path
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = (wiki_root / source).resolve() if wiki_root else candidate.resolve()
    if candidate.exists():
        return "local"

    raise SystemExit(
        f"ERROR: could not interpret {source!r} as a URL, Jira key, or existing file path."
    )


def run_script(script_name: str, args: list[str]) -> dict:
    cmd = [sys.executable, str(SCRIPT_DIR / script_name), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Bubble the stderr up to the caller — scripts already print friendly errors
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ERROR: {script_name} failed with exit code {proc.returncode}")
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Some scripts (e.g. describe_image.py) print a plain path
        return {"stdout": stdout}


def dispatch_fetch(source_type: str, source: str, wiki_root: Path, force: bool = False) -> dict:
    if source_type == "confluence":
        extra = ["--force"] if force else []
        return run_script(
            "fetch_confluence.py",
            ["--wiki-root", str(wiki_root), "--url", source, *extra],
        )
    if source_type == "jira":
        extra = ["--force"] if force else []
        if JIRA_KEY_RE.match(source):
            return run_script(
                "fetch_jira.py",
                ["--wiki-root", str(wiki_root), "--key", source, *extra],
            )
        return run_script(
            "fetch_jira.py",
            ["--wiki-root", str(wiki_root), "--url", source, *extra],
        )
    if source_type == "web":
        extra = ["--force"] if force else []
        return run_script(
            "fetch_web.py",
            ["--wiki-root", str(wiki_root), "--url", source, *extra],
        )
    if source_type == "local":
        return run_script(
            "fetch_local.py",
            ["--wiki-root", str(wiki_root), "--path", source],
        )
    raise SystemExit(f"ERROR: unknown source type: {source_type}")


def is_git_repo(path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def has_staged_changes(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def git_commit(wiki_root: Path, message: str) -> Optional[str]:
    subprocess.run(["git", "-C", str(wiki_root), "add", "raw", "wiki"], check=True)
    proc = subprocess.run(
        ["git", "-C", str(wiki_root), "diff", "--cached", "--quiet"],
    )
    if proc.returncode == 0:
        return None  # nothing to commit

    subprocess.run(
        ["git", "-C", str(wiki_root), "commit", "-m", message], check=True
    )
    show = subprocess.run(
        ["git", "-C", str(wiki_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return show.stdout.strip()


def git_push(wiki_root: Path, cfg) -> Optional[str]:
    """Push to configured remote. Returns "pushed" or None on skip/failure.

    Skips silently when no remote is configured. Warns but does not raise on
    push failure so the local commit is always preserved.
    """
    remote = cfg.git_remote()
    branch = cfg.git_branch()

    check = subprocess.run(
        ["git", "-C", str(wiki_root), "remote"],
        capture_output=True,
        text=True,
    )
    remotes = [r for r in check.stdout.strip().splitlines() if r]
    if remote not in remotes:
        return None  # no remote configured — skip silently

    cmd = ["git", "-C", str(wiki_root), "push", remote]
    if branch:
        cmd.append(branch)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return "pushed"
    except subprocess.CalledProcessError as e:
        print(
            f"WARNING: git push to '{remote}' failed: {e.stderr.strip() or e}",
            file=sys.stderr,
        )
        return None


def _looks_like_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    for marker in ("replace_me", "example.com", "your-", "changeme", "todo"):
        if marker in lowered:
            return True
    return False


def do_image_diff_loop(
    wiki_root: Path,
    slug: str,
) -> dict:
    """Run image manifest status, describe new/changed images, update manifest."""
    from image_manifest import (  # local import — stdlib
        classify,
        hash_file,
        load_manifest,
        scan_slug_dir,
    )

    cfg = load_config(wiki_root)
    raw_dir = cfg.raw_dir
    manifest = load_manifest(raw_dir, slug)
    scanned = scan_slug_dir(raw_dir, slug)

    if not scanned:
        return {"new": 0, "changed": 0, "unchanged": 0, "described": 0, "images": {}}

    images_dir = raw_dir / "images" / slug
    per_image = {}
    counts = {"new": 0, "changed": 0, "unchanged": 0, "described": 0, "skipped": 0}

    base_url = cfg.nano_banana_base_url()
    api_key = cfg.nano_banana_key()
    can_describe = bool(
        base_url
        and api_key
        and not _looks_like_placeholder(base_url)
        and not _looks_like_placeholder(api_key)
    )

    for image_name, sha in sorted(scanned.items()):
        status = classify(manifest, image_name, sha)
        counts[status] += 1
        image_path = images_dir / image_name
        description_file = images_dir / f"{Path(image_name).stem}.md"

        if status == "unchanged":
            per_image[image_name] = {"status": status, "sha256": sha}
            continue

        if not can_describe:
            per_image[image_name] = {
                "status": status,
                "sha256": sha,
                "described": False,
                "reason": (
                    "nano_banana.base_url or api_key is empty or looks like a placeholder "
                    "(REPLACE_ME / example.com / your-…) — description skipped"
                ),
            }
            manifest.set_entry(image_name, sha256=sha)
            manifest.save()
            counts["skipped"] += 1
            continue

        run_script(
            "describe_image.py",
            [
                "--wiki-root",
                str(wiki_root),
                "--image",
                str(image_path),
                "--output",
                str(description_file),
            ],
        )
        manifest.set_entry(
            image_name,
            sha256=sha,
            description_file=description_file.name,
        )
        manifest.mark_described(image_name)
        manifest.save()
        counts["described"] += 1
        per_image[image_name] = {
            "status": status,
            "sha256": sha,
            "description_file": description_file.name,
        }

    return {**counts, "images": per_image}


def _stream_prefetch(job_id: str, wiki_root: Path, retry_failed: bool, max_items: int, skip_images: bool) -> int:
    """Run prefetch.py as a subprocess and stream its output line-by-line.

    Returns the child's exit code.
    """
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "prefetch.py"),
        "--wiki-root",
        str(wiki_root),
        "--job-id",
        job_id,
    ]
    if retry_failed:
        cmd.append("--retry-failed")
    if max_items > 0:
        cmd.extend(["--max-items", str(max_items)])
    if skip_images:
        cmd.append("--skip-images")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise
    if proc.stderr is not None:
        err = proc.stderr.read()
        if err:
            sys.stderr.write(err)
    return rc


def _cmd_bulk(
    args: argparse.Namespace,
    kind: Optional[str],
    query: Optional[str],
    resume_job_id: Optional[str],
) -> int:
    """Dispatch to discover.py + prefetch.py.

    Either (kind, query) is set (for new/reused jobs), or resume_job_id is
    set (skip discovery, just resume prefetch).
    """
    wiki_root = args.wiki_root.resolve()
    try:
        cfg = load_config(wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    job_id: Optional[str] = resume_job_id

    if job_id is None:
        assert kind is not None and query is not None
        # Run discover.py
        discover_args = ["--wiki-root", str(cfg.wiki_root)]
        if kind == "confluence_space":
            discover_args += ["--space", query]
        elif kind == "confluence_cql":
            discover_args += ["--cql", query]
        elif kind == "jira_jql":
            discover_args += ["--jql", query]
        elif kind == "web_sitemap":
            # --site means "find the sitemap yourself"; --sitemap is an exact URL.
            discover_args += ["--site" if args.site else "--sitemap", query]
        elif kind == "web_crawl":
            discover_args += ["--crawl", query]
            if args.depth is not None:
                discover_args += ["--depth", str(args.depth)]
            if args.max_pages is not None:
                discover_args += ["--max-pages", str(args.max_pages)]
        else:
            raise SystemExit(f"ERROR: unknown bulk kind: {kind}")
        if kind in {"web_sitemap", "web_crawl"}:
            for pattern in args.include or []:
                discover_args += ["--include", pattern]
            for pattern in args.exclude or []:
                discover_args += ["--exclude", pattern]
            if args.since:
                discover_args += ["--since", args.since]
            if args.ignore_robots:
                discover_args.append("--ignore-robots")
        if args.replace:
            discover_args.append("--replace")
        if args.limit:
            discover_args += ["--limit", str(args.limit)]

        result = run_script("discover.py", discover_args)

        # No sitemap found for a --site run: surface the request for explicit
        # crawl bounds instead of guessing them.
        if result.get("status") == "needs_bounds":
            print(json.dumps({"mode": "bulk", "discover": result}, indent=2, ensure_ascii=False))
            return 0

        job_id = result.get("job_id")
        if not job_id:
            # Nothing discovered (empty result). Emit and exit gracefully.
            print(json.dumps({"mode": "bulk", "discover": result}, indent=2, ensure_ascii=False))
            return 0
        counts = result.get("counts") or {}
        print(
            json.dumps(
                {
                    "mode": "bulk",
                    "phase": "discover",
                    "job_id": job_id,
                    "kind": result.get("kind"),
                    "query": result.get("query"),
                    "counts": counts,
                    "reused": result.get("reused", False),
                    "replaced": result.get("replaced", False),
                },
                indent=2,
                ensure_ascii=False,
            ),
            flush=True,
        )

    if args.discover_only:
        return 0

    # Prefetch phase — stream so Claude/user can watch progress
    print(
        json.dumps({"mode": "bulk", "phase": "prefetch", "job_id": job_id}, ensure_ascii=False),
        flush=True,
    )
    rc = _stream_prefetch(
        job_id=job_id,
        wiki_root=cfg.wiki_root,
        retry_failed=bool(resume_job_id) or args.retry_failed,
        max_items=args.max_items,
        skip_images=args.skip_images,
    )

    # After prefetch, report queue counts to guide Claude's synthesis loop
    try:
        from bulk_queue import load_queue  # local stdlib import
        q = load_queue(cfg.wiki_root, job_id)
        counts = q.counts()
    except Exception:  # noqa: BLE001
        counts = {}
    print(
        json.dumps(
            {
                "mode": "bulk",
                "phase": "prefetch-complete",
                "job_id": job_id,
                "prefetch_exit_code": rc,
                "counts": counts,
                "next_step": (
                    "Claude: iterate items with raw_status in {done,unchanged} and "
                    "wiki_status=pending. Read raw/<slug>.md, synthesize wiki pages, "
                    "then run queue_admin.py mark <job-id> --ref <ref> --wiki-done. Commit "
                    "every --batch items."
                ),
                "batch_size": args.batch,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return rc


def _cmd_commit_only(args: argparse.Namespace) -> int:
    wiki_root = args.wiki_root.resolve()
    if not is_git_repo(wiki_root):
        print(f"ERROR: {wiki_root} is not a git repository", file=sys.stderr)
        return 1

    try:
        cfg = load_config(wiki_root)
    except ConfigError:
        cfg = None  # type: ignore[assignment]

    message = args.message or (
        f"ingest: {args.slug} ({args.new_images} new, {args.changed_images} changed images)"
    )
    commit = git_commit(wiki_root, message)
    result: dict = {"commit": commit, "message": message}
    if commit and cfg is not None and cfg.auto_push:
        result["pushed"] = git_push(wiki_root, cfg)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_push_only(args: argparse.Namespace) -> int:
    """Push already-made commits to the remote (used by /lint's grouped commits).

    Commits are created separately (e.g. one per category); this pushes them all
    in one shot, gated on auto_push. Push failures warn but never raise.
    """
    wiki_root = args.wiki_root.resolve()
    if not is_git_repo(wiki_root):
        print(f"ERROR: {wiki_root} is not a git repository", file=sys.stderr)
        return 1

    try:
        cfg = load_config(wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    result: dict = {"pushed": None}
    if cfg.auto_push:
        result["pushed"] = git_push(wiki_root, cfg)
    else:
        result["note"] = "auto_push is false — nothing pushed"
    print(json.dumps(result, indent=2))
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    wiki_root = args.wiki_root.resolve()

    try:
        cfg = load_config(wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    do_commit = cfg.auto_commit and not args.no_commit

    if do_commit and not is_git_repo(wiki_root):
        print(
            f"ERROR: {wiki_root} is not a git repo. Run `git init` or use /create-wiki first, "
            "or pass --no-commit to skip commit.",
            file=sys.stderr,
        )
        return 1

    source_type = detect_source_type(args.source, wiki_root, cfg)
    fetch_summary = dispatch_fetch(source_type, args.source, wiki_root, force=args.force)
    slug = fetch_summary.get("slug")
    if not slug:
        print(f"ERROR: fetcher for {source_type} did not report a slug", file=sys.stderr)
        return 1

    fetch_status = fetch_summary.get("status", "changed")
    if args.force and fetch_status == "unchanged":
        fetch_status = "forced"

    source_json = fetch_summary.get("source_json")

    # Diff gate: if the source is unchanged and we're not forcing, skip all
    # downstream work (image download, describe, git commit).
    if fetch_status == "unchanged" and not args.force:
        image_summary = {"new": 0, "changed": 0, "unchanged": 0, "described": 0, "skipped": 0, "images": {}}
        write_fetch_history(wiki_root, slug, "unchanged", args.source)
        result = {
            "source_type": source_type,
            "fetch": fetch_summary,
            "images": image_summary,
            "committed": None,
            "note": "content unchanged since last ingest — skipped image download, description, and commit. Pass --force to re-run everything.",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    # For Confluence/Jira/web, download image_hints before running the diff loop.
    if source_type in {"confluence", "jira", "web"} and source_json:
        image_hints = fetch_summary.get("image_hints") or []
        if image_hints:
            run_script(
                "extract_images.py",
                [
                    "--wiki-root",
                    str(wiki_root),
                    "--source-json",
                    source_json,
                    "--slug",
                    slug,
                ],
            )

    # Run the image diff loop (for local images, images are already on disk)
    image_summary = do_image_diff_loop(wiki_root, slug)

    write_fetch_history(wiki_root, slug, fetch_status, args.source)

    result = {
        "source_type": source_type,
        "fetch": fetch_summary,
        "images": image_summary,
        "committed": None,
    }

    if do_commit:
        message = (
            f"ingest: {slug} "
            f"({image_summary.get('new', 0)} new, "
            f"{image_summary.get('changed', 0)} changed images)"
        )
        commit_hash = git_commit(wiki_root, message)
        result["committed"] = {"commit": commit_hash, "message": message}
        if commit_hash and cfg.auto_push:
            result["pushed"] = git_push(wiki_root, cfg)
    else:
        result["committed"] = None
        result["note"] = (
            "auto_commit is false or --no-commit was passed — changes staged but not committed"
        )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest one source (single or bulk) into an LLM wiki")
    parser.add_argument("--wiki-root", type=Path, required=True)

    # Single-item source (URL / Jira key / local file path)
    parser.add_argument("--source", help="URL, Jira key, or local file path (single item)")

    # Bulk source flags — mutually exclusive with each other and with --source
    parser.add_argument("--space", help="Bulk: Confluence space key")
    parser.add_argument("--cql", help="Bulk: Confluence CQL query")
    parser.add_argument("--jql", help="Bulk: Jira JQL query")
    parser.add_argument("--sitemap", help="Bulk: sitemap URL (sitemap.xml, index, or .gz)")
    parser.add_argument("--site", help="Bulk: site URL — auto-discover its sitemap")
    parser.add_argument(
        "--crawl",
        help="Bulk: site URL — crawl it (requires --depth and --max-pages)",
    )
    parser.add_argument("--resume", help="Bulk: resume an existing job by id")

    # Web bulk knobs
    parser.add_argument(
        "--depth", type=int, default=None, help="Web crawl: link levels to follow"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None, help="Web crawl: hard cap on pages enumerated"
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Web bulk: keep URLs matching this regex (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Web bulk: drop URLs matching this regex (repeatable)",
    )
    parser.add_argument(
        "--since", help="Web bulk: keep sitemap entries with <lastmod> >= YYYY-MM-DD"
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Web bulk: do not enforce robots.txt (use only with permission)",
    )

    # Bulk knobs
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Bulk discovery: overwrite an existing job for the same (kind, query)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Bulk discovery: cap the number of items enumerated (0 = no cap)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Bulk: only run discovery, do not prefetch",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Bulk prefetch: process at most N items in this run (0 = no cap)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Bulk prefetch: also retry items previously marked failed (implicit with --resume)",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Bulk prefetch: skip nano-banana-pro description (still downloads image bytes)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=5,
        help="Bulk synthesis: how many items Claude should synthesize per batch before commit + user checkpoint",
    )

    # Single-item flags
    parser.add_argument("--no-commit", action="store_true", help="Single: skip the final git commit")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Single: bypass content-diff gates: re-download images, re-describe them, "
            "and commit even if the source content is unchanged"
        ),
    )

    parser.add_argument("--commit-only", action="store_true", help="Only run the git commit step")
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Only push already-made commits to the remote (gated on auto_push)",
    )
    parser.add_argument("--slug", help="Slug for --commit-only")
    parser.add_argument("--message", help="Override commit message")
    parser.add_argument("--new-images", type=int, default=0, help="For --commit-only")
    parser.add_argument("--changed-images", type=int, default=0, help="For --commit-only")

    args = parser.parse_args()

    if args.push_only:
        return _cmd_push_only(args)

    if args.commit_only:
        if not args.slug:
            parser.error("--slug is required with --commit-only")
        return _cmd_commit_only(args)

    bulk_flags = [
        ("confluence_space", args.space),
        ("confluence_cql", args.cql),
        ("jira_jql", args.jql),
        ("web_sitemap", args.sitemap),
        ("web_sitemap", args.site),
        ("web_crawl", args.crawl),
    ]
    active_bulk = [(k, v) for k, v in bulk_flags if v]
    bulk_flag_names = "--space / --cql / --jql / --sitemap / --site / --crawl"

    if args.resume:
        if active_bulk or args.source:
            parser.error(f"--resume cannot be combined with --source or {bulk_flag_names}")
        return _cmd_bulk(args, kind=None, query=None, resume_job_id=args.resume)

    if len(active_bulk) > 1:
        parser.error(f"Provide at most one of {bulk_flag_names}")

    if active_bulk:
        if args.source:
            parser.error(f"--source cannot be combined with {bulk_flag_names}")
        kind, query = active_bulk[0]
        if kind == "web_crawl" and (args.depth is None or args.max_pages is None):
            parser.error(
                "--crawl requires explicit --depth and --max-pages. Use --site to "
                "auto-discover a sitemap instead; it only asks for bounds if none exists."
            )
        return _cmd_bulk(args, kind=kind, query=query, resume_job_id=None)

    if not args.source:
        parser.error(
            f"provide --source (single) or {bulk_flag_names} / --resume (bulk), "
            "or use --commit-only"
        )

    # Auto-detect: is --source actually a bulk source URL?
    bulk_from_url = detect_bulk_from_url(args.source)
    if bulk_from_url is not None:
        kind, query = bulk_from_url
        return _cmd_bulk(args, kind=kind, query=query, resume_job_id=None)

    return _cmd_ingest(args)


if __name__ == "__main__":
    sys.exit(main())

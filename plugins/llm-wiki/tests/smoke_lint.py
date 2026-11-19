#!/usr/bin/env python3
"""Smoke tests for skills/lint/scripts/lint.py.

Run:
    python3 plugins/llm-wiki/tests/smoke_lint.py

Each test_* gets a fresh temp dir. Exits 0 on success, non-zero on failure.
Stdlib only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "lint" / "scripts" / "lint.py"


# ------------------------------- Helpers ------------------------------------


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def make_wiki(tmp: Path) -> Path:
    root = tmp / "w"
    (root / "wiki").mkdir(parents=True)
    (root / "raw").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    return root


def write_page(root: Path, slug: str, body: str, sources=("raw/a.md",), summary="s") -> Path:
    path = root / "wiki" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    title = slug.split("/")[-1]
    src = "\n".join(f"- `{s}`" for s in sources)
    path.write_text(
        f"# {title}\n\n**Summary**: {summary}\n\n**Sources**:\n{src}\n\n"
        f"**Last updated**: 2026-01-01\n\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def write_raw(root: Path, name: str, text: str) -> Path:
    path = root / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def run_lint(root: Path, *args: str, stdin: str = None) -> dict:
    out = subprocess.run(
        [sys.executable, str(LINT), "--wiki-root", str(root), *args],
        capture_output=True, text=True, input=stdin,
    )
    _assert(out.returncode == 0, f"lint.py exit {out.returncode}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def collision_fixture(tmp: Path) -> Path:
    """Two rate-limit pages that collide on 1000 vs 5000, plus an unrelated page; committed."""
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "rate-limits", "The API rate limit is 1000 requests per minute for all tenants (source: raw/a.md). "
               "Tenants are isolated by key. Bursts are smoothed over ten seconds windows always.")
    write_page(root, "api-rate-limit", "The API rate limit is 5000 requests per minute for all tenants (source: raw/a.md). "
               "Tenants are isolated by key. Bursts are smoothed over ten seconds windows always.")
    write_page(root, "unrelated", "Onboarding takes three steps and ends with a welcome email being sent.", sources=("raw/a.md",))
    commit(root, "ingest: initial")
    return root


def drift_fixture(tmp: Path) -> Path:
    """Page `drifts` cites raw/a.md, which changed in a later commit; `together` does not drift."""
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nlimit is 1000 per minute for everyone here.\n")
    write_raw(root, "b.md", "# b\n\nother source content that is long enough.\n")
    write_page(root, "together", "Committed with its raw. " * 4, sources=("raw/b.md",))
    write_page(root, "drifts", "Cites a. " * 8, sources=("raw/a.md",))
    commit(root, "ingest: initial")
    time.sleep(1.1)
    write_raw(root, "a.md", "# a\n\nlimit is 5000 per minute for everyone here.\n")
    commit(root, "ingest: a changed")
    return root


# ------------------------------- Tests --------------------------------------


def test_orphan_ignores_index_and_log(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nbody text long enough to not be a stub at all.\n")
    write_page(root, "lonely", "Nothing links here. " * 5)
    write_page(root, "hub", "See [[linked]] for details. " * 3)
    write_page(root, "linked", "Linked from hub. " * 5)
    (root / "wiki/index.md").write_text("# Index\n\n- [[lonely]]\n- [[hub]]\n- [[linked]]\n")
    (root / "wiki/log.md").write_text("# Log\n\n## 2026-01-01\n- [[lonely]]\n")
    r = run_lint(root)
    _assert(r["orphans"] == ["hub", "lonely"], f"orphans: {r['orphans']}")
    _assert(not any(v["page"] in ("index", "log") for v in r["format_violations"]), "index/log exempt from format")
    _assert(not any(v["page"] in ("index", "log") for v in r["unsourced_claims"]), "index/log exempt from unsourced")
    _assert("edges" not in r and "inbound_counts" not in r, "graph hidden by default")
    _assert("edges" in run_lint(root, "--graph"), "graph with --graph")
    _assert("warnings" in r, "warnings key present")


def test_bare_source_filename_resolves(tmp):
    root = make_wiki(tmp)
    write_raw(root, "api-guide.md", "# g\n\ncontent content content content content content\n")
    write_page(root, "p", "Body text. " * 6, sources=("api-guide.md",))
    r = run_lint(root)
    _assert(r["missing_sources"] == [], "bare filename is not a missing source")
    write_raw(root, "stub.md", "# stub\n")
    write_page(root, "q", "Body text. " * 6, sources=("stub.md",))
    r = run_lint(root)
    _assert(not any(e["slug"] == "stub" for e in r["empty_raw"]), f"bare-cited stub is cited: {r['empty_raw']}")


def test_bare_source_custom_raw_dir(tmp):
    root = make_wiki(tmp)
    (root / "sources").mkdir()
    (root / "sources/api-guide.md").write_text("# g\n")
    write_page(root, "p", "Body text. " * 6, sources=("api-guide.md",))
    r = run_lint(root, "--raw-dir", "sources")
    _assert(r["missing_sources"] == [], f"missing_sources: {r['missing_sources']}")
    _assert(r["empty_raw"] == [], f"bare-cited stub in custom raw dir is cited: {r['empty_raw']}")


def test_source_drift(tmp):
    root = drift_fixture(tmp)
    r = run_lint(root)
    _assert([d["page"] for d in r["source_drift"]] == ["drifts"], f"drift: {r['source_drift']}")
    _assert(r["source_drift"][0]["drifted"][0]["raw"] == "raw/a.md", "names the raw")
    _assert("stale_pages" not in r, "stale_pages removed")
    # a lint: commit touching the page must not clear drift
    p = root / "wiki/drifts.md"; p.write_text(p.read_text() + "\nlink fix\n")
    time.sleep(1.1); commit(root, "lint: unlink")
    _assert([d["page"] for d in run_lint(root)["source_drift"]] == ["drifts"], "lint: commit does not clear drift")
    # a non-lint commit touching the page clears it
    p.write_text(p.read_text() + "\nrewritten\n"); time.sleep(1.1); commit(root, "ingest: rewrite")
    _assert(run_lint(root)["source_drift"] == [], "page commit clears drift")


def test_drift_uncommitted_and_no_git(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\noriginal content long enough to count here.\n")
    write_page(root, "p", "Cites a. " * 8)
    commit(root, "ingest: initial")
    write_raw(root, "a.md", "# a\n\nmodified but not committed content here.\n")
    d = run_lint(root)["source_drift"]
    _assert(d and d[0]["drifted"][0]["uncommitted"] is True, "modified cited raw is drift")
    shutil.rmtree(root / ".git")
    r = run_lint(root)
    _assert(r["source_drift"] == [] and any("git" in w for w in r["warnings"]), "no git → empty + warning")


def test_drift_untracked_and_retired(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\noriginal content long enough to count here.\n")
    p = write_page(root, "old", "Cites a. " * 8)
    p.write_text(p.read_text().replace("**Last updated**", "**Status**: Archived\n\n**Last updated**"))
    write_page(root, "p", "Cites new. " * 8, sources=("raw/new.md",))
    commit(root, "ingest: initial")
    time.sleep(1.1)
    write_raw(root, "a.md", "# a\n\nchanged content long enough to count here.\n")
    commit(root, "ingest: a changed")
    write_raw(root, "new.md", "# new\n\nuntracked raw content long enough here.\n")
    # an untracked page whose cited raw is dirty is never drifted
    write_page(root, "fresh", "Cites a too. " * 8)
    write_raw(root, "a.md", "# a\n\ndirty again content long enough here.\n")
    d = {e["page"]: e for e in run_lint(root)["source_drift"]}
    _assert("old" not in d, f"archived page is skipped: {d}")
    _assert("fresh" not in d, "untracked page never drifted")
    _assert("p" in d and d["p"]["drifted"][0]["uncommitted"] is True, f"untracked cited raw is drift: {d}")


def test_fact_collision_and_overlap(tmp):
    root = collision_fixture(tmp)
    r = run_lint(root)
    fc = r["fact_collisions"]
    _assert(len(fc) == 1 and fc[0]["pages"] == ["api-rate-limit", "rate-limits"], f"collision: {fc}")
    _assert(fc[0]["differing"] == [["5000"], ["1000"]], f"differing: {fc[0]['differing']}")
    top = r["overlap_candidates"][0]
    _assert(top["pages"] == ["api-rate-limit", "rate-limits"] and top["shared_sources"] == 1, f"overlap: {top}")
    _assert(all("unrelated" not in c["pages"] for c in r["overlap_candidates"]), "unrelated page not a candidate")


def test_collision_cjk(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "seigen-a", "APIのレート制限は毎分1000リクエストです。全テナントに適用されます。")
    write_page(root, "seigen-b", "APIのレート制限は毎分5000リクエストです。全テナントに適用されます。")
    _assert(len(run_lint(root)["fact_collisions"]) == 1, "CJK collision found")


def test_collision_ignores_code_tables_versions(tmp):   # Review Focus 2
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "rel-a", "Release notes for the service.\n\n```\nretries = 3\ntimeout = 30\n```\n\n| limit | 10 |\n")
    write_page(root, "rel-b", "Release notes for the service.\n\n```\nretries = 5\ntimeout = 60\n```\n\n| limit | 20 |\n")
    _assert(run_lint(root)["fact_collisions"] == [], "code blocks and tables are not compared")


def test_pair_generation_scales(tmp):                    # Review Focus 3
    import random
    root = make_wiki(tmp)
    words = ("cache queue worker billing search gateway ledger auth export import index shard "
             "replica tenant invoice report metric alert backup token").split()
    for i in range(300):
        rnd = random.Random(i)
        body = " ".join(
            f"The {rnd.choice(words)} {rnd.choice(words)} handles {rnd.randint(1, 9999)} "
            f"{rnd.choice(words)} jobs per {rnd.choice(('second', 'minute', 'hour'))} in region {rnd.randint(1, 20)}."
            for _ in range(22))
        write_page(root, f"topic{i:03d}x", body, sources=(f"raw/g{i // 30}.md",))  # groups of 30 share a raw
    t = time.time(); r = run_lint(root); elapsed = time.time() - t
    print(f"     300 pages / 10 groups of 30: {elapsed:.1f}s")
    _assert(elapsed < 30, f"300 pages under 30s: {elapsed:.1f}s")
    _assert(len(r["overlap_candidates"]) <= 20, "candidate cap respected")
    _assert(len(r["fact_collisions"]) <= 50, "collision cap respected")
    _assert(r["overlap_candidates"] and r["fact_collisions"], "pair scoring actually ran")


NEAR_DUP_A = (
    "The ingestion service reads every source from the raw folder and writes one wiki page per topic. "
    "Each page carries a summary, a list of sources and a last updated date at the top. "
    "When a source changes, the page that cites it is flagged for review by the linter. "
    "Links between pages use double brackets and must point at an existing page or the archive. "
    "Archived pages keep their slug so that older references continue to resolve correctly. "
    "Conflicting statements across pages are reported to the user before anything is rewritten. "
    "The index page lists every active page with a one line description of what it covers. "
    "Large spaces are ingested in batches with a resumable job queue and a circuit breaker. "
    "Images found in a source are described once and reused until the image itself changes. "
    "Every run ends with a log entry and a single commit so the history stays easy to follow."
)
NEAR_DUP_B = (
    "The ingestion service reads each source from the raw folder and writes one wiki page per topic. "
    "Links between pages use double brackets and must point at an existing page or the archive. "
    "Each page carries a summary, a list of sources and a last-updated date at the top. "
    "When a source changes, the page citing it is flagged for review by the linter. "
    "Archived pages keep their slug so older references continue to resolve correctly. "
    "The index page lists every active page with a one line description of what it covers. "
    "Conflicting statements across pages are reported to the user before anything gets rewritten. "
    "Large spaces are ingested in batches using a resumable job queue and a circuit breaker. "
    "Images found in a source are described once and then reused until the image changes. "
    "Every run ends with a log entry and one commit so that the history stays easy to follow."
)


def test_near_duplicate_both_slug_orders(tmp):
    _assert(len(NEAR_DUP_A) > 800 and len(NEAR_DUP_B) > 800, "fixture pages are >800 chars")
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    for first, second in ((NEAR_DUP_A, NEAR_DUP_B), (NEAR_DUP_B, NEAR_DUP_A)):
        write_page(root, "ingest-pipeline", first)
        write_page(root, "wiki-workflow", second)
        cands = run_lint(root)["overlap_candidates"]
        _assert(any(c["pages"] == ["ingest-pipeline", "wiki-workflow"] for c in cands), f"near-dup found: {cands}")


def test_hub_key_exact_collision_and_warning(tmp):
    root = make_wiki(tmp)
    write_raw(root, "hub.md", "# hub\n\nsource body that is long enough to count.\n")
    for i in range(58):
        write_page(root, f"filler{i:03d}", "Filler page that mentions the [[glossary]] once in passing here.",
                   sources=("raw/hub.md",))
    write_page(root, "alpha", "The rate limit is 1000 requests per minute. See [[glossary]].", sources=("raw/hub.md",))
    write_page(root, "beta", "The rate limit is 5000 requests per minute. See [[glossary]].", sources=("raw/hub.md",))
    r = run_lint(root)
    fc = [c for c in r["fact_collisions"] if c["pages"] == ["alpha", "beta"]]
    _assert(len(fc) == 1 and fc[0]["differing"] == [["1000"], ["5000"]], f"hub collision: {fc}")
    _assert(any("raw/hub.md" in w and "60" in w for w in r["warnings"]), f"hub warning: {r['warnings']}")


def test_collision_short_key_value_sentences(tmp):
    for one, two in (("SLA is 99.9% uptime.", "SLA: 99.95% uptime."),
                     ("Timeout is 30 s.", "Timeout: 60 s.")):
        root = make_wiki(tmp / one[:3])
        write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
        write_page(root, "page-one", one)
        write_page(root, "page-two", two)
        fc = run_lint(root)["fact_collisions"]
        _assert(len(fc) == 1 and fc[0]["sentences"] == [one, two], f"{one!r} vs {two!r}: {fc}")


def test_collision_reordered_numbers_and_self_link(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "one", "Window opens at 9 and closes at 17 every day. See [[two]].", sources=("raw/x.md",))
    write_page(root, "two", "Window opens at 17 and closes at 9 every day.", sources=("raw/y.md",))
    fc = run_lint(root)["fact_collisions"]
    _assert(len(fc) == 1 and fc[0]["differing"] == [["9", "17"], ["17", "9"]], f"positional differing: {fc}")


def test_unbannered_archive_link_skips_index(tmp):
    root = make_wiki(tmp)
    write_page(root, "archive/old", "Old content without a banner.")
    write_page(root, "user", "See [[archive/old]].")
    (root / "wiki" / "index.md").write_text("# Index\n\n- [[archive/old]]\n- [[user]]\n", encoding="utf-8")
    links = run_lint(root)["archive_links_without_banner"]
    _assert([l["from"] for l in links] == ["user"], f"index must not be flagged: {links}")


def test_traversal_source_reported_missing(tmp):
    root = make_wiki(tmp)
    write_page(root, "p", "Body text.", sources=("raw/../../etc/passwd",))
    ms = run_lint(root)["missing_sources"]  # run_lint asserts exit 0
    _assert(ms == [{"page": "p", "missing": ["raw/../../etc/passwd"]}], f"missing_sources: {ms}")


def test_journal_pages_exempt(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    day = "## 09:00 — Decided limit\n\nThe API rate limit is 1000 requests per minute for all tenants.\n\n**Sources**: `raw/a.md`\n"
    write_page(root, "2026-01-02", day)
    write_page(root, "2026-01-03", day.replace("1000", "5000"))
    write_page(root, "2026-01-index", "- [[2026-01-02]]\n- [[2026-01-03]]\n")
    write_page(root, "only-from-diary", "Linked only from a day page. " * 4)
    p = root / "wiki/2026-01-02.md"; p.write_text(p.read_text() + "\nSee [[only-from-diary]].\n")
    r = run_lint(root)
    journal = {"2026-01-02", "2026-01-03", "2026-01-index"}
    _assert(not journal & set(r["orphans"]), f"journal not orphans: {r['orphans']}")
    _assert("only-from-diary" not in r["orphans"], "day-page link counts as inbound")
    _assert(r["fact_collisions"] == [] and r["overlap_candidates"] == [], "journal pages never compared")
    for key in ("empty_pages", "format_violations", "unsourced_claims"):
        _assert(not any(e["page"] in journal for e in r[key]), f"journal exempt from {key}")
    _assert(not any(d["page"] in journal for d in r["source_drift"]), "journal exempt from drift")


def test_link_resolution(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "target", "Has a section. " * 5)
    write_page(root, "linker", "See [[target#Section]], [[other-wiki/some-page]], [[ghost#part]] and [[#local]]. " * 2)
    write_page(root, "2026-02-01", "## 10:00 — Note\n\nSee [[ghost2]] and [[label/slug]].\n")
    r = run_lint(root)
    broken = {(b["from"], b["to"]) for b in r["broken_links"]}
    _assert("target" not in r["orphans"], "anchor link counts as inbound to target")
    _assert(all(b["from"] != "linker" or "ghost" in b["to"] for b in r["broken_links"]), f"only ghost is broken from linker: {broken}")
    _assert(any(b["from"] == "linker" and b["to"].startswith("ghost") for b in r["broken_links"]), "missing page with anchor is broken")
    _assert(any(b["from"] == "2026-02-01" and b["to"] == "ghost2" for b in r["broken_links"]), "journal links still checked")
    _assert(not any("/" in b["to"] and not b["to"].startswith("archive/") for b in r["broken_links"]), "cross-wiki never broken")


def test_review_memory(tmp):
    root = collision_fixture(tmp)
    pair = ["api-rate-limit", "rate-limits"]
    sents = run_lint(root)["fact_collisions"][0]["sentences"]
    out = run_lint(root, "--mark-reviewed", stdin=json.dumps({
        "pairs": [{"pages": pair, "verdict": "fine"}],
        "collisions": [{"pages": pair, "sentences": sents, "verdict": "rejected"}]}))
    _assert(out == {"recorded": 2, "pruned": 0}, f"mark: {out}")
    r = run_lint(root)
    _assert(r["fact_collisions"] == [] and r["overlap_candidates"] == [], "reviewed items suppressed")
    _assert(r["suppressed"] == {"pairs": 1, "collisions": 1, "drift": 0}, f"suppressed: {r['suppressed']}")
    p = root / "wiki/rate-limits.md"; p.write_text(p.read_text().replace("1000", "1200"))
    r = run_lint(root)
    _assert(len(r["fact_collisions"]) == 1 and len(r["overlap_candidates"]) == 1, "page change invalidates entries")
    (root / "wiki/api-rate-limit.md").unlink()
    out = run_lint(root, "--mark-reviewed", stdin="{}")
    _assert(out["pruned"] == 2, "entries for deleted pages pruned")


def test_drift_ack(tmp):
    root = drift_fixture(tmp)
    run_lint(root, "--mark-reviewed", stdin=json.dumps({"drift": [{"page": "drifts", "raw": "raw/a.md"}]}))
    _assert(run_lint(root)["source_drift"] == [], "acknowledged drift suppressed")
    write_raw(root, "a.md", "# a\n\nlimit is 9000 per minute for everyone here.\n"); time.sleep(1.1); commit(root, "ingest: again")
    _assert(len(run_lint(root)["source_drift"]) == 1, "new raw content re-opens drift")


def test_since_last_lint(tmp):
    root = collision_fixture(tmp)
    _assert(len(run_lint(root, "--since-last-lint")["fact_collisions"]) == 1, "no lint commit → full")
    (root / "wiki/log.md").write_text("# Log\n"); time.sleep(1.1); commit(root, "lint: baseline")
    _assert(run_lint(root, "--since-last-lint")["fact_collisions"] == [], "nothing changed since lint")
    _assert(len(run_lint(root)["fact_collisions"]) == 1, "--full default still reports")
    p = root / "wiki/rate-limits.md"; p.write_text(p.read_text() + "\nedit\n")
    _assert(len(run_lint(root, "--since-last-lint")["fact_collisions"]) == 1, "changed page re-enters scope")


def test_collision_template_does_not_crowd_out(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    for i in range(12):
        write_page(root, f"build{i:02d}", f"Build number is {i + 1}.")
    write_page(root, "sla-one", "SLA is 99.9% uptime.")
    write_page(root, "sla-two", "SLA: 99.95% uptime.")
    r = run_lint(root)
    fc = r["fact_collisions"]
    _assert(any(c["pages"] == ["sla-one", "sla-two"] for c in fc), f"SLA collision reported: {len(fc)} entries")
    _assert(len(fc) == 50 and all("kind" not in c for c in fc), f"cap filled, no internal keys: {len(fc)}")
    _assert("fact_collisions: showing 50 of 67 found (exact: 66, fuzzy: 1)" in r["warnings"], f"warning: {r['warnings']}")


def main() -> int:
    tests =[v for k, v in globals().items() if k.startswith("test_") and callable(v)]
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

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


def test_paraphrased_overlap_shared_source(tmp):
    # Same topic, one shared raw, different wording: scores ~0.40 (below the old 0.5 threshold).
    root = make_wiki(tmp)
    write_raw(root, "auth.md", "# auth\n\nSessions expire after 30 minutes. Login uses SSO with a one-time code.\n")
    write_page(root, "auth", "Users sign in through the company SSO portal and then confirm with a one-time code. "
               "A session expires after 30 minutes of inactivity and the user must sign in again "
               "(source: raw/auth.md).", sources=("raw/auth.md",))
    write_page(root, "authentication-flow", "Login goes through single sign-on, then the user enters a one-time "
               "code. Sessions time out after 10 minutes of inactivity, after which a fresh login is required "
               "(source: raw/auth.md).", sources=("raw/auth.md",))
    commit(root, "ingest: auth")
    cands = run_lint(root)["overlap_candidates"]
    _assert(any(c["pages"] == ["auth", "authentication-flow"] for c in cands), f"paraphrase found: {cands}")


BILLING_SIBLINGS = {
    "billing-invoices": "Invoices are generated on the first business day of each month for every active "
        "subscription. Each invoice lists the plan, the seat count and any usage overage, and it is emailed "
        "to the account owner as a PDF. Unpaid invoices are retried by the card processor three times before "
        "the account is flagged for the collections team.",
    "billing-refunds": "Refunds are issued only by the support lead and go back to the original payment "
        "method. A customer who cancels within fourteen days of a renewal gets the full amount back; after "
        "that, only unused seats are credited. Partial refunds appear as a negative line on the next "
        "statement rather than as a separate transfer.",
    "billing-taxes": "Sales tax is calculated from the customer's billing address, not from where their "
        "users sit. European business customers who supply a valid VAT number are reverse-charged, and "
        "the number is checked against the VIES service at checkout. Tax exemption certificates must be "
        "uploaded by finance before the exemption takes effect.",
    "billing-currencies": "Prices are set in US dollars, euros and pounds; every other country is billed "
        "in dollars. The currency is fixed when the first subscription is created and cannot be switched "
        "later without opening a new account. Exchange rates are never applied to existing contracts.",
    "payments": "Card payments run through the external processor, while enterprise customers may pay by "
        "bank wire against a purchase order. Wire payments are matched to accounts by the reference code "
        "printed on the statement, and unmatched wires are held in a suspense ledger for manual review.",
}


def test_billing_siblings_not_overlap(tmp):
    # Distinct pages on one handbook, sharing its source and linking to each other: not duplicates.
    root = make_wiki(tmp)
    write_raw(root, "billing-handbook.md", "# Billing handbook\n\nInvoices, refunds, taxes, currencies, payments.\n")
    for slug, prose in BILLING_SIBLINGS.items():
        links = " ".join(f"[[{o}]]" for o in BILLING_SIBLINGS if o != slug)
        write_page(root, slug, f"{prose} (source: raw/billing-handbook.md)\n\nSee also {links}.",
                   sources=("raw/billing-handbook.md",))
    commit(root, "ingest: billing")
    cands = run_lint(root)["overlap_candidates"]
    _assert(not any(set(c["pages"]) <= set(BILLING_SIBLINGS) for c in cands), f"siblings reported: {cands}")


def test_no_prose_pages_text_sim_zero(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "rates-table", "| plan | rate |\n|---|---|\n| a | 1 |\n")
    write_page(root, "rates-tables", "```\nrate = 2\n```\n")
    cands = run_lint(root)["overlap_candidates"]
    _assert(cands == [], f"no prose -> text_sim 0, so slug + source alone do not report: {cands}")


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
    # A day-page and a control page with the same defects: no Summary/Last
    # updated, a needs-verification marker, and a cited raw changed later.
    bare = ("# {}\n\n**Sources**: `raw/b.md`\n\n## 10:00 — Open question\n\n"
            "Retries may be capped at three (source: needs verification).\n")
    write_raw(root, "b.md", "# b\n\noriginal source body that is long enough.\n")
    (root / "wiki/2026-01-04.md").write_text(bare.format("2026-01-04"))
    (root / "wiki/control.md").write_text(bare.format("control"))
    commit(root, "ingest: initial")
    time.sleep(1.1)
    write_raw(root, "b.md", "# b\n\nchanged source body that is long enough.\n")
    commit(root, "ingest: b changed")
    r = run_lint(root)
    journal = {"2026-01-02", "2026-01-03", "2026-01-04", "2026-01-index"}
    _assert(not journal & set(r["orphans"]), f"journal not orphans: {r['orphans']}")
    _assert("only-from-diary" not in r["orphans"], "day-page link counts as inbound")
    _assert(not any(journal & set(c["pages"]) for c in r["fact_collisions"] + r["overlap_candidates"]),
            "journal pages never compared")
    for key in ("empty_pages", "format_violations", "unsourced_claims", "source_drift"):
        _assert(not any(e["page"] in journal for e in r[key]), f"journal exempt from {key}")
    for key in ("format_violations", "unsourced_claims", "source_drift"):
        _assert(any(e["page"] == "control" for e in r[key]), f"control reported in {key}: {r[key]}")


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


def test_archive_link_case_insensitive(tmp):
    root = make_wiki(tmp)
    write_page(root, "archive/old", "Old content kept for history.")
    write_page(root, "user", "See [[Archive/old]] and [[ARCHIVE/old#Part]]. " * 2)
    commit(root, "ingest: initial")
    r = run_lint(root, "--fix")
    _assert(not any(b["from"] == "user" for b in r["broken_links"]), f"Archive/ resolves: {r['broken_links']}")
    _assert("[[Archive/old]]" in (root / "wiki/user.md").read_text(), "--fix keeps Archive/ link")
    _assert([l["to"] for l in r["archive_links_without_banner"]] == ["archive/old"], f"{r['archive_links_without_banner']}")


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


STUB = "Placeholder — add content when a source covers this concept."
NO_FIXES = {"deleted_pages": [], "unlinked": [], "repaired_sources": [], "removed_index_lines": []}


def lint_proc(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LINT), "--wiki-root", str(root), *args],
                          capture_output=True, text=True)


def wiki_bytes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted((root / "wiki").rglob("*")) if p.is_file()}


def test_fix(tmp):
    root = make_wiki(tmp)
    write_raw(root, "api-guide.md", "# g\n\nreal content that is long enough to count.\n")
    write_page(root, "stub", STUB)
    write_page(root, "keeper", "See [[stub]] and [[ghost|the ghost page]] and [[real]]. " * 2, sources=("raw/api-guide.pdf",))
    write_page(root, "real", "Links back to [[keeper]]. " * 4, sources=("raw/api-guide.md",))
    (root / "wiki/index.md").write_text("# Index\n\n- [[stub]] — placeholder\n- [[keeper]] — keeper\n- [[real]] — real\n")
    commit(root, "ingest: initial")
    before = wiki_bytes(root)
    _assert(run_lint(root)["empty_pages"] and wiki_bytes(root) == before, "no --fix → no writes")
    r = run_lint(root, "--fix")
    _assert(r["fixed"] == {
        "deleted_pages": ["stub"],
        "unlinked": [{"page": "keeper", "target": "stub"}],
        "repaired_sources": [{"page": "keeper", "from": "raw/api-guide.pdf", "to": "raw/api-guide.md"}],
        "removed_index_lines": ["stub"]}, f"fixed: {r['fixed']}")
    _assert(not (root / "wiki/stub.md").exists(), "stub deleted")
    text = (root / "wiki/keeper.md").read_text()
    _assert("[[stub]]" not in text and "See stub and" in text, "link to deleted stub unlinked")
    _assert("[[ghost|the ghost page]]" in text and "[[real]]" in text, "other links kept")  # Review Focus 4
    _assert("raw/api-guide.md" in text and "api-guide.pdf" not in text, "exact-stem source repaired")
    _assert((root / "wiki/index.md").read_text() == "# Index\n\n- [[keeper]] — keeper\n- [[real]] — real\n",
            "index line removed, not turned into text")
    _assert(r["broken_links"] == [{"from": "keeper", "to": "ghost"}], f"other broken links report-only: {r['broken_links']}")
    _assert(r["empty_pages"] == [] and r["missing_sources"] == [], "report is post-fix")
    after = wiki_bytes(root)
    r2 = run_lint(root, "--fix", "--allow-dirty")
    _assert(wiki_bytes(root) == after and r2["fixed"] == NO_FIXES, f"second --fix changes nothing: {r2['fixed']}")
    commit(root, "lint: fix")
    out = lint_proc(root, "--fix")
    _assert(out.returncode == 0 and json.loads(out.stdout)["fixed"] == NO_FIXES and wiki_bytes(root) == after,
            f"--fix after committing the fix changes nothing: {out.stderr}")


def test_fix_stub_needs_short_body(tmp):
    root = make_wiki(tmp)
    long_pages = {
        "style-guide": "\n\n> Placeholder — add content when a source covers this concept.\n",
        "code-tbd": "\n\nWrite `TBD — mentioned in [[x]]` as the stub summary.\n",
        "code-ph": "\n\nUse ``placeholder - add content`` in new stubs.\n",
        "owners": "\n\nOwner: TBD - mentioned in standup.\n",
    }
    for slug, extra in long_pages.items():
        write_page(root, slug, NEAR_DUP_A + extra)
    (root / "wiki/foo.md").write_text(   # the stub template from ingest/references/page-format.md
        "# Foo\n\n**Summary**: TBD — mentioned in [[bar]] but not yet expanded.\n\n**Sources**: (none yet)\n\n"
        "**Last updated**: 2026-01-01\n\n---\n\nPlaceholder — add content when a source covers this concept.\n")
    # `#1 ...` is prose, not a heading: its text counts as content.
    write_page(root, "priorities", "Placeholder — add content.\n"
               "#1 priority: the payments refactor ships before the audit deadline.\n")
    (root / "wiki/index.md").write_text("# Index\n\n- [[priorities]] — what ships first\n")
    commit(root, "ingest: initial")
    before = wiki_bytes(root)
    r = run_lint(root, "--fix")
    _assert(r["fixed"]["deleted_pages"] == ["foo"], f"only the real stub deleted: {r['fixed']}")
    for slug in [*long_pages, "priorities", "index"]:
        key = f"wiki/{slug}.md"
        _assert((root / key).read_bytes() == before[key], f"{slug} byte-identical")
    _assert(not any(e["page"] in [*long_pages, "priorities"] for e in r["empty_pages"]), f"not stubs: {r['empty_pages']}")


def test_page_body_header_detection(tmp):
    sys.path.insert(0, str(LINT.parent))
    import lint
    wrapped = ("# T\n\n**Summary**: a long summary that is\nhard-wrapped onto a second line\n\n"
               "**Sources**:\n- `raw/a.md`\n\n**Last updated**: 2026-01-01\n\n---\n\nShort body.\n")
    _assert(lint.page_body(wrapped).strip() == "Short body.", "wrapped field is header material")
    plain_list = "# T\n\n- a\n- b\n\n---\n\nrest\n"
    _assert(lint.page_body(plain_list).strip().startswith("- a"), "a list after the H1 is body, not header")


def test_fix_keeps_short_and_retired_pages(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    (root / "wiki/hr.md").write_text("# Release process\n\nReleases are cut every second Tuesday by on-call.\n\n---\n\nSee also.\n")
    write_page(root, "def", "TTL: time to live, in seconds.")
    write_page(root, "redirect", "See [[target-page]].")
    write_page(root, "table", "| a | b |\n|---|---|\n| 1 | 2 |")
    p = write_page(root, "old-stub", STUB)
    p.write_text(p.read_text().replace("**Last updated**", "**Status**: Superseded by [[target-page]]\n\n**Last updated**"))
    write_page(root, "target-page", "See [[def]], [[redirect]], [[table]], [[old-stub]] and [[hr]]. " * 2)
    (root / "wiki/index.md").write_text("# Index\n\n- [[def]] — TTL definition\n- [[old-stub]] — old\n")
    commit(root, "ingest: initial")
    before = wiki_bytes(root)
    r = run_lint(root, "--fix")
    _assert(r["fixed"] == NO_FIXES and wiki_bytes(root) == before, f"nothing deleted or rewritten: {r['fixed']}")
    _assert(not any(e["page"] == "hr" for e in r["empty_pages"]), "a --- rule in prose is not the header separator")


def test_fix_never_touches_code_or_embeds(tmp):
    root = make_wiki(tmp)
    write_page(root, "stub", STUB)
    code = ("Shell check:\n\n```sh\nif [[ -f \"$cfg\" ]]; then\n  echo [[stub]]\nfi\n```\n\n"
            "~~~~\n[[stub]]\n```\nstill fenced [[stub]]\n~~~~\n\n"
            "Inline `[[stub]]` and ``a `[[stub]]` b`` and an embed ![[stub]] and ![[diagram.png]] "
            "and `[[ghost-inline]]` and x=[[template]].\n")
    write_page(root, "code", code)
    write_page(root, "prose", "Mentions [[stub]] in plain prose. " * 2)
    commit(root, "ingest: initial")
    before = (root / "wiki/code.md").read_bytes()
    r = run_lint(root, "--fix")
    _assert(r["fixed"]["deleted_pages"] == ["stub"], f"stub deleted: {r['fixed']}")
    _assert((root / "wiki/code.md").read_bytes() == before, "code, inline code and embeds byte-identical")
    _assert(r["fixed"]["unlinked"] == [{"page": "prose", "target": "stub"}], f"unlinked: {r['fixed']['unlinked']}")


def test_fix_keeps_case_and_nested_archive_links(tmp):
    root = make_wiki(tmp)
    write_page(root, "real-page", "Real content that is long enough here. " * 2)
    write_page(root, "archive/2025/old", "Old content kept for history.")
    write_page(root, "stub", STUB)
    write_page(root, "linker", "See [[Real-Page]] and [[archive/2025/old]]. " * 2)
    commit(root, "ingest: initial")
    before = (root / "wiki/linker.md").read_bytes()
    r = run_lint(root, "--fix")
    _assert(r["fixed"]["deleted_pages"] == ["stub"] and r["fixed"]["unlinked"] == [], f"fixed: {r['fixed']}")
    _assert((root / "wiki/linker.md").read_bytes() == before, "existing-page links byte-identical")


def test_fix_index_nested_line_kept(tmp):
    root = make_wiki(tmp)
    write_page(root, "stub", STUB)
    write_page(root, "stub2", STUB)
    write_page(root, "keeper", "Keeper content that is long enough here. " * 2)
    (root / "wiki/index.md").write_text(
        "# Index\n\n- [[stub]] — gone\n- [[stub2]] — has children\n  - [[keeper]] — nested\n"
        "- [[keeper]] and [[stub]] — two links\n- `[[stub]]` is the syntax\n")
    commit(root, "ingest: initial")
    out = lint_proc(root, "--fix")
    _assert(out.returncode == 0, out.stderr)
    r = json.loads(out.stdout)
    _assert((root / "wiki/index.md").read_text() ==
            "# Index\n\n- [[stub2]] — has children\n  - [[keeper]] — nested\n- [[keeper]] and stub — two links\n"
            "- `[[stub]]` is the syntax\n",
            f"index: {(root / 'wiki/index.md').read_text()!r}")
    _assert("stub2" in out.stderr and "nested" in out.stderr, f"stderr note: {out.stderr}")
    _assert(r["fixed"]["removed_index_lines"] == ["stub"], f"fixed: {r['fixed']}")
    _assert(r["fixed"]["unlinked"] == [{"page": "index", "target": "stub"}], f"fixed: {r['fixed']}")


def test_fix_sources_exact_stem_only(tmp):
    root = make_wiki(tmp)
    for name in ("old-api-guide-v1.md", "security-guide.md", "manual.md"):
        write_raw(root, name, "# r\n\nraw content that is long enough to count here.\n")
    write_page(root, "loose", "Cites near-miss paths only. " * 3, sources=("raw/api-guide.md", "raw/guide.md"))
    write_page(root, "exact", "Cites two other formats. " * 3, sources=("raw/manual.pdf", "raw/manual.txt"))
    commit(root, "ingest: initial")
    before = (root / "wiki/loose.md").read_bytes()
    r = run_lint(root, "--fix")
    _assert((root / "wiki/loose.md").read_bytes() == before, "no segment-run repair")
    _assert(r["fixed"]["repaired_sources"] == [{"page": "exact", "from": "raw/manual.pdf", "to": "raw/manual.md"}],
            f"one entry per repaired file: {r['fixed']['repaired_sources']}")
    _assert("manual.pdf" not in (root / "wiki/exact.md").read_text(), "both paths repointed")


def test_fix_preserves_bytes(tmp):
    root = make_wiki(tmp)
    write_page(root, "stub", STUB)
    crlf = (b"# crlf\r\n\r\n**Summary**: s\r\n\r\n**Sources**:\r\n- `raw/a.md`\r\n\r\n**Last updated**: 2026-01-01\r\n"
            b"\r\n---\r\n\r\nSee [[stub]] for the plan and more words here.\r\n")
    latin = b"# latin\n\n---\n\nCaf\xe9 notes link [[stub]] and more words to be long enough.\n"
    (root / "wiki/crlf.md").write_bytes(crlf)
    (root / "wiki/latin.md").write_bytes(latin)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    commit(root, "ingest: initial")
    r = run_lint(root, "--fix")
    _assert((root / "wiki/crlf.md").read_bytes() == crlf.replace(b"[[stub]]", b"stub"), "CRLF kept")
    _assert((root / "wiki/latin.md").read_bytes() == latin, "non-UTF-8 page never written")
    _assert(any("latin.md" in w for w in r["warnings"]), f"warning names it: {r['warnings']}")


def test_fix_refuses_non_git_and_dirty(tmp):
    root = make_wiki(tmp)
    write_page(root, "stub", STUB)
    p = write_page(root, "p", "See [[stub]] for the plan. " * 2)
    commit(root, "ingest: initial")
    p.write_text(p.read_text() + "\nlocal edit\n")
    snap = wiki_bytes(root)
    out = lint_proc(root, "--fix")
    _assert(out.returncode == 2 and "p.md" in out.stderr and "--allow-dirty" in out.stderr, f"dirty tracked: {out.stderr}")
    _assert(wiki_bytes(root) == snap, "nothing written on refusal")
    commit(root, "ingest: edit")
    write_page(root, "new", "An untracked page with enough words. " * 2)
    snap = wiki_bytes(root)
    out = lint_proc(root, "--fix")
    _assert(out.returncode == 2 and "new.md" in out.stderr and wiki_bytes(root) == snap, f"untracked .md: {out.stderr}")
    r = run_lint(root, "--fix", "--allow-dirty")
    _assert(r["fixed"]["deleted_pages"] == ["stub"], "--allow-dirty proceeds")
    shutil.rmtree(root / ".git")
    write_page(root, "stub", STUB)
    snap = wiki_bytes(root)
    out = lint_proc(root, "--fix", "--allow-dirty")
    _assert(out.returncode == 2 and "git" in out.stderr and wiki_bytes(root) == snap, f"non-git: {out.stderr}")


def test_fix_journal_crosswiki_log_traversal(tmp):
    root = make_wiki(tmp)
    write_raw(root, "api-guide.md", "# g\n\nreal content that is long enough to count.\n")
    write_page(root, "stub", STUB)
    day = root / "wiki/2026-01-02.md"
    day.write_text("# 2026-01-02\n\n## 09:00 — Note\n\nSee [[other/slug]], [[2026-01-02#Heading]], [[stub]] and [[ghost3]].\n")
    write_page(root, "normal", "See [[other/slug]], [[stub#part]] and [[ghost#part]] here. " * 2,
               sources=("raw/../api-guide.md",))
    (root / "wiki/log.md").write_text("# Log\n\n## 2026-01-01\n- created [[stub]]\n")
    (root / "wiki/.lint-reviewed.json").write_text('{"pairs": []}\n')
    commit(root, "ingest: initial")
    snap = {p: p.read_bytes() for p in (day, root / "wiki/log.md", root / "wiki/.lint-reviewed.json")}
    r = run_lint(root, "--fix")
    for p, b in snap.items():
        _assert(p.read_bytes() == b, f"{p.name} byte-identical after --fix")
    text = (root / "wiki/normal.md").read_text()
    _assert("[[other/slug]]" in text and "[[ghost#part]]" in text, "cross-wiki and never-existing links kept")
    _assert("[[stub" not in text and "stub#part and" in text, f"anchored link to deleted page unlinked: {text}")
    _assert("raw/../api-guide.md" in text and r["fixed"]["repaired_sources"] == [], "traversal never repaired")
    _assert(r["fixed"]["unlinked"] == [{"page": "normal", "target": "stub"}], f"unlinked: {r['fixed']['unlinked']}")
    out = lint_proc(root, "--fix", "--mark-reviewed")
    _assert(out.returncode == 2 and "not allowed" in out.stderr, f"--fix with --mark-reviewed: {out.stderr}")


def test_claude_md_warning(tmp):
    root = make_wiki(tmp); (root / "CLAUDE.md").write_text("# W\n\n## Rules\n- old\n")
    _assert(any("--upgrade" in w for w in run_lint(root)["warnings"]), "warns without managed block")
    (root / "CLAUDE.md").write_text("# W\n<!-- llm-wiki:begin -->\nx\n<!-- llm-wiki:end -->\n")
    _assert(not any("--upgrade" in w for w in run_lint(root)["warnings"]), "no warning with block")


def test_malformed_review_memory(tmp):
    root = collision_fixture(tmp)
    bad = {"pairs": [{"pages": ["a", "b"]}, "x"],
           "collisions": [{"pages": [["a"], "b"], "hash": "h"}],
           "drift": [{"page": "x"}]}
    (root / "wiki/.lint-reviewed.json").write_text(json.dumps(bad))
    r = run_lint(root)  # asserts exit 0
    _assert(len(r["fact_collisions"]) == 1 and r["suppressed"] == {"pairs": 0, "collisions": 0, "drift": 0},
            f"bad entries suppress nothing: {r['suppressed']}")
    _assert(sum("ignored 4 malformed" in w for w in r["warnings"]) == 1, f"one warning: {r['warnings']}")
    out = run_lint(root, "--mark-reviewed", stdin=json.dumps({"pairs": [{"pages": ["api-rate-limit", "rate-limits"], "verdict": "fine"}]}))
    _assert(out == {"recorded": 1, "pruned": 0}, f"mark over bad file: {out}")
    kept = json.loads((root / "wiki/.lint-reviewed.json").read_text())
    _assert(kept["collisions"] == [] and kept["drift"] == [] and len(kept["pairs"]) == 1,
            f"bad entries dropped on rewrite: {kept}")


def test_mark_reviewed_rejects_noop_entries(tmp):
    root = collision_fixture(tmp)
    sents = run_lint(root)["fact_collisions"][0]["sentences"]
    for payload in ({"pairs": [{"pages": ["rate-limits", "rate-limits"], "verdict": "fine"}]},
                    {"pairs": [{"pages": "rate-limits", "verdict": "fine"}]},
                    {"pairs": [{"pages": ["rate-limits", 3], "verdict": "fine"}]},
                    {"collisions": [{"pages": ["api-rate-limit", "unrelated"], "sentences": sents, "verdict": "fine"}]},
                    {"collisions": [{"pages": ["api-rate-limit", "rate-limits"], "sentences": [sents[0], "made up 7."],
                                     "verdict": "fine"}]}):
        out = subprocess.run([sys.executable, str(LINT), "--wiki-root", str(root), "--mark-reviewed"],
                             input=json.dumps(payload), capture_output=True, text=True)
        _assert(out.returncode == 1 and "ERROR" in out.stderr, f"rejected {payload}: {out.returncode} {out.stderr}")
        _assert(not (root / "wiki/.lint-reviewed.json").exists(), f"nothing written for {payload}")


def test_since_last_lint_non_ascii(tmp):
    root = make_wiki(tmp)
    write_page(root, "料金", "The API rate limit is 1000 requests per minute.", sources=("raw/x.md",))
    write_page(root, "zz", "The API rate limit is 5000 requests per minute.", sources=("raw/y.md",))
    commit(root, "ingest: initial")
    (root / "wiki/log.md").write_text("# Log\n"); time.sleep(1.1); commit(root, "lint: baseline")
    _assert(run_lint(root, "--since-last-lint")["fact_collisions"] == [], "nothing changed")
    p = root / "wiki/料金.md"; p.write_text(p.read_text() + "\nedit\n")
    fc = run_lint(root, "--since-last-lint")["fact_collisions"]
    _assert(len(fc) == 1 and "料金" in fc[0]["pages"], f"non-ASCII changed page in scope: {fc}")


def test_drift_non_ascii_raw(tmp):
    root = make_wiki(tmp)
    write_raw(root, "料金.md", "# a\n\noriginal content long enough to count here.\n")
    write_page(root, "modified", "Cites it. " * 8, sources=("raw/料金.md",))
    write_page(root, "untracked", "Cites new. " * 8, sources=("raw/新規.md",))
    commit(root, "ingest: initial")
    write_raw(root, "料金.md", "# a\n\nchanged content long enough to count here.\n")
    write_raw(root, "新規.md", "# n\n\nuntracked content long enough to count here.\n")
    d = {e["page"]: e["drifted"][0] for e in run_lint(root)["source_drift"]}
    _assert(set(d) == {"modified", "untracked"} and all(x["uncommitted"] for x in d.values()),
            f"non-ASCII dirty raws are drift: {d}")


def test_since_last_lint_scan_budget(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    for i in range(46):  # C(46, 2) = 1035 exact hits > the 1000-hit scan budget
        write_page(root, f"build{i:02d}", f"Build number is {i + 1}.")
    commit(root, "ingest: initial")
    (root / "wiki/log.md").write_text("# Log\n"); time.sleep(1.1); commit(root, "lint: baseline")
    p = root / "wiki/build45.md"; p.write_text(p.read_text() + "\nedit\n")
    full = run_lint(root)
    _assert(any("at least" in w and "truncated" in w for w in full["warnings"]), f"full warns: {full['warnings']}")
    r = run_lint(root, "--since-last-lint")
    fc = r["fact_collisions"]
    _assert(len(fc) == 45 and all("build45" in c["pages"] for c in fc), f"all 45 in-scope found: {len(fc)} {r['warnings']}")


def test_collision_exact_cap_binds(tmp):
    root = make_wiki(tmp)
    for i in range(12):
        write_page(root, f"build{i:02d}", f"Build number is {i + 1}.")
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
             "kilo lima mike november oscar papa quebec romeo sierra tango").split()
    for i, w in enumerate(words):  # 20 fuzzy hits, one per pair sharing its own raw
        write_page(root, f"q{i:02d}a", f"The {w} queue limit is 10 jobs.", sources=(f"raw/q{i}.md",))
        write_page(root, f"q{i:02d}b", f"The {w} queue limit: 20 jobs.", sources=(f"raw/q{i}.md",))
    r = run_lint(root)
    fc = r["fact_collisions"]
    n_exact = sum(c["pages"][0].startswith("build") for c in fc)
    _assert(len(fc) == 50 and n_exact == 35, f"35 exact slots: {n_exact} of {len(fc)}; {r['warnings']}")
    _assert(any("exact: 66" in w for w in r["warnings"]), f"warning: {r['warnings']}")


def test_collision_exact_fewest_pages_first(tmp):
    root = make_wiki(tmp)
    for i in range(3):  # "retry" sorts before "timeout", but its group spans more pages
        write_page(root, f"retry{i}", f"Retry count is {i + 1}.", sources=(f"raw/r{i}.md",))
    write_page(root, "timeout-a", "Timeout is 30 s.", sources=("raw/t1.md",))
    write_page(root, "timeout-b", "Timeout is 60 s.", sources=("raw/t2.md",))
    fc = run_lint(root)["fact_collisions"]
    _assert(len(fc) == 4 and fc[0]["pages"] == ["timeout-a", "timeout-b"], f"fewest pages first: {fc}")


def test_drift_ack_one_of_two(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\noriginal a content long enough to count.\n")
    write_raw(root, "b.md", "# b\n\noriginal b content long enough to count.\n")
    write_page(root, "both", "Cites two. " * 8, sources=("raw/a.md", "raw/b.md"))
    commit(root, "ingest: initial"); time.sleep(1.1)
    write_raw(root, "a.md", "# a\n\nchanged a content long enough to count.\n")
    write_raw(root, "b.md", "# b\n\nchanged b content long enough to count.\n")
    commit(root, "ingest: both changed")
    run_lint(root, "--mark-reviewed", stdin=json.dumps({"drift": [{"page": "both", "raw": "raw/a.md"}]}))
    r = run_lint(root)
    _assert([[x["raw"] for x in d["drifted"]] for d in r["source_drift"]] == [["raw/b.md"]],
            f"unacknowledged raw stays: {r['source_drift']}")
    _assert(r["suppressed"]["drift"] == 1, f"suppressed: {r['suppressed']}")


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

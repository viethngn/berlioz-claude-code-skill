# llm-wiki Lean Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make lint detect overlap, source drift and contradicting facts by script, make ingest update existing pages instead of duplicating, and remove the external image-description endpoint.

**Architecture:** `lint.py` gains deterministic detectors and a `--fix` mode; a new stdlib `wiki_tools.py` does bookkeeping (locate, citing, index-sync, log) for both skills. Ingest scripts stop describing images themselves and instead hand the session model a pending list, plus the related/citing pages and raw diff it needs. Skill text is rewritten around those script outputs.

**Tech Stack:** Python 3 stdlib (`difflib`, `hashlib`, `subprocess` → `git`), existing ingest deps unchanged minus `google-genai` and `pillow`.

**Spec:** `docs/superpowers/specs/2026-11-20-llm-wiki-lean-rework-design.md`

## Global Constraints

- All paths below are relative to `plugins/llm-wiki/` unless they start with `docs/`.
- `skills/lint/scripts/lint.py` and `skills/lint/scripts/wiki_tools.py` are stdlib-only. No new third-party dependency anywhere.
- No script names a model or calls a model endpoint.
- Scripts never edit `raw/` file contents.
- Existing wikis keep working: flat `wiki/*.md` + `wiki/archive/`; header fields `**Summary**`, `**Sources**`, `**Last updated**`, optional `**Status**`; no new required page field; a leftover `nano_banana` block in `.wikirc.json` is ignored without error.
- Matching is character-based, not word-token-based. Sentence terminators: `. ! ? 。 ！ ？` and newline.
- A bare filename in a `Sources` block (`api-guide.md`) resolves to `raw/api-guide.md` when that file exists.
- Without `--fix` / `--mark-reviewed`, `lint.py` writes nothing.
- One citation format in all skill/template text: `(source: raw/<file>)`.
- Tests are plain-assert smoke scripts in `tests/`, run as `python3 plugins/llm-wiki/tests/<name>.py`, exit 0 on success (same style as `tests/smoke_bulk_ingest.py`).
- Work on a branch `llm-wiki-lean-rework` (create it before Task 1; the repo is on `main`). Commit messages end with the `Co-Authored-By` line the session provides.

## Review Focus

1. **Wiki root is not a git repo, or git has no commits yet** → `lint.py` still exits 0, `source_drift` is `[]`, `warnings` says why. (Task 2)
2. **Version strings, dates in changelogs and numbers inside fenced code blocks or tables** → not reported as fact collisions; only prose sentences are compared. (Task 3)
3. **A few hundred pages** → pair generation must not compute text similarity for all pairs; 300 synthetic pages finish in under 20 seconds. (Task 3)
4. **Aliased links `[[slug|shown text]]` during `--fix` unlink** → brackets replaced by `shown text`, not by the slug. (Task 5)
5. **`index.md` with hand-written entries in a non-standard shape** (`* [[a]]: text`, entries inside a table, a link in prose) → `index-sync` recognises the page as already listed and neither duplicates nor rewrites the line. (Task 6)

---

### Task 1: lint.py — orphan fix, exemptions, tolerant sources, compact output

**Files:**
- Modify: `skills/lint/scripts/lint.py`
- Create: `tests/smoke_lint.py`

**Interfaces:**
- Produces: `EXEMPT_PAGES = {"index", "log", "README", "readme"}` (rename of `EXEMPT_ORPHANS`, used by all checks);
  `resolve_source_paths(text: str, raw_dir: Path) -> List[str]` — the `raw/...` paths a page cites in its `Sources` block, with bare filenames resolved to `raw/<name>` when that file exists (unresolvable bare names are dropped);
  report keys unchanged except: `edges`/`inbound_counts` only with `--graph`; new key `warnings: List[str]` (always present).
- Produces (test helpers in `tests/smoke_lint.py`, reused by every later task): `make_wiki(tmp: Path) -> Path` (creates `wiki/`, `raw/`, `git init`, sets a local git user), `write_page(root, slug, body, sources=("raw/a.md",), summary="s")`, `write_raw(root, name, text)`, `commit(root, msg)`, `run_lint(root, *args) -> dict` (runs lint.py, parses stdout JSON), `_assert(cond, msg)`; `main()` calls each `test_*` in order with a fresh temp dir.

- [ ] **Step 1: Write the failing tests** in `tests/smoke_lint.py`

```python
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
    _assert(r["warnings"] == [] or isinstance(r["warnings"], list), "warnings key present")

def test_bare_source_filename_resolves(tmp):
    root = make_wiki(tmp)
    write_raw(root, "api-guide.md", "# g\n\ncontent content content content content content\n")
    write_page(root, "p", "Body text. " * 6, sources=("api-guide.md",))
    r = run_lint(root)
    _assert(r["missing_sources"] == [], "bare filename is not a missing source")
```

- [ ] **Step 2: Run** `python3 plugins/llm-wiki/tests/smoke_lint.py` — Expected: FAIL at `orphans: []`.

- [ ] **Step 3: Implement.** In `inbound_counts`, skip sources in `EXEMPT_PAGES`. Skip `EXEMPT_PAGES` in `find_format_violations` and `find_unsourced`. Add `resolve_source_paths`; make `find_missing_sources` and `cited_raw_slugs` use it for the Sources block (keep `cited_raw_slugs`' whole-text `raw/` scan). Add `--graph`; add `warnings` list threaded through `main`.

- [ ] **Step 4: Run** the test — Expected: PASS (prints `OK` per test, exit 0).

- [ ] **Step 5: Commit** `git commit -m "lint: fix orphan detection, exempt index/log, tolerant sources"`

---

### Task 2: lint.py — source drift (replaces stale check)

**Files:**
- Modify: `skills/lint/scripts/lint.py`, `tests/smoke_lint.py`

**Interfaces:**
- Consumes: `resolve_source_paths`, test helpers.
- Produces: `last_commit(wiki_root: Path, rel_path: str, skip_lint: bool = False) -> Optional[Tuple[str, int]]` — `(sha, committer_unix_time)` of the newest commit touching the path; with `skip_lint=True`, commits whose subject starts with `lint:` are ignored;
  `find_source_drift(pages, wiki_root, wiki_subdir, raw_dir, warnings) -> List[dict]`;
  report key `source_drift`: `[{"page": slug, "drifted": [{"raw": "raw/a.md", "page_commit": sha|None, "raw_commit": sha|None, "uncommitted": bool}]}]`.
- Removes: `find_stale`, `--stale-days`, report key `stale_pages`, `parse_last_updated` if unused afterwards.

- [ ] **Step 1: Write the failing tests**

```python
def test_source_drift(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nlimit is 1000 per minute for everyone here.\n")
    write_raw(root, "b.md", "# b\n\nother source content that is long enough.\n")
    write_page(root, "together", "Committed with its raw. " * 4, sources=("raw/b.md",))
    write_page(root, "drifts", "Cites a. " * 8, sources=("raw/a.md",))
    commit(root, "ingest: initial")
    time.sleep(1.1)
    write_raw(root, "a.md", "# a\n\nlimit is 5000 per minute for everyone here.\n")
    commit(root, "ingest: a changed")
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
```

- [ ] **Step 2: Run** — Expected: FAIL with `KeyError: 'source_drift'`.

- [ ] **Step 3: Implement.** One `git log --format=%H%x09%ct%x09%s -- <path>` per file via `subprocess` (cache results in a dict). Drift when `raw_time > page_time`; equal commit sha is not drift. `git status --porcelain -- raw` gives modified/untracked raws (→ `uncommitted: True`). An untracked page is never drifted. No repo / no commits → `[]` plus a warning containing the word `git`.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "lint: git-based source drift replaces mtime stale check"`

---

### Task 3: lint.py — overlap candidates and fact collisions

**Files:**
- Modify: `skills/lint/scripts/lint.py`, `tests/smoke_lint.py`

**Interfaces:**
- Consumes: `resolve_source_paths`, `page_body`, `wiki_links_in`.
- Produces: constants at top of file `MAX_CANDIDATES = 20`, `OVERLAP_THRESHOLD`, `SENTENCE_SIM_THRESHOLD`, weight constants (values are the builder's choice);
  `prose_sentences(body: str) -> List[str]` — sentences from prose only (fenced code blocks, table rows, headings and the `## Related pages` list are dropped), split on `. ! ? 。 ！ ？` and newlines;
  `related_pairs(pages, raw_dir) -> List[Tuple[dict, dict]]` — pairs sharing ≥1 cited raw, ≥1 outbound link, or a slug token of ≥4 chars; built from inverted indexes, never by scoring all pairs;
  `find_overlap(pairs, raw_dir, limit) -> List[dict]`; `find_fact_collisions(pairs) -> List[dict]`;
  CLI `--max-candidates N`;
  report keys `overlap_candidates`: `[{"pages": [a, b], "score": float, "shared_sources": int, "shared_links": int, "slug_sim": float, "text_sim": float}]` (pages sorted, list sorted by score desc), and `fact_collisions`: `[{"pages": [a, b], "sentences": [s_a, s_b], "differing": [[tokens_a], [tokens_b]]}]`.
- A numeric/date token is a maximal run matching `\d[\d.,:/\-]*\d|\d` optionally followed by `%`. A collision is two sentences whose text with those tokens removed has `SequenceMatcher` ratio ≥ `SENTENCE_SIM_THRESHOLD` and whose token lists differ. Sentences with no numeric token are skipped.

- [ ] **Step 1: Write the failing tests**

```python
def test_fact_collision_and_overlap(tmp):
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    write_page(root, "rate-limits", "The API rate limit is 1000 requests per minute for all tenants (source: raw/a.md). "
               "Tenants are isolated by key. Bursts are smoothed over ten seconds windows always.")
    write_page(root, "api-rate-limit", "The API rate limit is 5000 requests per minute for all tenants (source: raw/a.md). "
               "Tenants are isolated by key. Bursts are smoothed over ten seconds windows always.")
    write_page(root, "unrelated", "Onboarding takes three steps and ends with a welcome email being sent.", sources=("raw/a.md",))
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
    root = make_wiki(tmp)
    write_raw(root, "a.md", "# a\n\nsource body that is long enough to count.\n")
    for i in range(300):
        write_page(root, f"topic{i:03d}x", f"Page number {i} talks about subject {i} in some detail here. " * 5,
                   sources=(f"raw/s{i}.md",))
    t = time.time(); r = run_lint(root)
    _assert(time.time() - t < 20, "300 pages under 20s")
    _assert(len(r["overlap_candidates"]) <= 20, "candidate cap respected")
```

- [ ] **Step 2: Run** — Expected: FAIL with `KeyError: 'fact_collisions'`.

- [ ] **Step 3: Implement** the functions above. `text_sim` uses `SequenceMatcher(None, a, b).quick_ratio()` first and `ratio()` only when the quick ratio clears the threshold. Score is a weighted sum of the four components; only pairs ≥ `OVERLAP_THRESHOLD` are emitted, top `--max-candidates`.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "lint: deterministic overlap candidates and fact collisions"`

---

### Task 4: lint.py — review memory and `--since-last-lint`

**Files:**
- Modify: `skills/lint/scripts/lint.py`, `tests/smoke_lint.py`

**Interfaces:**
- Produces: file `wiki/.lint-reviewed.json`:
  `{"pairs": [{"pages": [a, b], "hash": h, "verdict": "fine"|"rejected"}], "collisions": [{"pages": [a, b], "hash": h, "verdict": ...}], "drift": [{"page": p, "raw": "raw/x.md", "hash": h}]}`.
  Hashes are sha256 hex: pair = text of page a + `\0` + text of page b (a, b sorted by slug); collision = the two sentences sorted, joined by `\0`; drift = bytes of the raw file.
- `lint.py --mark-reviewed` reads from stdin `{"pairs": [{"pages": [a, b], "verdict": v}], "collisions": [{"pages": [a, b], "sentences": [s1, s2], "verdict": v}], "drift": [{"page": p, "raw": r}]}` (every key optional), computes hashes, merges into the file (replacing an entry with the same key), prunes entries naming a page that no longer exists, writes sorted JSON, prints `{"recorded": n, "pruned": m}`, and runs no checks.
- A candidate, collision or drifted raw whose stored entry's hash still matches is dropped from the report. Report gains `suppressed: {"pairs": n, "collisions": n, "drift": n}`.
- `--since-last-lint`: baseline = newest commit whose subject starts with `lint:`. `overlap_candidates`/`fact_collisions` keep only pairs where at least one page file changed since the baseline (`git diff --name-only <baseline>..HEAD` plus uncommitted changes); `source_drift` keeps only pages where the page or a cited raw changed since it. No such commit → behaves as `--full`. `--full` is the default; passing both is an argparse error.

- [ ] **Step 1: Write the failing tests**

```python
def test_review_memory(tmp):
    root = collision_fixture(tmp)            # helper: the two rate-limit pages from Task 3, committed
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
    root = drift_fixture(tmp)                # helper: Task 2's drifted state
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
```

- [ ] **Step 2: Run** — Expected: FAIL (`unrecognized arguments: --mark-reviewed`).

- [ ] **Step 3: Implement** `load_reviewed(wiki_dir) -> dict`, `mark_reviewed(wiki_dir, raw_dir, payload) -> dict`, `changed_since_last_lint(wiki_root) -> Optional[set]` (set of repo-relative paths; `None` when no baseline), and the filtering in `main`. Add `stdin` support to the `run_lint` test helper.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "lint: review memory and --since-last-lint scope"`

---

### Task 5: lint.py — `--fix`

**Files:**
- Modify: `skills/lint/scripts/lint.py`, `tests/smoke_lint.py`

**Interfaces:**
- Produces: `apply_fixes(pages, wiki_dir, raw_dir) -> dict` returning `{"deleted_pages": [slug], "unlinked": [{"page": slug, "target": slug}], "repaired_sources": [{"page": slug, "from": path, "to": path}]}`. With `--fix`, `main` applies fixes, reloads pages, then runs all checks and adds the dict as report key `fixed`.
- Rules: delete pages in `empty_pages`; replace every `[[target]]` / `[[target|text]]` whose target does not resolve (including targets just deleted) with `text` if given, else `target`; rewrite a missing `Sources` path when exactly one `raw/*.md` file's stem equals the missing path's stem, or contains it as a whole hyphen-delimited segment run. `index.md` and `log.md` are never deleted; links inside `log.md` are left alone (append-only history).

- [ ] **Step 1: Write the failing test**

```python
def test_fix(tmp):
    root = make_wiki(tmp)
    write_raw(root, "api-guide-v2.md", "# g\n\nreal content that is long enough to count.\n")
    write_page(root, "stub", "Placeholder — add content when a source covers this concept.")
    write_page(root, "keeper", "See [[stub]] and [[ghost|the ghost page]] and [[real]]. " * 2, sources=("raw/api-guide.md",))
    write_page(root, "real", "Links back to [[keeper]]. " * 4, sources=("raw/api-guide-v2.md",))
    before = (root / "wiki/keeper.md").read_text()
    _assert(run_lint(root)["empty_pages"] and (root / "wiki/stub.md").exists(), "no --fix → no writes")
    _assert((root / "wiki/keeper.md").read_text() == before, "no --fix → page untouched")
    r = run_lint(root, "--fix")
    _assert(r["fixed"]["deleted_pages"] == ["stub"] and not (root / "wiki/stub.md").exists(), "stub deleted")
    text = (root / "wiki/keeper.md").read_text()
    _assert("[[stub]]" not in text and "the ghost page" in text and "[[ghost" not in text, "unlinked, alias text kept")  # Review Focus 4
    _assert("[[real]]" in text, "valid link kept")
    _assert("raw/api-guide-v2.md" in text, "source path repaired")
    _assert(r["broken_links"] == [] and r["empty_pages"] == [] and r["missing_sources"] == [], "report is post-fix")
```

- [ ] **Step 2: Run** — Expected: FAIL (`unrecognized arguments: --fix`).
- [ ] **Step 3: Implement** `apply_fixes` and the `--fix` flag.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "lint: --fix for judgment-free cleanups"`

---

### Task 6: wiki_tools.py — locate, citing, index-sync, log

**Files:**
- Create: `skills/lint/scripts/wiki_tools.py`
- Modify: `tests/smoke_lint.py`

**Interfaces:**
- Consumes (import from `lint.py`, same directory): `collect_pages`, `load_page`, `slug_from_path`, `page_body`, `wiki_links_in`, `archived_slugs`, `cited_raw_slugs`, `EXEMPT_PAGES`.
- Produces (importable functions and matching CLI subcommands, all `--wiki-root PATH`, JSON on stdout; wiki/raw dir names read from `.wikirc.json` the way `lint.py` does — factor that into `resolve_dirs(wiki_root) -> Tuple[Path, Path]` in `lint.py`):
  - `locate(wiki_dir: Path, terms: List[str], limit: int = 10) -> List[dict]` → `[{"page", "summary", "score"}]`; case-insensitive substring match; slug/H1 hit weighs more than Summary, Summary more than body; zero-score pages omitted. CLI: `locate TERM [TERM...]`.
  - `citing(wiki_dir: Path, raw_slug: str) -> List[str]` → sorted slugs. CLI: `citing RAW_SLUG`.
  - `index_sync(wiki_dir: Path) -> dict` → `{"added": [slug], "removed": [slug], "archived": [slug]}`. A page counts as listed when `[[slug]]` or `[[slug|...]]` appears anywhere in `index.md`. Missing active pages are appended as `- [[slug]] — <Summary>` under `## Uncategorized` (created at the end, before `## Archive` if that exists). A list line whose only link is to a page that no longer exists is removed; if the page now lives in `wiki/archive/`, the line is instead moved under `## Archive` (created at the end if absent) with the link rewritten to `[[archive/slug]]`. No other line changes. CLI: `index-sync`.
  - `append_log(wiki_dir: Path, op: str, title: str, bullets: List[str], today: Optional[str] = None) -> None` → inserts `## YYYY-MM-DD` (`ingest`) or `## YYYY-MM-DD (lint)` + `- <title>` + bullets directly under the H1; creates `log.md` with `# Wiki Log` if absent. CLI: `log --op ingest|lint --title TEXT [--bullet TEXT ...]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_locate_and_citing(tmp):
    root = collision_fixture(tmp)
    hits = run_tool(root, "locate", "rate limit")
    _assert({h["page"] for h in hits[:2]} == {"api-rate-limit", "rate-limits"}, f"locate: {hits}")
    _assert(run_tool(root, "locate", "zzzznothing") == [], "no hits → empty")
    _assert(run_tool(root, "citing", "a") == ["api-rate-limit", "rate-limits", "unrelated"], "citing")

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

def test_log(tmp):
    root = make_wiki(tmp)
    (root / "wiki/log.md").write_text("# Wiki Log\n\n## 2026-01-01\n- old entry\n")
    run_tool(root, "log", "--op", "lint", "--title", "Cleaned 2 pages", "--bullet", "Merged [[a]] into [[b]]")
    lines = (root / "wiki/log.md").read_text().splitlines()
    _assert(lines[0] == "# Wiki Log" and lines[2].endswith("(lint)") and lines[3] == "- Cleaned 2 pages", f"log: {lines[:6]}")
    _assert("## 2026-01-01" in lines and lines.index("## 2026-01-01") > 4, "old entries kept below")
```

- [ ] **Step 2: Run** — Expected: FAIL (`wiki_tools.py` not found). Add helper `run_tool(root, *args) -> Any`.
- [ ] **Step 3: Implement** `wiki_tools.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "llm-wiki: wiki_tools.py for locate, citing, index-sync, log"`

---

### Task 7: Images — remove the endpoint, off by default, pending list

**Files:**
- Delete: `skills/ingest/scripts/describe_image.py`
- Modify: `skills/ingest/scripts/config.py`, `ingest.py`, `prefetch.py`, `image_manifest.py`, `_deps.py`, `rate_limiter.py` (comments only), `requirements.txt`, `skills/create-wiki/templates/wikirc.example.json`, `skills/create-wiki/scripts/bootstrap.py:340`, `tests/smoke_bulk_ingest.py:192`
- Create: `tests/smoke_images.py`

**Interfaces:**
- `config.py`: remove the `nano_banana` default block, the `nano_banana*` accessors and its redaction. Add default `"images": {"describe": False}` and property `Config.describe_images -> bool`. A user file that still has `nano_banana` loads without error and the key is simply unused.
- `ingest.py`: flag `--describe-images` (single and bulk; passed through to `prefetch.py` as the same flag). `images_enabled = args.describe_images or cfg.describe_images`. When disabled: `extract_images.py` is not run, `do_image_diff_loop` is not called, summary `images` is `{"enabled": false}`. `_looks_like_placeholder` is deleted if nothing else uses it.
- `do_image_diff_loop(wiki_root: Path, slug: str) -> dict` returns `{"enabled": true, "new": n, "changed": n, "unchanged": n, "pending_images": [{"image": "<abs path>", "description": "<abs path to <stem>.md>", "status": "new"|"changed"}]}`. It no longer writes to the manifest and never calls a describer.
- `image_manifest.py` new subcommand: `--raw-dir R --slug S mark-described IMAGE_NAME [IMAGE_NAME...]` — for each name: require that `<stem>.md` exists beside it (else exit 1 naming the file), then `set_entry(name, sha256=<current hash>, description_file="<stem>.md")`, `mark_described`, save. Prints `{"marked": [names]}`.
- An image stays `new`/`changed` (pending) until `mark-described` records its sha.
- Unchanged source: early return stays as is; `--describe-images` only acts on new/changed sources, or on any source with `--force`.

- [ ] **Step 1: Write the failing tests** in `tests/smoke_images.py` (driver uses a temp wiki with a minimal `.wikirc.json` and a 1×1 PNG written from bytes; imports `ingest`/`config`/`image_manifest` from the scripts dir)

```python
def test_default_off_and_legacy_config(tmp):
    root = make_wiki(tmp, wikirc={"nano_banana": {"base_url": "x", "api_key": "y"}})
    cfg = config.load_config(root)
    _assert(cfg.describe_images is False, "off by default; legacy nano_banana block tolerated")
    _assert(not hasattr(cfg, "nano_banana_key"), "nano_banana accessors removed")
    _assert(not (SCRIPTS_DIR / "describe_image.py").exists(), "describe_image.py deleted")
    reqs = (PLUGIN_ROOT / "requirements.txt").read_text()
    _assert("google-genai" not in reqs and "pillow" not in reqs, "deps removed")

def test_pending_then_marked(tmp):
    root = make_wiki(tmp, wikirc={"images": {"describe": True}})
    img_dir = root / "raw/images/doc"; img_dir.mkdir(parents=True); (img_dir / "0.png").write_bytes(PNG_1X1)
    out = ingest.do_image_diff_loop(root, "doc")
    _assert([p["status"] for p in out["pending_images"]] == ["new"], f"pending: {out}")
    _assert(out["pending_images"][0]["description"].endswith("raw/images/doc/0.md"), "description path")
    _assert(ingest.do_image_diff_loop(root, "doc")["pending_images"], "still pending until marked")
    rc = run_script("image_manifest.py", ["--raw-dir", str(root / "raw"), "--slug", "doc", "mark-described", "0.png"])
    _assert(rc.returncode == 1, "mark-described refuses when description file is missing")
    (img_dir / "0.md").write_text("# Image description — 0.png\n\ntext\n")
    rc = run_script("image_manifest.py", ["--raw-dir", str(root / "raw"), "--slug", "doc", "mark-described", "0.png"])
    _assert(rc.returncode == 0 and ingest.do_image_diff_loop(root, "doc")["pending_images"] == [], "marked → not pending")
    (img_dir / "0.png").write_bytes(PNG_1X1 + b"x")
    _assert(ingest.do_image_diff_loop(root, "doc")["pending_images"][0]["status"] == "changed", "changed bytes → pending again")

def test_local_ingest_images_disabled(tmp):
    root = make_wiki(tmp)
    src = tmp / "note.md"; src.write_text("# Note\n\nSome body text for the local file ingest path.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit")
    _assert(out["images"] == {"enabled": False}, f"images summary: {out['images']}")
```

- [ ] **Step 2: Run** `python3 plugins/llm-wiki/tests/smoke_images.py` — Expected: FAIL (`describe_images` attribute missing).
- [ ] **Step 3: Implement** the changes listed under Interfaces. In `tests/smoke_bulk_ingest.py` drop the `nano_banana` block from `_write_wikirc`. Remove `"google.genai"` and `"PIL"` from `_deps.py`.
- [ ] **Step 4: Run** `smoke_images.py` and `smoke_bulk_ingest.py` — Expected: both PASS. Then `grep -rni "nano.banana\|gemini\|genai" plugins/llm-wiki --include='*.py' --include='*.json' --include='*.txt'` — Expected: no output.
- [ ] **Step 5: Commit** `git commit -m "ingest: drop external image endpoint; images off by default with pending list"`

---

### Task 8: Ingest context, source date, bulk tweaks

**Files:**
- Modify: `skills/ingest/scripts/ingest.py`, `prefetch.py`, `fetch_confluence.py`, `fetch_jira.py`, `fetch_slack.py`, `fetch_local.py`, `tests/smoke_images.py` → rename to `tests/smoke_ingest.py` (same file, broader scope), `tests/smoke_bulk_ingest.py`

**Interfaces:**
- Consumes: `wiki_tools.locate`, `wiki_tools.citing` (import by inserting `skills/lint/scripts` into `sys.path` relative to `__file__`), `lint.EMPTY_BODY_CHARS`, `lint.body_after_h1`.
- Produces: `build_context(wiki_root: Path, slug: str, status: Optional[str] = None) -> dict` in `ingest.py` returning
  `{"status": "new"|"changed", "related_pages": [{"page","summary","score"}], "citing_pages": [slug], "raw_diff": str, "raw_diff_truncated": bool}`.
  `related_pages` = `locate` on the raw file's H1 text and its `##` heading texts. `citing_pages`/`raw_diff` are filled only when status is `changed`. `RAW_DIFF_MAX_CHARS = 8000`.
  When `status` is given (single ingest): diff is `git diff HEAD -- raw/<slug>.md` after staging intent (`git diff --cached` if staged). When `status` is `None` (bulk): status is `changed` if `git log --format=%H -- raw/<slug>.md` has ≥2 commits, or 1 commit plus uncommitted changes; the diff is previous version vs current content.
- Single-ingest summary gains top-level key `context` (the dict above). New CLI mode `ingest.py --wiki-root R --context --slug S` prints the dict for bulk synthesis.
- `source.json` gains `source_date` (`YYYY-MM-DD`): Confluence from `version_when`, Jira from `updated_at`, Slack = date of the newest message in the range, local = mtime of the original file at copy time. It is part of the stable metadata (not volatile).
- `prefetch.py`: after a fetch succeeds, if `body_after_h1(raw text)` has fewer than `EMPTY_BODY_CHARS` non-whitespace chars, delete the just-written `raw/<slug>.md` + `.source.json`, set the item `raw_status = "skipped"` with `last_error = "empty body"`, emit `{"ref", "status": "skipped", "reason": "empty body"}`, and do not count it toward the circuit breaker. If `bulk_queue.py` validates statuses, add `skipped` to the allowed set.
- `ingest.py --commit-only` gains `--no-push`; the existing `--push-only` is used once at the end of a bulk job.

- [ ] **Step 1: Write the failing tests**

```python
def test_context_new_and_changed(tmp):          # tests/smoke_ingest.py
    root = make_wiki(tmp)
    write_page(root, "rate-limits", "The API rate limit is 1000 per minute (source: raw/api-guide.md). " * 2,
               sources=("raw/api-guide.md",), summary="API rate limits.")
    src = tmp / "api-guide.md"; src.write_text("# API Guide\n\n## Rate limits\n\nThe limit is 1000 per minute.\n")
    out = run_ingest(root, "--source", str(src), "--no-commit")
    ctx = out["context"]
    _assert(ctx["status"] == "new" and ctx["related_pages"][0]["page"] == "rate-limits", f"ctx: {ctx}")
    _assert(ctx["citing_pages"] == [] and ctx["raw_diff"] == "", "no diff for new source")
    meta = json.loads((root / "raw/api-guide.source.json").read_text())
    _assert(re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["source_date"]), "source_date present")
    git(root, "add", "-A"); git(root, "commit", "-m", "ingest: api-guide")
    src.write_text("# API Guide\n\n## Rate limits\n\nThe limit is 5000 per minute.\n")
    ctx = run_ingest(root, "--source", str(src), "--no-commit")["context"]
    _assert(ctx["status"] == "changed" and ctx["citing_pages"] == ["rate-limits"], f"changed ctx: {ctx}")
    _assert("-The limit is 1000" in ctx["raw_diff"] and "+The limit is 5000" in ctx["raw_diff"], "diff shown")
    git(root, "add", "-A"); git(root, "commit", "-m", "ingest: api-guide v2")
    bulk = run_ingest(root, "--context", "--slug", "api-guide")
    _assert(bulk["status"] == "changed" and "+The limit is 5000" in bulk["raw_diff"], "bulk context from git history")
```

In `tests/smoke_bulk_ingest.py`, add one mock page whose storage body renders to nothing but whitespace/an empty macro wrapper (non-empty HTML so the fetcher does not error), and assert after prefetch: that item has `raw_status == "skipped"`, no `raw/<slug>.md` exists for it, and the other items are still `done`.

- [ ] **Step 2: Run** both smoke tests — Expected: FAIL (`KeyError: 'context'`; bulk assertion on `skipped`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `smoke_ingest.py`, `smoke_bulk_ingest.py`, `smoke_lint.py` — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "ingest: related/citing/diff context, source_date, skip empty bulk items"`

---

### Task 9: CLAUDE.md managed block and upgrade command

**Files:**
- Modify: `skills/create-wiki/templates/CLAUDE.md`, `skills/create-wiki/scripts/bootstrap.py`, `skills/lint/scripts/lint.py`, `tests/smoke_lint.py`
- Create: `tests/smoke_upgrade.py`

**Interfaces:**
- Markers, exact: `<!-- llm-wiki:begin -->` and `<!-- llm-wiki:end -->`, each on its own line.
- Template `CLAUDE.md`: title and `## Purpose` stay above the block. Inside the block, replacing the current sections: folder structure; "to ingest use `/ingest`, to lint use `/lint`, their instructions take precedence over anything here"; the page header format; the citation format `(source: raw/<file>)`; question answering (unchanged content); the rules list minus anything the skills now own (no per-source summary page, no "10-15 pages", no hand-editing `index.md`/`log.md` — say they are maintained by `wiki_tools.py`).
- `bootstrap.py`: `upgrade_claude_md(text: str, block: str) -> str` (pure) and CLI `--upgrade --target <wiki-root>`. `block` is the template's marker-to-marker text, rendered with the same context as bootstrap. Markers present → replace marker-to-marker. Markers absent → delete each section whose heading is exactly one of `## Folder structure`, `## Ingest workflow`, `## Page format`, `## Citation rules`, `## Question answering`, `## Lint`, `## Rules` (heading through the line before the next `## ` heading or EOF) and insert the block where the first deleted section began; if none of those headings exist, append the block at the end. `--upgrade` writes only `CLAUDE.md`, prints a unified diff (`difflib.unified_diff`) or `CLAUDE.md already up to date`, does not commit, and exits 1 with a message when `CLAUDE.md` is missing. `--title` is not required with `--upgrade`.
- `lint.py`: when `<wiki_root>/CLAUDE.md` exists and lacks the begin marker, append to `warnings`: `CLAUDE.md has no llm-wiki managed block — run bootstrap.py --upgrade --target <wiki_root>`.

- [ ] **Step 1: Write the failing tests** in `tests/smoke_upgrade.py`

```python
OLD = ("# My Wiki\n\n## Purpose\n\nMine.\n\n## Folder structure\n\nold\n\n## Ingest workflow\n\n"
       "3. Create a summary page in `wiki/` named after the source\n\n## Team conventions\n\nKeep me.\n\n"
       "## Rules\n\n- old rule\n")

def test_upgrade_old_style(tmp):
    (tmp / "CLAUDE.md").write_text(OLD)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and "+<!-- llm-wiki:begin -->" in rc.stdout, "prints diff")
    _assert("summary page" not in text and "old rule" not in text, "old template sections removed")
    _assert("## Purpose\n\nMine." in text and "## Team conventions\n\nKeep me." in text, "custom content kept")
    _assert(text.index("## Purpose") < text.index("<!-- llm-wiki:begin -->") < text.index("## Team conventions"), "block placed where first old section was")
    _assert(text.count("<!-- llm-wiki:begin -->") == 1 and text.count("<!-- llm-wiki:end -->") == 1, "one block")
    rc2 = run_bootstrap("--upgrade", "--target", str(tmp))
    _assert("already up to date" in rc2.stdout and (tmp / "CLAUDE.md").read_text() == text, "idempotent")
    _assert(sorted(p.name for p in tmp.iterdir()) == ["CLAUDE.md"], "no other file written")

def test_upgrade_missing_file(tmp):
    _assert(run_bootstrap("--upgrade", "--target", str(tmp)).returncode == 1, "missing CLAUDE.md → exit 1")

def test_fresh_bootstrap_has_block(tmp):
    run_bootstrap("--target", str(tmp / "w"), "--title", "T", "--no-git", *SKIP_DEPS_FLAGS)
    _assert("<!-- llm-wiki:begin -->" in (tmp / "w/CLAUDE.md").read_text(), "new wikis get the managed block")
```

(`SKIP_DEPS_FLAGS`: whatever existing flag(s) `bootstrap.py` offers to skip the dependency install — read `main()`; if there is none, drop this third test's bootstrap run and assert on the template file's text instead.)

And in `tests/smoke_lint.py`:

```python
def test_claude_md_warning(tmp):
    root = make_wiki(tmp); (root / "CLAUDE.md").write_text("# W\n\n## Rules\n- old\n")
    _assert(any("--upgrade" in w for w in run_lint(root)["warnings"]), "warns without managed block")
    (root / "CLAUDE.md").write_text("# W\n<!-- llm-wiki:begin -->\nx\n<!-- llm-wiki:end -->\n")
    _assert(not any("--upgrade" in w for w in run_lint(root)["warnings"]), "no warning with block")
```

- [ ] **Step 2: Run** both — Expected: FAIL (`unrecognized arguments: --upgrade`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `smoke_upgrade.py`, `smoke_lint.py` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "create-wiki: managed CLAUDE.md block and --upgrade"`

---

### Task 10: Skill, reference and README text

**Files:**
- Modify: `skills/ingest/SKILL.md`, `skills/ingest/references/page-format.md`, `skills/ingest/references/setup.md`, `skills/ingest/references/local-files.md`, `skills/lint/SKILL.md`, `skills/create-wiki/SKILL.md`, `README.md`
- Create: `tests/smoke_docs.py`

**Interfaces:**
- Consumes: every CLI defined in Tasks 1–9; the docs must use those exact flags and JSON keys.

Content decisions (wording is the writer's):

`skills/ingest/SKILL.md`
- Frontmatter description: remove nano-banana; say images are described by the session model only when enabled; drop "Requires … nano-banana endpoints".
- Opening line: "These rules take precedence over workflow text in the wiki's `CLAUDE.md`."
- Phase 3 (single and bulk) becomes: (1) read `context.related_pages`; update those pages by default; (2) create a page only when the topic has no existing home and the source has enough substance for its own page; (3) no summary page per source, no stub pages — mention unknown concepts in plain text; (4) for `status: changed`, read `raw_diff`, open each `citing_pages` page and rewrite the affected claims in place, never append a second version; (5) conflict with a claim from a different source: the source with the later `source_date` in its `.source.json` wins and replaces the claim, keeping the citation; equal or missing dates → keep both with an explicit note; (6) cite as `(source: raw/<file>)`; (7) run `wiki_tools.py index-sync` and `wiki_tools.py log --op ingest --title … --bullet …` instead of editing `index.md`/`log.md` by hand; a newly added line may then be moved out of `## Uncategorized` when an obviously fitting section exists.
- Image step, shown only as "when `images.enabled` is true": for each `pending_images` entry read the image file, write the description to the given path using four parts (image type; visible text verbatim; structure and content in 3–8 sentences; up to 8 key facts), then `image_manifest.py --raw-dir … --slug … mark-described <names>`. If the session model cannot read images, report them as skipped and continue.
- Bulk: per item run `ingest.py --context --slug <slug>`; commit per item with `--commit-only --no-push`; one `--push-only` after the last item; items with `raw_status: skipped` are reported, not synthesised.
- Concrete rules: remove nano-banana layers and rate-limit lines; document `images.describe` / `--describe-images` (new/changed sources only, or any with `--force`), `source_date`, and that with images off nothing is downloaded while `image_hints` stay in `source.json`.

`skills/ingest/references/page-format.md`
- Delete the stub-page block under "Wiki-links" and replace with "no stubs; plain text until a page exists". Replace the anti-pattern "One giant page: split into 5-10 focused pages" with "split only when a page covers clearly separate topics". Citation examples use `raw/` paths. index/log sections describe the `wiki_tools.py` commands.

`skills/lint/SKILL.md` — replace Phases 1–6 with the seven steps in the spec's "Lint `SKILL.md`" section, using: `lint.py --fix --since-last-lint` (or `--full` on first run / when a full audit is asked), the report keys `fact_collisions`, `overlap_candidates`, `source_drift`, `orphans`, `empty_raw`, `fixed`, `suppressed`, `warnings`; verdicts recorded with `lint.py --mark-reviewed` (give the stdin JSON shape); merge = fold unique content into the surviving page, repoint links, `git mv` the other to `wiki/archive/`; re-run `lint.py` and require `broken_links`, `empty_pages`, `format_violations`, `missing_sources` all empty; `wiki_tools.py index-sync` and `log --op lint`; grouped commits all starting with `lint:`; push via `ingest.py --push-only`. State that nothing semantic (merge, delete of a non-empty page, rewrite, raw prune) is applied before the single approval report is approved, and that these rules take precedence over the wiki's `CLAUDE.md`. Update the frontmatter description and the options/design-notes sections to match (no `--stale-days`).

`skills/create-wiki/SKILL.md` — remove nano-banana mentions; add an "Upgrade an existing wiki's CLAUDE.md" section with the `bootstrap.py --upgrade --target` command and what it keeps/replaces, including the caveat that edits made inside old template sections are replaced.

`README.md`, `references/setup.md`, `references/local-files.md` — remove nano-banana/Gemini setup; describe images-off default and the lint/ingest behaviour in a few lines each.

- [ ] **Step 1: Write the failing test** `tests/smoke_docs.py`

```python
DOCS = [p for p in PLUGIN_ROOT.rglob("*.md")] + [PLUGIN_ROOT / "skills/create-wiki/templates/wikirc.example.json"]

def test_no_stale_text():
    banned = ["nano-banana", "nano_banana", "gemini", "--stale-days", "stale_pages", "10-15 wiki pages",
              "Create a summary page", "create a stub page", "(source: filename.md)", "5-10 focused pages"]
    for p in DOCS:
        low = p.read_text(encoding="utf-8").lower()
        for b in banned:
            _assert(b.lower() not in low, f"{p.relative_to(PLUGIN_ROOT)} still contains {b!r}")

def test_required_text():
    ingest = (PLUGIN_ROOT / "skills/ingest/SKILL.md").read_text()
    lint = (PLUGIN_ROOT / "skills/lint/SKILL.md").read_text()
    for s in ["related_pages", "citing_pages", "raw_diff", "source_date", "pending_images", "mark-described",
              "index-sync", "--describe-images", "--context", "--no-push", "take precedence"]:
        _assert(s in ingest, f"ingest SKILL.md missing {s}")
    for s in ["--fix", "--since-last-lint", "--mark-reviewed", "fact_collisions", "overlap_candidates",
              "source_drift", "index-sync", "take precedence"]:
        _assert(s in lint, f"lint SKILL.md missing {s}")
    _assert("--upgrade" in (PLUGIN_ROOT / "skills/create-wiki/SKILL.md").read_text(), "create-wiki documents --upgrade")
```

- [ ] **Step 2: Run** `python3 plugins/llm-wiki/tests/smoke_docs.py` — Expected: FAIL on the first banned string.
- [ ] **Step 3: Rewrite the documents** per the content decisions above.
- [ ] **Step 4: Run** all five smoke tests (`smoke_lint`, `smoke_ingest`, `smoke_bulk_ingest`, `smoke_upgrade`, `smoke_docs`) — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "llm-wiki: rewrite skill and docs for script-first lint and lean ingest"`

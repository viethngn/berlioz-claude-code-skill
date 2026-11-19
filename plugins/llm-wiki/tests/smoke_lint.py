#!/usr/bin/env python3
"""Smoke tests for skills/lint/scripts/lint.py.

Run:
    python3 plugins/llm-wiki/tests/smoke_lint.py

Each test_* gets a fresh temp dir. Exits 0 on success, non-zero on failure.
Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


def run_lint(root: Path, *args: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(LINT), "--wiki-root", str(root), *args],
        capture_output=True, text=True,
    )
    _assert(out.returncode == 0, f"lint.py exit {out.returncode}: {out.stderr.strip()}")
    return json.loads(out.stdout)


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

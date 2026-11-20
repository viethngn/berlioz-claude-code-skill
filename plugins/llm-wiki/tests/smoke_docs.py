#!/usr/bin/env python3
"""Smoke test for the llm-wiki skill and reference text.

Run:
    python3 plugins/llm-wiki/tests/smoke_docs.py

Exits 0 on success. Stdlib only. Checks that no document still describes a
removed feature, and that the skills name the flags and keys the scripts use.
CHANGELOG.md is history and is not checked.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
DOCS = sorted(PLUGIN_ROOT.rglob("*.md")) + [PLUGIN_ROOT / "skills/create-wiki/templates/wikirc.example.json"]

BANNED = ["nano-banana", "nano_banana", "gemini", "--stale-days", "stale_pages", "10-15 wiki pages",
          "Create a summary page", "create a stub page", "(source: filename.md)", "5-10 focused pages",
          'model: "haiku"']


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _check_banned(name: str, text: str) -> None:
    low = text.lower()
    for b in BANNED:
        _assert(b.lower() not in low, f"{name} still contains {b!r}")


def test_no_stale_text() -> None:
    for p in DOCS:
        _check_banned(str(p.relative_to(PLUGIN_ROOT)), p.read_text(encoding="utf-8"))


def test_repo_level_llm_wiki_text() -> None:
    # The repo also ships a separate `nano-banana-pro` plugin, so only the
    # llm-wiki parts of the root README and the marketplace are checked.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^### `llm-wiki`\n(.*?)(?=^### |\Z)", readme, re.S | re.M)
    _assert(m is not None, "root README has an llm-wiki section")
    _check_banned("README.md (llm-wiki section)", m.group(1))

    market = json.loads((REPO_ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "llm-wiki")
    _check_banned("marketplace.json (llm-wiki entry)", json.dumps(entry))
    _assert(market["metadata"]["version"] == "2.0.0", "marketplace version is 2.0.0")


def test_required_text() -> None:
    ingest = (PLUGIN_ROOT / "skills/ingest/SKILL.md").read_text(encoding="utf-8")
    lint = (PLUGIN_ROOT / "skills/lint/SKILL.md").read_text(encoding="utf-8")
    for s in ["related_pages", "citing_pages", "raw_diff", "source_date", "pending_images", "mark-described",
              "index-sync", "--describe-images", "--context", "--job-id", "take precedence"]:
        _assert(s in ingest, f"ingest SKILL.md missing {s}")
    for s in ["--fix", "--since-last-lint", "--mark-reviewed", "--allow-dirty", "fact_collisions",
              "overlap_candidates", "source_drift", "warnings", "index-sync", "take precedence"]:
        _assert(s in lint, f"lint SKILL.md missing {s}")
    _assert("--upgrade" in (PLUGIN_ROOT / "skills/create-wiki/SKILL.md").read_text(encoding="utf-8"),
            "create-wiki documents --upgrade")


def main() -> int:
    tests = [test_no_stale_text, test_repo_level_llm_wiki_text, test_required_text]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print("smoke_docs: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

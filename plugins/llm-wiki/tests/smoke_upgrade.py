#!/usr/bin/env python3
"""Smoke test for `bootstrap.py --upgrade` (the managed CLAUDE.md block).

Run:
    python3 plugins/llm-wiki/tests/smoke_upgrade.py

Exits 0 on success. Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
BOOTSTRAP = PLUGIN / "skills" / "create-wiki" / "scripts" / "bootstrap.py"
SKIP_DEPS_FLAGS = ("--skip-deps",)
BEGIN, END = "<!-- llm-wiki:begin -->", "<!-- llm-wiki:end -->"

OLD = ("# My Wiki\n\n## Purpose\n\nMine.\n\n## Folder structure\n\nold\n\n## Ingest workflow\n\n"
       "3. Create a summary page in `wiki/` named after the source\n\n## Team conventions\n\nKeep me.\n\n"
       "## Rules\n\n- old rule\n")

# The real pre-2.0 shape: `## Page format` holds a fenced example with a
# `## Related pages` heading, which must not end the section early.
OLD_FENCED = ("# W\n\n## Purpose\n\nMine.\n\n## Page format\n\n```markdown\n# Page Title\n\n"
              "old body\n\n## Related pages\n\n- [[old-link]]\n```\n\nafter the fence\n\n"
              "## Team conventions\n\nKeep me.\n")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def run_bootstrap(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BOOTSTRAP), *args], capture_output=True, text=True)


def test_upgrade_old_style(tmp: Path) -> None:
    (tmp / "CLAUDE.md").write_text(OLD)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and "+" + BEGIN in rc.stdout, f"prints diff: {rc.stdout}{rc.stderr}")
    _assert("summary page" not in text and "old rule" not in text, "old template sections removed")
    _assert("## Purpose\n\nMine." in text and "## Team conventions\n\nKeep me." in text, "custom content kept")
    _assert(text.index("## Purpose") < text.index(BEGIN) < text.index("## Team conventions"),
            "block placed where first old section was")
    _assert(text.count(BEGIN) == 1 and text.count(END) == 1, "one block")
    rc2 = run_bootstrap("--upgrade", "--target", str(tmp))
    _assert("already up to date" in rc2.stdout and (tmp / "CLAUDE.md").read_text() == text, "idempotent")
    _assert(sorted(p.name for p in tmp.iterdir()) == ["CLAUDE.md"], "no other file written")


def test_upgrade_fenced_heading(tmp: Path) -> None:
    (tmp / "CLAUDE.md").write_text(OLD_FENCED)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0, rc.stderr)
    _assert("old body" not in text and "old-link" not in text and "after the fence" not in text,
            "fenced ## heading did not end the Page format section")
    _assert(text.index("## Purpose") < text.index(BEGIN) < text.index("## Team conventions\n\nKeep me."),
            "block placed, custom content kept")
    _assert(text.count(BEGIN) == 1, "one block")


def test_upgrade_replaces_existing_block(tmp: Path) -> None:
    (tmp / "CLAUDE.md").write_text(f"# W\n\nmine above\n\n{BEGIN}\nstale\n{END}\n\nmine below\n")
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and "stale" not in text, "block replaced")
    _assert(text.startswith("# W\n\nmine above\n\n" + BEGIN) and text.endswith(END + "\n\nmine below\n"),
            "content outside markers untouched")


def test_upgrade_no_known_sections(tmp: Path) -> None:
    (tmp / "CLAUDE.md").write_text("# W\n\n## Mine\n\nkeep\n")
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0, rc.stderr)
    _assert(text.startswith("# W\n\n## Mine\n\nkeep\n") and text.rstrip().endswith(END), "block appended")
    _assert("no known template sections" in rc.stderr, "appended note on stderr")


def test_upgrade_missing_file(tmp: Path) -> None:
    _assert(run_bootstrap("--upgrade", "--target", str(tmp)).returncode == 1, "missing CLAUDE.md -> exit 1")
    _assert(not (tmp / "CLAUDE.md").exists(), "missing CLAUDE.md is not created")


def test_upgrade_begin_without_end(tmp: Path) -> None:
    src = f"# W\n\n{BEGIN}\nhalf a block\n\n## Mine\n\nkeep\n"
    (tmp / "CLAUDE.md").write_text(src)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    _assert(rc.returncode == 1 and (tmp / "CLAUDE.md").read_text() == src, "begin without end -> exit 1, unchanged")


def test_upgrade_level1_heading_ends_drop(tmp: Path) -> None:
    tail = "# Appendix\n\nMY TEXT\n\n### Sub\n\nmore\n"
    (tmp / "CLAUDE.md").write_text("# W\n\n## Rules\n- old\n\n### old sub\n\nold detail\n\n" + tail)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and text.endswith(END + "\n\n" + tail), "# section after ## Rules kept byte-for-byte")
    _assert("old detail" not in text and "- old\n" not in text, "### subsection dropped with its section")


def test_upgrade_inline_marker_mention(tmp: Path) -> None:
    above = f"# W\n\nThe plugin owns everything between `{BEGIN}` and `{END}`.\n\n"
    (tmp / "CLAUDE.md").write_text(above + f"{BEGIN}\nstale\n{END}\n")
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and text.startswith(above + BEGIN + "\n"), "inline mention untouched")
    _assert("stale" not in text and text.count(BEGIN) == 2, "only the real block replaced")


def test_upgrade_crlf(tmp: Path) -> None:
    user = "# W\r\n\r\n## Purpose\r\n\r\nMine.\r\n\r\n"
    (tmp / "CLAUDE.md").write_bytes((user + "## Rules\r\n\r\n- old\r\n\r\n## Team\r\n\r\nKeep.\r\n").encode())
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    raw = (tmp / "CLAUDE.md").read_bytes().decode()
    _assert(rc.returncode == 0 and raw.replace("\r\n", "") .count("\n") == 0, "no bare LF introduced")
    _assert(raw.startswith(user + BEGIN + "\r\n") and raw.endswith(END + "\r\n\r\n## Team\r\n\r\nKeep.\r\n"),
            "user lines unchanged")
    _assert("-# W" not in rc.stdout and "-## Purpose" not in rc.stdout, "diff shows only real changes")
    rc2 = run_bootstrap("--upgrade", "--target", str(tmp))
    _assert("already up to date" in rc2.stdout, "CRLF idempotent")


def test_upgrade_nested_fence(tmp: Path) -> None:
    fenced = "````md\n```\n## Rules\nINSIDE\n```\n````\n"
    (tmp / "CLAUDE.md").write_text("# W\n\n## Mine\n\n" + fenced)
    rc = run_bootstrap("--upgrade", "--target", str(tmp))
    text = (tmp / "CLAUDE.md").read_text()
    _assert(rc.returncode == 0 and text.startswith("# W\n\n## Mine\n\n" + fenced), "nothing inside the outer fence dropped")


def test_fresh_bootstrap_has_block(tmp: Path) -> None:
    run_bootstrap("--target", str(tmp / "w"), "--title", "T", "--no-git", *SKIP_DEPS_FLAGS)
    text = (tmp / "w/CLAUDE.md").read_text()
    _assert(BEGIN in text and "# T" in text, "new wikis get the managed block")
    _assert(text.index("## Purpose") < text.index(BEGIN), "Purpose stays above the block")


def main() -> int:
    tests = [test_upgrade_old_style, test_upgrade_fenced_heading, test_upgrade_replaces_existing_block,
             test_upgrade_no_known_sections, test_upgrade_missing_file, test_upgrade_begin_without_end,
             test_upgrade_level1_heading_ends_drop, test_upgrade_inline_marker_mention, test_upgrade_crlf,
             test_upgrade_nested_fence, test_fresh_bootstrap_has_block]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"ok  {t.__name__}")
    print("smoke_upgrade: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Smoke test for image handling in ingest.

Images are off by default and no script calls a description endpoint. When
enabled, `do_image_diff_loop` only reports which images still need a
description (`pending_images`); the session writes `<stem>.md` and records it
with `image_manifest.py mark-described`.

Run:
    ~/.llm-wiki-venv/bin/python3 plugins/llm-wiki/tests/smoke_images.py

Exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
    # Unchanged source keeps its early return.
    out = run_ingest(root, "--source", str(src), "--no-commit", "--describe-images")
    _assert(out["fetch"].get("status") == "unchanged", f"unchanged: {out['fetch']}")
    _assert(out["images"] == {"enabled": False}, f"unchanged images: {out['images']}")
    print("[OK] local ingest: images disabled by default, flag and --skip-images honored")


def main() -> int:
    for test in (test_default_off_and_legacy_config, test_pending_then_marked, test_local_ingest_images_disabled):
        with tempfile.TemporaryDirectory() as d:
            test(Path(d))
    print("ALL IMAGE SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""SHA-256 image manifest — the source of truth for image diff detection.

Every image under raw/images/<slug>/ has an entry in .manifest.json:

    {
      "images": {
        "0.png": {
          "sha256": "abcdef...",
          "described_at": "2026-07-02T18:00:00Z",
          "description_file": "0.md",
          "source_url": "https://... (optional)"
        }
      }
    }

Stdlib only. Every caller uses:

    from image_manifest import load_manifest, classify, update_entry, save_manifest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional


MANIFEST_FILENAME = ".manifest.json"
CHUNK = 1024 * 64


class Manifest:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self.data.setdefault("images", {})

    @property
    def images(self) -> dict:
        return self.data["images"]

    def entry(self, image_name: str) -> Optional[dict]:
        return self.images.get(image_name)

    def set_entry(
        self,
        image_name: str,
        sha256: str,
        description_file: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> None:
        existing = self.images.get(image_name, {})
        entry = {
            "sha256": sha256,
            "described_at": existing.get("described_at"),
            "description_file": description_file
            or existing.get("description_file"),
        }
        if source_url:
            entry["source_url"] = source_url
        elif existing.get("source_url"):
            entry["source_url"] = existing["source_url"]
        self.images[image_name] = {k: v for k, v in entry.items() if v is not None}

    def mark_described(self, image_name: str) -> None:
        entry = self.images.setdefault(image_name, {})
        entry["described_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")


def slug_dir(raw_dir: Path, slug: str) -> Path:
    return raw_dir / "images" / slug


def manifest_path(raw_dir: Path, slug: str) -> Path:
    return slug_dir(raw_dir, slug) / MANIFEST_FILENAME


def load_manifest(raw_dir: Path, slug: str) -> Manifest:
    path = manifest_path(raw_dir, slug)
    if not path.exists():
        return Manifest(path, {"images": {}})
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {"images": {}}
    return Manifest(path, data)


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def classify(manifest: Manifest, image_name: str, sha256: str) -> str:
    """Return 'new', 'changed', or 'unchanged'."""
    entry = manifest.entry(image_name)
    if entry is None:
        return "new"
    if entry.get("sha256") != sha256:
        return "changed"
    if not entry.get("description_file"):
        return "new"
    return "unchanged"


def scan_slug_dir(raw_dir: Path, slug: str) -> Dict[str, str]:
    """Return {image_name: sha256} for every image file in the slug's image dir."""
    d = slug_dir(raw_dir, slug)
    if not d.exists():
        return {}
    out: Dict[str, str] = {}
    for entry in sorted(d.iterdir()):
        if entry.is_file() and not entry.name.startswith("."):
            if entry.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                out[entry.name] = hash_file(entry)
    return out


def _cmd_status(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir).resolve()
    manifest = load_manifest(raw_dir, args.slug)
    scanned = scan_slug_dir(raw_dir, args.slug)

    if not scanned:
        print(json.dumps({"slug": args.slug, "images": {}, "summary": {"new": 0, "changed": 0, "unchanged": 0}}, indent=2))
        return 0

    per_image = {}
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for name, sha in sorted(scanned.items()):
        status = classify(manifest, name, sha)
        per_image[name] = {"status": status, "sha256": sha}
        counts[status] += 1

    print(
        json.dumps(
            {"slug": args.slug, "images": per_image, "summary": counts},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir).resolve()
    manifest = load_manifest(raw_dir, args.slug)
    image_path = Path(args.image).resolve()
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 1
    sha = hash_file(image_path)
    manifest.set_entry(
        image_path.name,
        sha256=sha,
        description_file=args.description_file,
        source_url=args.source_url,
    )
    if args.mark_described:
        manifest.mark_described(image_path.name)
    manifest.save()
    print(
        json.dumps(
            {
                "slug": args.slug,
                "image": image_path.name,
                "sha256": sha,
                "manifest": str(manifest.path),
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage SHA-256 image manifests")
    parser.add_argument("--raw-dir", required=True, help="Path to raw/ directory")
    parser.add_argument("--slug", required=True, help="Source slug")
    sub = parser.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="Report new/changed/unchanged per image")

    rec = sub.add_parser("record", help="Record hash + description file for one image")
    rec.add_argument("--image", required=True, help="Path to image file")
    rec.add_argument("--description-file", default=None)
    rec.add_argument("--source-url", default=None)
    rec.add_argument(
        "--mark-described",
        action="store_true",
        help="Set described_at to now",
    )

    args = parser.parse_args()
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "record":
        return _cmd_record(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

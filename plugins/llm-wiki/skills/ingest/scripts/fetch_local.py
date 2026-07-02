#!/usr/bin/env python3
"""Ingest a local file into raw/.

Supported formats:
    .md / .markdown  - verbatim copy
    .txt             - wrapped in a code fence
    .html / .htm     - markdownify
    .pdf             - pypdf text extraction
    .docx            - python-docx paragraphs + tables
    .png/.jpg/.webp/.gif - the file IS the source (goes straight to raw/images/)

Usage:
    python3 fetch_local.py --wiki-root /path/to/wiki --path /path/to/source
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Optional

from _deps import require

require(["markdownify", "bs4"])

from bs4 import BeautifulSoup
from markdownify import markdownify

from config import ConfigError, load_config
from raw_store import write_raw_if_changed


_slug_re = re.compile(r"[^\w\-]+", re.UNICODE)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def slugify(name: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    dashed = _slug_re.sub("-", lowered).strip("-")
    return dashed or "untitled"


def parse_md(path: Path) -> tuple[str, list[Path]]:
    body = path.read_text(encoding="utf-8", errors="replace")
    if not body.startswith("#"):
        title = path.stem.replace("-", " ").replace("_", " ").title()
        body = f"# {title}\n\n" + body
    return body, []


def parse_txt(path: Path) -> tuple[str, list[Path]]:
    body = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("-", " ").replace("_", " ").title()
    fenced = "```\n" + body.rstrip() + "\n```\n"
    return f"# {title}\n\n{fenced}", []


def parse_html(path: Path) -> tuple[str, list[str]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    image_hints = [img.get("src", "") for img in soup.find_all("img") if img.get("src")]
    title = (soup.title.string if soup.title else path.stem) or path.stem
    title = title.strip()
    body_html = str(soup.body) if soup.body else str(soup)
    md = markdownify(body_html, heading_style="ATX", bullets="-").strip()
    body = f"# {title}\n\n{md}\n"
    return body, image_hints


def parse_pdf(path: Path, images_dir: Path) -> tuple[str, list[Path]]:
    require(["pypdf"])
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(str(path))
    except PyPdfError as e:
        raise SystemExit(f"ERROR: could not read PDF {path}: {e}")

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise SystemExit(
                    f"ERROR: PDF {path} is encrypted with a password. "
                    "Decrypt it first."
                )
        except NotImplementedError:
            raise SystemExit(
                f"ERROR: PDF {path} uses an unsupported encryption scheme."
            )

    parts: list[str] = []
    saved_images: list[Path] = []
    idx = 0

    for page_num, page in enumerate(reader.pages, start=1):
        text = ""
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""

        parts.append(f"## Page {page_num}\n\n{text.strip()}\n")

        try:
            page_images = list(page.images or [])
        except Exception:  # noqa: BLE001
            page_images = []

        for image_object in page_images:
            data = getattr(image_object, "data", None)
            name = getattr(image_object, "name", None) or f"page{page_num}-img"
            if data is None:
                continue
            ext = Path(name).suffix.lower() or ".png"
            if ext not in IMAGE_EXTS:
                ext = ".png"
            image_path = images_dir / f"{idx}{ext}"
            images_dir.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(data)
            saved_images.append(image_path)
            idx += 1

    title = path.stem.replace("-", " ").replace("_", " ").title()
    body = f"# {title}\n\n" + "\n---\n\n".join(parts)
    return body, saved_images


def parse_docx(path: Path, images_dir: Path) -> tuple[str, list[Path]]:
    require(["docx"])
    from docx import Document

    doc = Document(str(path))
    lines: list[str] = []

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        style = getattr(para.style, "name", "") or ""
        if not text:
            lines.append("")
            continue
        if style.startswith("Heading"):
            level_str = style.replace("Heading", "").strip() or "1"
            try:
                level = max(1, min(6, int(level_str)))
            except ValueError:
                level = 2
            lines.append("#" * level + " " + text)
        else:
            lines.append(text)
        lines.append("")

    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
            rows.append(cells)
        if not rows:
            continue
        widths = [max(len(c) for c in col) for col in zip(*rows)]
        header = "| " + " | ".join(rows[0]) + " |"
        sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
        body_rows = ["| " + " | ".join(r) + " |" for r in rows[1:]]
        lines.append(header)
        lines.append(sep)
        lines.extend(body_rows)
        lines.append("")

    saved_images: list[Path] = []
    idx = 0
    with zipfile.ZipFile(path) as z:
        media_entries = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        for name in media_entries:
            ext = Path(name).suffix.lower() or ".png"
            if ext not in IMAGE_EXTS:
                continue
            data = z.read(name)
            images_dir.mkdir(parents=True, exist_ok=True)
            image_path = images_dir / f"{idx}{ext}"
            image_path.write_bytes(data)
            saved_images.append(image_path)
            idx += 1

    title = path.stem.replace("-", " ").replace("_", " ").title()
    body = f"# {title}\n\n" + "\n".join(lines).strip() + "\n"
    return body, saved_images


def parse_image(
    path: Path, slug: str, raw_dir: Path
) -> tuple[str, list[Path]]:
    images_dir = raw_dir / "images" / slug
    images_dir.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext not in IMAGE_EXTS:
        raise SystemExit(f"ERROR: unsupported image extension: {ext}")
    dest = images_dir / f"0{ext}"
    shutil.copyfile(path, dest)

    title = path.stem.replace("-", " ").replace("_", " ").title()
    md = (
        f"# {title}\n\n"
        f"![{title}](images/{slug}/{dest.name})\n\n"
        f"See `raw/images/{slug}/0.md` for the description.\n"
    )
    return md, [dest]


PARSERS = {
    ".md": ("copy", parse_md),
    ".markdown": ("copy", parse_md),
    ".txt": ("copy", parse_txt),
    ".html": ("html", parse_html),
    ".htm": ("html", parse_html),
    ".pdf": ("pdf", parse_pdf),
    ".docx": ("docx", parse_docx),
}


def hash_source_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def read_previous_source_sha(raw_dir: Path, slug: str) -> Optional[str]:
    src_path = raw_dir / f"{slug}.source.json"
    if not src_path.exists():
        return None
    try:
        with src_path.open("r", encoding="utf-8") as f:
            return json.load(f).get("source_sha256")
    except (json.JSONDecodeError, OSError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest a local file into raw/")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--slug", default=None, help="Override the slug (default: filename stem)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    source = args.path.expanduser().resolve()
    if not source.exists():
        print(f"ERROR: file not found: {source}", file=sys.stderr)
        return 1

    ext = source.suffix.lower()
    slug = args.slug or slugify(source.stem)
    raw_dir = cfg.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    images_dir = raw_dir / "images" / slug

    source_sha = hash_source_file(source)
    prior_sha = read_previous_source_sha(raw_dir, slug)

    if prior_sha == source_sha:
        # Fast-path: the local file's bytes haven't changed since last ingest.
        # Don't re-parse (PDF/DOCX parsing is expensive), don't rewrite anything.
        md_path = raw_dir / f"{slug}.md"
        src_path = raw_dir / f"{slug}.source.json"
        print(
            json.dumps(
                {
                    "slug": slug,
                    "title": slug.replace("-", " ").title(),
                    "raw_md": str(md_path),
                    "source_json": str(src_path),
                    "image_hints": [],
                    "saved_images": [],
                    "status": "unchanged",
                    "source_sha256": source_sha,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    saved_images: list[Path] = []
    image_hints: list = []

    if ext in IMAGE_EXTS:
        markdown, saved_images = parse_image(source, slug, raw_dir)
        kind = "image"
    elif ext in PARSERS:
        kind, parser_fn = PARSERS[ext]
        if kind in {"pdf", "docx"}:
            markdown, saved_images = parser_fn(source, images_dir)
        elif kind == "html":
            markdown, image_hints = parser_fn(source)
        else:
            markdown, _ = parser_fn(source)
    else:
        supported = sorted(set(list(PARSERS.keys()) + sorted(IMAGE_EXTS)))
        print(
            f"ERROR: unsupported extension {ext}. Supported: {', '.join(supported)}",
            file=sys.stderr,
        )
        return 1

    metadata = {
        "type": "local",
        "kind": kind,
        "path": str(source),
        "filename": source.name,
        "source_sha256": source_sha,
        "image_hints": image_hints,
    }

    result = write_raw_if_changed(raw_dir, slug, markdown.strip() + "\n", metadata)

    print(
        json.dumps(
            {
                "slug": slug,
                "title": slug.replace("-", " ").title(),
                "raw_md": result["raw_md"],
                "source_json": result["source_json"],
                "image_hints": image_hints,
                "saved_images": [str(p) for p in saved_images],
                "status": result["status"],
                "source_sha256": source_sha,
                "content_sha256": result["content_sha256"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

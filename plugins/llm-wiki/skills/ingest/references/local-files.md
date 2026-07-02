# Local File Parsing Reference

Load when debugging local file ingestion or adding support for a new format.

## Supported formats

| Extension | Parser | Notes |
|-----------|--------|-------|
| `.md`, `.markdown` | copy | Verbatim copy into `raw/<slug>.md` |
| `.txt` | wrap | Wrap in a code fence, save as `raw/<slug>.md` |
| `.html`, `.htm` | BeautifulSoup + markdownify | Full HTML → Markdown; embedded `<img>` extracted |
| `.pdf` | pypdf | Text extracted page by page; embedded images via `page.images` |
| `.docx` | python-docx | Paragraphs + tables + headings; images via zipped `word/media/*` |
| `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif` | image | The file **is** the source; describe via nano-banana-pro |

## Slug derivation

`slugify(filename_stem)` — Unicode-normalized, lowercase, non-alphanumerics
replaced with `-`, collapsed dashes.

## Markdown / text

Verbatim copy is the safest. Preserve the exact bytes so re-ingest is a no-op
when the file is unchanged.

```python
shutil.copyfile(src, raw_dir / f"{slug}.md")
```

For `.txt`, we still save with the `.md` extension for consistency but wrap
the content in a code fence:

````markdown
# {filename_stem}

```
{original text}
```
````

## HTML

Read the file bytes, decode with `<meta charset>` detection if present, then:

```python
from bs4 import BeautifulSoup
from markdownify import markdownify

soup = BeautifulSoup(html_bytes, "html.parser")
# Strip <script>, <style>, <nav>, <footer>
for tag in soup(["script", "style", "nav", "footer"]):
    tag.decompose()
# Extract <img src="..."> for extract_images.py
image_refs = [img.get("src") for img in soup.find_all("img")]
markdown = markdownify(str(soup), heading_style="ATX")
```

## PDF

```python
from pypdf import PdfReader

reader = PdfReader(pdf_path)
pages = []
for i, page in enumerate(reader.pages):
    pages.append(page.extract_text() or "")
    for img in page.images:  # pypdf >= 4.0
        # img.data, img.name — write out to raw/images/<slug>/<n>.<ext>
        ...
markdown = "\n\n---\n\n".join(f"## Page {i+1}\n\n{p}" for i, p in enumerate(pages))
```

Edge cases:

- **Encrypted PDF**: `reader.is_encrypted` — try `reader.decrypt("")`; if that
  fails, the ingest exits with a clear message asking for the password.
- **OCR-only PDF (scanned images, no text layer)**: text extraction returns
  empty strings. Fall back to treating each page as an image — write each
  page to `raw/images/<slug>/page-<n>.png` and let `describe_image.py`
  process them.
- **Non-Latin scripts**: pypdf handles Unicode; no special handling needed.

## DOCX

DOCX files are ZIP archives. Prefer `python-docx` for text and structure:

```python
from docx import Document

doc = Document(docx_path)
lines = []
for para in doc.paragraphs:
    if para.style.name.startswith("Heading"):
        level = int(para.style.name.replace("Heading ", "") or "1")
        lines.append("#" * level + " " + para.text)
    else:
        lines.append(para.text)
for table in doc.tables:
    # Render as GitHub-flavored markdown table
    ...
markdown = "\n\n".join(lines)
```

For images, `python-docx` doesn't expose them cleanly — fall back to raw ZIP:

```python
import zipfile
with zipfile.ZipFile(docx_path) as z:
    for name in z.namelist():
        if name.startswith("word/media/"):
            # z.read(name) → image bytes, save to raw/images/<slug>/<n>.<ext>
            ...
```

Order matters: enumerate `word/media/*` alphabetically to keep image
numbering stable across re-ingests.

## Images

Standalone images (`.png`/`.jpg`/`.webp`/`.gif`) are the source itself. Copy
to `raw/images/<slug>/0.<ext>`, add to the manifest, and describe. The raw
Markdown file `raw/<slug>.md` for a standalone image looks like:

```markdown
# {filename_stem}

![{filename_stem}](images/{slug}/0.{ext})

See `raw/images/{slug}/0.md` for the description.
```

The description file `raw/images/<slug>/0.md` is populated by
`describe_image.py`.

## Adding a new format

To add support for e.g. `.rtf` or `.epub`:

1. Add the extension → parser mapping in `fetch_local.py`'s `PARSERS` dict.
2. Implement `parse_<ext>(path) -> (markdown_text, list_of_image_paths)`.
3. Add the dependency to `requirements.txt` and to the `require([...])` list
   at the top of `fetch_local.py`.
4. Update this reference doc with the new row in the table.
5. Update the top-level `README.md` list of supported types.

## Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Empty raw file for a PDF | OCR-only, no text layer | Force image-per-page fallback (`--pdf-as-images`) |
| Garbled unicode in HTML | Wrong charset detection | Manually open in an editor, save as UTF-8 first |
| DOCX images out of order | Filesystem/zip enumeration order varies | We sort alphabetically — if the doc uses `image1.png`, `image2.png`, etc., this is stable |
| Ingest doesn't detect a supported format | Unknown extension | Check the case of the extension (`.PDF` vs `.pdf`) — we lowercase before matching |

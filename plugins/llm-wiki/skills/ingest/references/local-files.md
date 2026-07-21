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
| `.xlsx` | openpyxl | Each sheet → `##` heading + GFM table; formula cells read cached computed values; images via zipped `xl/media/*` |
| `.csv` | stdlib `csv` | Delimiter auto-detected (`csv.Sniffer`); single GFM table |
| `.pptx` | python-pptx | Each slide → `##` heading + bullet text + tables; embedded picture shapes extracted per slide |
| `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif` | image | The file **is** the source; describe via nano-banana-pro |

Legacy binary formats — `.xls`, `.ppt`, `.doc` — have **no native parser**
(openpyxl/python-pptx/python-docx only read the modern OOXML zip formats).
They fall through to the "unsupported extensions" path below.

## Copy-into-raw and versioning

On ingest, the original file is **copied into the wiki** so the wiki owns its
sources and the diff check never depends on an external path:

- **Non-image files** are copied to `raw/<slug><ext>` (flat, same stem as
  `raw/<slug>.md`). `source_sha256` is computed from **this copy**, and parsing
  reads the copy too. So if the external original later moves or is deleted,
  re-ingest still works — it hashes the in-raw copy.
- **Images** keep their dedicated home `raw/images/<slug>/0<ext>` (via
  `parse_image`) and are **not** also copied to the raw root.
- `.source.json` stores a **portable relative** `path` (`raw/<slug><ext>` or
  `raw/images/<slug>/0<ext>`), plus `original_filename` and `original_path`
  (the absolute external location it came from — provenance only, never read
  back for diffing).

### What gets committed vs kept local

`git add raw` honors `.gitignore`. The template ignores copied-in **media**
originals at the raw root while committing everything else:

| Category | Extensions | Committed? |
|----------|-----------|-----------|
| Documents | `.pdf` `.docx` `.doc` `.txt` `.md` `.rtf` `.html` `.htm` | ✅ committed |
| Spreadsheets | `.xlsx` `.xls` `.csv` `.tsv` `.ods` | ✅ committed |
| Presentations | `.pptx` `.ppt` `.odp` | ✅ committed |
| Images / video / audio | `.png` `.jpg` … `.mp4` `.mov` `.mp3` … | ❌ git-ignored (local only) |

This keeps the wiki self-contained for the text-bearing sources it can diff and
re-parse, without bloating git with large media binaries.

### Unsupported extensions

`fetch_local.py` still **copies and versions** a file whose extension has no
`PARSERS` entry (e.g. legacy binary `.xls`, `.ppt`, `.doc`). It writes a
self-describing placeholder `raw/<slug>.md` pointing at the copied original,
and Claude synthesizes the wiki content directly from the source file. The
copy/versioning path does not depend on a parser existing.

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

## XLSX

`.xlsx` is also a ZIP archive. Use `openpyxl` with `data_only=True` so
formula cells resolve to their last-computed value instead of the formula
string:

```python
import openpyxl

wb = openpyxl.load_workbook(xlsx_path, data_only=True)
for ws in wb.worksheets:
    rows = list(ws.iter_rows(values_only=True))
    # sanitize + render each sheet as "## <sheet title>" + a GFM table
```

Images are extracted the same way as DOCX — `openpyxl` doesn't expose them
cleanly, so fall back to the raw ZIP's `xl/media/*` entries.

Edge cases:

- **Formula cells with no cached value**: `data_only=True` returns `None` for
  a formula cell that has never been opened/saved in a real spreadsheet app
  (e.g. a file built directly with `openpyxl` and never round-tripped through
  Excel). This renders as an empty table cell — there is no formula-evaluation
  engine here, so this is expected, not a bug. Files actually produced by
  Excel/Sheets and re-saved have cached values and render correctly.
- **Blank section-divider sheets**: openpyxl reports at least one `(None,)`
  row even for a genuinely empty sheet. Detect "no real content" (every cell
  blank) and render the `##` heading with no table, rather than an empty
  `| |` row.
- **Non-Latin sheet names**: rendered verbatim as the `##` heading — no
  special handling needed (confirmed against real Japanese-named sheets).

## CSV

Pure stdlib — `csv.Sniffer` detects the delimiter (comma/semicolon/tab/pipe),
falling back to comma if sniffing fails (e.g. a single-column file with no
delimiter to detect):

```python
import csv

with open(csv_path, newline="") as f:
    sample = f.read(4096)
    f.seek(0)
    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    rows = list(csv.reader(f, dialect))
# render as a single GFM table, first row as header
```

No images are possible in a CSV. Rows are padded to the widest row's length
before rendering so a file with inconsistent trailing commas doesn't
misalign columns.

## PPTX

Use `python-pptx`; iterate slides (do **not** slice `prs.slides` — the
`Slides` collection doesn't support Python slicing) and render per-slide
content:

```python
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

prs = Presentation(pptx_path)
for i, slide in enumerate(prs.slides, start=1):
    title = slide.shapes.title  # None if the slide has no title placeholder
    for shape in slide.shapes:
        if shape.has_table:
            ...  # render as a GFM table
        elif shape.has_text_frame:
            ...  # each non-empty line as a bullet
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            ...  # shape.image.blob → raw/images/<slug>/<n>.<ext>
```

Edge cases:

- **Picture shapes with no embedded image**: a shape can report
  `shape_type == PICTURE` yet be a *linked* (not embedded) image or an empty
  picture placeholder — `shape.image` raises `ValueError("no embedded
  image")` in that case. Catch it and skip; there is nothing to extract.
  Confirmed against a real deck: 9 picture-typed shapes, only 8 had an
  embedded image to save.
- **Extracting per-slide, not via raw ZIP**: unlike DOCX/XLSX, `python-pptx`
  exposes images cleanly and in slide order via `shape.image`, so there's no
  need for (and no benefit to) a `ppt/media/*` ZIP fallback. A raw ZIP scan
  would actually be **wrong** here — it also picks up slide-master/layout
  media never shown on any slide (confirmed: 15 ZIP media entries vs. 9 real
  picture shapes in the same file).
- **Slide with no title placeholder**: `slide.shapes.title` is `None`; the
  slide heading is just `## Slide N` with no title suffix, and no shape is
  excluded from the body loop.

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
| XLSX table cell is blank where a formula should show a value | The workbook was never opened/saved in a real spreadsheet app, so there's no cached computed value | Open once in Excel/Sheets and save, or accept the blank — there is no formula engine here |
| `.xls`/`.ppt`/`.doc` fall to the placeholder synthesis path | Legacy binary formats have no native parser (openpyxl/python-pptx/python-docx only read OOXML) | Convert to `.xlsx`/`.pptx`/`.docx` first if native parsing matters, or accept model synthesis |

# Supported formats

Extraction turns a document into a list of pages (`{"page": 1-based int, "text":
str}`) that the chunker consumes. It is the only place a document parser is
imported; everything downstream works on plain text, so adding a format never
touches the chunker or the index.

## Core (installed by default, MIT-compatible)

| Format | Extractor | Notes |
|---|---|---|
| PDF (text layer) | `pypdf` (BSD-3) | One page per PDF page; pages with no text are preserved so citations keep their numbers |
| Markdown (`.md`, `.markdown`) | stdlib | Heading lines are preserved so the chunker can build the breadcrumb |
| HTML (`.html`, `.htm`) | stdlib `html.parser` | Visible text only; scripts, styles and navigation are dropped |
| Plain text (`.txt`, `.text`) | stdlib | UTF-8 with replacement for invalid bytes |
| EPUB | stdlib `zipfile` + HTML | One page per spine document, in reading order |
| Email (`.eml`) | stdlib `email` | Subject/from/date plus the text body (HTML body is stripped if there is no text part) |

Nothing in the default install pulls in a copyleft or proprietary parser.

## Optional extras (never required)

| Extra | Adds | License |
|---|---|---|
| `peekseed[office]` | DOCX, PPTX, XLSX via python-docx / python-pptx / openpyxl | MIT |
| `peekseed[tables]` | PDF table structure via pdfplumber | MIT |
| `peekseed[ocr]` | Scanned PDF pages and images via Tesseract/pytesseract | Apache-2.0 |
| `peekseed[layout]` | Layout, tables and figures via Docling (0.2.0) | MIT |
| `peekseed[pymupdf]` | PyMuPDF as an alternative PDF parser | AGPL-3.0 or commercial |

PyMuPDF is **opt-in only**: it is never a default dependency and never bundled.
The default install stays MIT-compatible, and a CI license check fails if an
AGPL/GPL dependency sneaks into the default closure.

## Scanned PDFs and OCR

A PDF page whose text layer has almost no characters is marked `needs_ocr`. With
`peekseed[ocr]` installed, those pages are OCR'd on demand. Without it, the file
fails with an actionable error naming the file and page:

```
OcrRequired: 'scan.pdf' page 3 has no text layer; install peekseed[ocr] to read scanned PDFs
```

A scan never silently produces an empty dataset: either the text is extracted or
the source is marked `error` with that message.

## Extractor identity is provenance

Every extraction result records its extractor name and version
(`{"name": "pypdf", "version": "6.21.1"}`). The extraction cache is keyed by the
document's sha256 **and** that identity, so upgrading `pypdf` invalidates the
cache and the chunks it fed, which then requires re-embedding. This is deliberate:
a parser upgrade can change the extracted text, and an index must never claim to
be built from text it was not built from.

## Third-party extractors

Extractors are discovered through the `peekseed.extractors` entry-point group, so
a plugin can add a format without forking peekseed:

```toml
[project.entry-points."peekseed.extractors"]
myformat = "my_package.extractors:MyExtractor"
```

A plugin implement `name`, `version`, `extensions` and `extract(path)` returning
the page list. Core imports stay lazy, so an unused extra costs nothing at import
time.

## What 0.1.0 does not extract

Table grids, figure images, page screenshots and bounding boxes are the 0.2.0
evidence layer. For 0.1.0 a PDF is its text; questions about figures and tables
are answered less well than with a layout-aware extractor, and the docs say so.

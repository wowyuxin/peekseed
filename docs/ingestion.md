# Ingestion and indexes

## Flow

```
upload PDFs -> datasets/<id>/original/ -> extract text -> chunks.jsonl
            -> embed in batches through the enabled embed link
            -> datasets/<id>/index/<space>/{vectors.npy, meta.json, info.json}
            -> IndexStore reload -> searchable
```

The UI's **Datasets** dialog posts the files, then offers **Build index** on the
new row. Indexing runs in a background thread so the browser can poll progress;
the index store is reloaded when the job completes.

The CLI does the same headlessly:

```bash
python -m peekseed index --name "Product docs" --file manual.pdf
```

## PDF extraction

The default extractor is `pypdf` (pure Python, no system packages). It returns
one record per page with a 1-based page number. Text is normalised (NUL and
control characters removed, excessive blank lines collapsed) but page boundaries
are preserved so chunks can cite a page range.

`pymupdf` is an optional extra. When installed it may be preferred (faster, more
robust on some layouts). It is AGPL-3.0 or commercial, so it is not a default
dependency; the default install stays MIT-compatible.

The uploaded PDF is kept unchanged under `original/` and served at
`/pdf/<dataset>/<file>`; result citations link to `...#page=N` and open the
browser's native PDF viewer. No PDF viewer is bundled.

## Chunking

Chunks are built to be readable and citable, not merely token-sized:

- heading lines (markdown `#`, `1.2.3 Title`, short lines without terminal
  punctuation, ALL CAPS) start a section and update the breadcrumb;
- body text is split on paragraph boundaries near `target_chars` (default 1,200)
  and never exceeds `max_chars` (default 2,000) unless a single paragraph is
  longer;
- every chunk records `page_start`/`page_end`, `section`, `chapter`, `numbering`,
  `parent`, `path` and `source`;
- `doc` is the embedded/scored text (`breadcrumb\n\nbody`); `text` is the reader
  view with `[p.N]` markers at page transitions.

Chunk ids are deterministic (`<dataset>-000001`, ...), so rebuilding from the
same PDF yields the same ids and history citations stay meaningful.

## Index files

```
index/<space>/vectors.npy   float32 [n, dim]
index/<space>/meta.json     row-aligned chunk metadata
index/<space>/info.json     space identity and provenance
```

`space` is `"<embed link id>__<model id>"`. Changing the embed link's model (or
using another link) produces a different space and therefore a different index:
peekseed never compares vectors produced by different models. The UI lists which
spaces a dataset has and marks it as needing an index for the currently enabled
one.

## Limits and failure modes

- 50 MB per uploaded PDF, PDF content type checked.
- Indexing is synchronous inside the worker: if the embedder is slow, the job
  progress moves in batch-sized steps; it is not a queue and does not retry.
- A failed embed call fails the whole job with the provider's message (never the
  API key) and leaves no partial index (the index is written to a temp directory
  and swapped in only on success).
- Datasets and indexes can be deleted from the UI; this removes the dataset
  directory, including its original PDFs.

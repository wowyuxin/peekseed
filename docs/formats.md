# Formats and extraction

## Upload → storage; extraction → peelfeed

peekseed **stores** files; it does not parse them. Uploading a file (UI or
`POST /api/datasets/{id}/sources`) does exactly this:

1. check the file is a PDF (`%PDF-` magic + `.pdf` extension) — the upload
   format is fixed for now;
2. store the bytes **once** at `<shared>/sources/<sha256>/<name>.pdf` (0600;
   the same bytes are never stored twice, and two datasets share the one file);
3. record `{name, path, sha256, bytes, status:"uploaded"}` in `dataset.json`,
   with `path = sources/<sha256>/<name>` and
   `extraction.state = "not_extracted"`.

No page count, no text extraction, no chunking and no embedding happen at upload
time. The file list, open-stored-PDF, rename and delete operations work with
**no extraction engine configured**.

Extraction is provided by the separate **peelfeed** engine (its own repo and
venv, `docling` + `torch`), one peel per source file. peekseed invokes it
**through its command line** (subprocess; no daemon/HTTP). When a peelfeed CLI
is configured, the UI/API can trigger Extract on a file:

```
peekseed upload → peelfeed ingest <file> --root $PEEKSEED_SHARED_DIR/peels
    → <shared>/peels/<slug>-<sha8>/
    source/<name>.pdf          hardlink to the stored sources/ copy
    extract/corpus/{manifest.json,chunks.jsonl,assets.jsonl,validation.json,review_queue.json,...}
    .peel
```

peekseed then reads that corpus (never the PDF) to build the index:

```
GET /api/datasets/{id}/sources        per-source extraction state
POST /api/datasets/{id}/sources/{sha}/extract
POST /api/datasets/{id}/index         read every extracted corpus → embed → index
```

**Which formats are supported is decided by the peelfeed profile**, not by
peekseed. peekseed itself never imports `pypdf`, `docling` or `torch`; its
dependency set is the web stack only (FastAPI/uvicorn/httpx/numpy/pydantic/
python-multipart). Today peelfeed takes PDFs; other formats can be added on the
peelfeed side without changing peekseed.

## Without peelfeed

No peelfeed CLI configured (or the command fails) is a supported state:

| Feature | No peelfeed | With peelfeed |
|---|---|---|
| Upload (store + sha256 + list) | ✅ | ✅ |
| File management (list, open stored PDF, delete) | ✅ | ✅ |
| Trigger extraction (Extract button / API) | ❌ disabled, API 503 with a clear reason | ✅ |
| Extraction status / validation / review | ❌ shows "not extracted" | ✅ |
| Search / ask / research | only for datasets with an extracted + indexed corpus; otherwise a clear empty/409 state | ✅ |
| Build index | ❌ 409 `no_extracted_sources` | ✅ after Extract |

Uploads keep working when the peelfeed CLI is missing, and deleting a file never
depends on it. `GET /api/peelfeed/status` and the file-list banner report whether
the CLI is configured and answers `capabilities`.

## Corpus gate before indexing

`build_index` reads `<shared>/peels/<peel_id>/extract/corpus/…` and refuses a
source (with a per-source reason) unless:

* the peel directory resolves under `$PEEKSEED_SHARED_DIR/peels` after
  `realpath`;
* `manifest.source_sha256` equals the stored source's sha256;
* `manifest.conversion_id` equals the `conversion_id` recorded in
  `dataset.json` for that source;
* `validation.errors` is empty (0 errors).

Sources that are `not_extracted`/`queued`/`running`/`failed`/`stale` are skipped
and listed in the index response (`skipped_sources`), never silently ignored. If
no source is extracted, the index build is refused with `no_extracted_sources`
(HTTP 409) and a message telling the user to configure peelfeed and Extract.

## Provenance

Every index records what produced it:

* `extractor = {"name": "peelfeed", "version": "<engine fingerprint>"}`;
* `chunker = {"name": "peel", "version": 1, "target_chars": 8000,
  "max_chars": 12000, "image_cap": 3, ...}` (the peelfeed chunk contract);
* `peel = {peel_id, conversion_id, profile, validation}` (one peel per source;
  a multi-source index lists them under `peel.peels`);
* `documents_digest` (source set) and `corpus_digest` (peel identity: sha256 +
  peel_id + conversion_id) — a rebuilt peel or a changed source makes the index
  stale, and `peekseed status` says so.

The legacy extraction cache (`.peekseed/extracted/<sha256>.json`) is no longer
read or written: the peel directory is the cache. There are no format extras and
no plugin entry points for extractors in peekseed any more.

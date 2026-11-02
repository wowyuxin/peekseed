# M-1 report — remove peekseed's old extraction support

Date: 2026-11-01. Scope: `source/` only. Task: `task-1` (completed). Reference:
`peelfeed/HANDOFF.md` §4 M-1, `peelfeed/docs/peelfeed-integration-design.md`
§1.6/§4.2–§4.5.

## What changed

### Deleted
- `peekseed/extract.py` (all format backends, registry, `cache_key`, extractor identity)
- `peekseed/ingest/pdf.py` (pypdf extraction, `page_count`, `extract_pdf`)
- `peekseed/ingest/chunk.py` (char chunker)
- `peekseed/importer.py` and the `peekseed import` CLI command + `docs/import.md`
- `tests/test_extract.py`, `tests/test_chunk.py`, `tests/test_import.py`
- Extraction/cache tests inside `test_scan.py`, `test_ingest.py`, `test_index.py`,
  `test_api.py`, `test_jobs.py`, `test_cli.py`, `test_acceptance.py`, `test_migrate.py`

### Added
- `peekseed/ingest/errors.py` — `IngestError` without the deleted PDF module.
- `ingest/index.py::NoExtractedSources` (`.code == "no_extracted_sources"`, `.skipped`),
  `peel_root()`, peel-corpus reader (`_read_peel`, `_collect_corpus_chunks`,
  `_corpus_chunk_record`), `_refresh_extraction`, `_peel_extractor`,
  `_current_info_if_up_to_date`.
- `tests/fixtures.py::make_peel()` — fake peelfeed peel corpus
  (`source/`, `extract/corpus/{manifest,chunks,blocks,assets,validation,review_queue}`)
  and marks the dataset source extracted; used by every index test.

### Rewritten
- `peekseed/ingest/index.py`
  - `add_source` is **storage only**: PDF magic/extension check, atomic copy,
    sha256/bytes, `status="uploaded"`, `extraction.state="not_extracted"`, no page
    count/extraction/chunking; same-file re-upload is idempotent.
  - `build_index` reads each `extraction.state == "extracted"` source's corpus from
    `<peel_root>/<peel_id>/extract/corpus/` (default peel root `<data_root>/peels`,
    `PEEKSEED_PEELFEED_ROOT` overrides). Gates: realpath allowlist, manifest
    `source_sha256 == source.sha256`, recorded `conversion_id == manifest.conversion_id`,
    `validation.errors == 0` (list length or int), source file exists/size matches.
    Corpus chunks are mapped to peekseed's chunk contract (`doc`/`text` semantics
    unchanged; `block_index` from `blocks.jsonl` ordinal, content-derived id;
    `chunker_version = PEEL_CHUNKER_VERSION = 1`; optional `kind`, `image_paths`,
    `source_pages`, `char_count`, `parts`/`part`, `prev`/`next`, `canonical_ids`,
    `engine`, `conversion_id`, `peel_id`). No `extract_pdf` fallback exists.
  - Zero extracted sources → `NoExtractedSources` (HTTP maps to 409); non-extracted
    sources are listed in `info.skipped_sources`.
  - `info.json` gains `extractor={name:"peelfeed",version:…}`, `chunker={name:"peel",
    version:1,target_chars:8000,max_chars:12000,image_cap:3}`, `peel={peel_id,
    conversion_id,profile,validation}` (or `{peels:[...]}` for multi-source),
    `corpus_digest`, `skipped_sources`. Existing embed/link/space/generation/probe/job
    machinery is intact.
  - A second build over unchanged corpora returns the live `info.json` with
    `up_to_date=True` and **zero** embed calls; `force=True` (used by `reindex`)
    rebuilds. Resume of an interrupted generation is tried first.
  - `index_state` now also compares `corpus_digest` (a rebuilt peel is stale).
- `peekseed/scan.py` — discovery/reconciliation only: walk/hash/diff manifest,
  write `dataset.json` sources (`uploaded` + extraction default, preserving an
  existing extraction block when content is unchanged), report index state
  read-only. No extraction cache, no chunking, no index build, no embed calls.
  `scan(...)` keeps its public shape; `link` is accepted but ignored.
- `peekseed/contracts.py` — `DATASET_SCHEMA = INFO_SCHEMA = 3`;
  `CHUNKER_NAME="peel"`, `PEEL_CHUNKER_VERSION=1`; `FILE_STATUSES` +
  `uploaded/not_extracted/queued/running/extracted/failed/stale`;
  `EXTRACTION_STATES`; `extraction_default()/extraction_state()`;
  `corpus_digest()`; per-source `extraction` validation; optional `peel`,
  `corpus_digest`, `skipped_sources` in `info.json`; optional peelfeed chunk
  fields in `validate_chunk`; upgrades to schema 3 (legacy sources marked
  `not_extracted`).
- `peekseed/migrate.py` — schema migrations only (dataset/info to schema 3); no
  extraction/cache migrations exist any more.
- `pyproject.toml` — dropped `pypdf` and extras `office/tables/ocr/layout/pymupdf`;
  dependency set is web-stack only + `test` extra; removed the deleted
  `extract.py` ruff ignore.
- Docs — `docs/formats.md` rewritten (upload→storage; extraction by peelfeed
  profile; no-peelfeed behavior; corpus gate; provenance). `docs/ingestion.md`
  rewritten. `docs/import.md` deleted. `README.md`, `docs/install.md`,
  `docs/architecture.md`, `docs/contracts.md`, `docs/api.md`, `docs/workspace.md`
  updated for schema 3 / storage-only uploads / peelfeed corpus.
- Tests — adapted to storage-only semantics and fake peels; new gate tests:
  fake-peel build, zero-embed second build, skipped sources, sha/conversion/
  validation/peel-root refusals, scan preserves extraction on rename, scan does
  not parse files, index 409 without extracted sources.

## Verification (exact commands + numbers)

All commands from `source/`. The runtime venv does not contain pytest/ruff, so
they were installed into a throwaway `/tmp` target (the project venv was not
modified):

```bash
PYTHONPATH=/tmp/peekseed-testdeps ../runtime/.venv/bin/python -m ruff check .
# -> All checks passed!

PYTHONPATH=/tmp/peekseed-testdeps ../runtime/.venv/bin/python -m pytest tests -q
# -> 260 passed, 1 warning (Starlette deprecation)

PEEKSEED_DATA=/tmp/... ../runtime/.venv/bin/python -m peekseed status
# -> workspace/documents/derived/last scan/collections only; no extraction reporting
../runtime/.venv/bin/python -m peekseed --help
# -> init scan status doctor workspace reindex migrate serve index version (no import)

# empty data root, service on 127.0.0.1:8039:
curl /health                                  -> 200 {"ok":true,"datasets":0}
curl -F name=Docs -F files=@manual.pdf /api/datasets
                                              -> 201, sources=["manual.pdf"], chunks=0
curl /api/datasets/docs/sources               -> status=uploaded,
                                                 extraction.state=not_extracted,
                                                 pages=0, present=true
curl -X POST /api/datasets/docs/index         -> 409
                                                 "no_extracted_sources: … Sources:
                                                  manual.pdf (not_extracted)"
curl /pdf/docs/manual.pdf                     -> 200 application/pdf (inline)
curl -X DELETE /api/datasets/docs             -> 200

grep -rn "pypdf|extract_pdf|ingest.chunk|importer|cache_key|chunk_pages|page_count" \
     peekseed pyproject.toml
# -> no stale references (only honest "never imports pypdf/docling/torch" docs)
```

## Decisions / risks / ambiguities

1. **Duplicate uploads**: re-uploading the same file is idempotent (one stored
   copy/entry); the same bytes under a different filename remain two stored
   sources (pre-M-1 semantics). peelfeed reuses one peel by sha256.
2. **Schema bump**: `dataset.json`/`info.json` are schema 3. Legacy datasets are
   upgraded on read; `peekseed migrate` writes them. Legacy sources are marked
   `not_extracted` because their old in-process extraction cannot be reused
   honestly — they need a peelfeed peel.
3. **`chunker_version` is the peel chunk-contract version (1)**, not the old
   heading chunker's. Every chunk id changes, which is the deliberate reindex the
   design asks for.
4. **`block_index`** is the ordinal of the chunk's first block in
   `blocks.jsonl`; when the corpus has no `blocks.jsonl`, the chunk's position is
   used (still stable for that corpus).
5. **`info.peel`** is a single dict for one extracted source and
   `{peels:[…], count:N}` for multi-source datasets; per-source identity is also
   recorded in `info.sources[].extraction`.
6. **Scan status order**: the dataset is written once before the read-only index
   check so the index verdict is judged against the new source set; the final
   status is written after. A successful build is reported as `indexed`.
7. **`extracted/` legacy path** is kept on `Paths`/`Workspace` (API compatibility)
   but nothing writes it; it is no longer created by `init`.
8. **M1 hook**: the real peelfeed corpus also carries `build_summary.json`
   (engine version/fingerprint, counts). M-1 reads the corpus files it needs;
   M1 will read `build_summary.json` to fill `info.extractor.version` with the
   engine fingerprint.
9. `peekseed status`/`doctor` do not probe peelfeed yet; M1 adds the peelfeed
   section and `GET /api/peelfeed/status`.

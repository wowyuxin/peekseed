# Ingestion and indexes

peekseed turns uploaded files into a searchable vector index without ever
modifying your files. Since M-1, **extraction is not a peekseed feature**: it is
a separate peelfeed job per source file, and peekseed consumes the corpus that
job writes. The pipeline is:

```
upload ──▶ <shared>/sources/<sha256>/<name>.pdf + dataset.json (status=uploaded, no parse)
           │
           ▼  user clicks Extract (or POST .../sources/{sha}/extract)
peelfeed ──▶ peelfeed ingest (CLI subprocess) → <shared>/peels/<peel_id>/extract/corpus/{manifest,chunks,validation,...}
           │  (peelfeed hardlinks the stored source into the peel's source/;
           │   copies only when the hardlink fails)
           ▼  Build index (or POST /api/datasets/{id}/index)
build_index ──▶ read corpus (gate: realpath/sha/conversion/validation.errors==0)
             ──▶ map corpus chunks → peekseed chunk contract (stable ids)
             ──▶ embed (batched, checkpointed)
             ──▶ index/<space>/generations/<uuid>/ + CURRENT (written last)
             ──▶ search
```

Upload and extraction are deliberately decoupled: uploading, listing, opening and
deleting files all work with no peelfeed configured. Search/ask only work for a
dataset whose sources have been extracted **and** indexed.

## Upload (storage only)

`POST /api/datasets` (create a collection + upload) or
`POST /api/datasets/{id}/sources` (upload into an existing collection) validate
the PDF magic/extension, stream the bytes to disk with the per-file/file-count/
total limits enforced as they arrive, sanitize the filename before any path is
built (zip-slip safe), and store the bytes **once** at
`<shared>/sources/<sha256>/<name>` (0600).

`dataset.json` gets one source entry:

```json
{
  "name": "manual.pdf", "path": "sources/<sha256>/manual.pdf",
  "sha256": "…", "bytes": 12345, "pages": 0,
  "status": "uploaded",
  "extraction": {"state": "not_extracted", "peel_id": "", "conversion_id": "",
                 "profile": "", "engine": "", "validation": {"errors": 0,
                 "review_items": 0}, "started_at": null, "finished_at": null,
                 "error": null}
}
```

Same content uploaded twice is idempotent (one stored copy, one source entry);
two datasets that upload the same bytes share the one stored file. There is no
page count, no extraction and no chunking at upload time.

## Per-source extraction state

`GET /api/datasets/{id}/sources` returns each source with its extraction block.
The state chip is one of `not_extracted | queued | running | extracted | failed |
stale`. `Extract` runs `peelfeed ingest` with the stored file's path (peelfeed
hardlinks it into the peel's `source/`, copying only when the hardlink fails),
then records `state="queued"` and a background worker polls `peelfeed status`
until the peel is ready or failed. The UI polls until `extracted` (or `failed`
with a readable reason and a Retry action). peelfeed's `.peel` log is the source
of truth for the peel state; peekseed only stores the last observed state and
identity in `dataset.json`.

Review (crop + evidence + LLM suggestion) and Apply & rebuild run the peelfeed
CLI (`review`/`decide`/`rebuild`); they never rewrite the corpus themselves.

## Chunk contract (from the peel corpus)

peekseed does not chunk. The canonical unit is the peelfeed corpus chunk
(`chunks.jsonl`, the **reading layer**: UI, answers and agent tools); it is
mapped to peekseed's chunk record per design §4.2.

The right chunk size depends on the embedding model, so each embed link names
its `chunk` sizes and an index is embedded from the peel's matching **chunk set**
(`chunks_all_ver/c<target>-<max>-i<cap>/chunks.jsonl`; links with equal sizes
share a set). A link without sizes cannot build an index. A peel that lacks the
set gets it from `peelfeed rebuild --chunk-versions` first (Docling evidence is
reused; the canonical chunks and conversion id stay the same, so other indexes
stay fresh); Extract passes the sizes of every embed link. Each embedded row
records `chunk_set` and `reading_ids` (the canonical chunks it covers), and a
search hit is returned as those canonical chunks. The record fields:

- required fields unchanged: `id`, `dataset`, `source`, `source_sha256`,
  `page_start`, `page_end`, `section`, `chapter`, `numbering`, `parent`, `path`,
  `doc`, `text`, `heading_only`, `block_index`, `chunk_index`,
  `chunker_version`;
- optional corpus fields carried through: `kind`, `image_paths`, `source_pages`,
  `char_count`, `parts`/`part`, `prev`/`next`, `canonical_ids`, `engine`,
  `conversion_id`, `peel_id` (+ `chunk_set`, `reading_ids` on index rows).

`doc` keeps the embedding/BM25 semantics: the section path on the first line,
then the chunk body. `text` is the reader view (page markers, table markdown, row
atoms) used by the UI/answer path. `chunker_version` is `1`
(`PEEL_CHUNKER_VERSION`), the peelfeed chunk-contract version.

Chunk ids stay **content-derived**:

```
id = sha256(source_sha256 | block_index | chunk_index | chunker_version)[:16]
```

`block_index` is the ordinal of the chunk's first block in the corpus
`blocks.jsonl` (stable and page-independent); `chunk_index` is the chunk's
position within its source. Adding file B cannot renumber file A's chunks.

## Corpus gate before indexing

`build_index` refuses a source (per-source reason) unless the peel directory
resolves under `$PEEKSEED_SHARED_DIR/peels` after `realpath`,
`manifest.source_sha256` equals the stored sha256,
`manifest.conversion_id` equals the recorded conversion id, and
`validation.errors` is empty. Non-extracted sources are listed in
`skipped_sources`; zero extracted sources is `no_extracted_sources` (HTTP 409).

A second Build index over the same corpora/chunker returns the live `info.json`
unchanged with **zero** embedding calls; `peekseed reindex` forces a rebuild.

## Embedding and the probe

An **embedding space** is identified by more than a model name: link id, model,
protocol, dimension, normalization and the asymmetric `doc_prefix`/`query_prefix`
all change the vectors. `info.json` records the full identity, and the index
directory name combines the link id and the model
(`<link_id>__<model>`, e.g. `embed-local__qwen3-vl-embedding-2b-8bit-mlx`).

A fixed **probe text** is embedded once when the index is built and stored as
`probe.npy` with its text sha256. At query time the live embedder re-embeds the
probe: a cosine below **0.999** means the server is no longer the same embedding
space as the index (a swapped model, different weights, changed prefixes) and the
search returns **409** with an actionable message instead of silently comparing
incompatible vectors. That check runs on every search/ask/research request; it is
one extra short embed call, tiny next to the query embedding and the rerank. An
index built before `probe.npy` existed (legacy flat layout) cannot be verified and
also returns 409 asking for a rebuild.

## Index generations

An index is never written in place. A build writes into a new directory and flips
a pointer only when it is complete:

```
index/<space>/
├── CURRENT                      # one line: the live generation uuid
└── generations/
    ├── 0f9c…/ {vectors.npy, meta.json, info.json, probe.npy}
    └── 3ab2…/
```

`CURRENT` is written last, with a single atomic rename. A crash mid-build leaves
the previous generation serving; the incomplete generation is resumed from its
per-batch checkpoint or discarded. Vectors are preallocated as a `.npy` memmap and
filled batch by batch, so a large corpus is never held in memory twice.

The loader validates `schema`, row alignment (`vectors` rows == metadata rows ==
`info.n`, width == `info.dim`), unique ids and dimension. An invalid index is
reported as `needs_reindex` — never silently skipped and never half-served.
`peekseed reindex --gc` removes generations older than `CURRENT`.

`info.json` also records the extraction provenance:

```json
"extractor": {"name": "peelfeed", "version": "<engine fingerprint>"},
"chunker": {"name": "peel", "version": 1, "target_chars": 8000,
            "max_chars": 12000, "image_cap": 3},
"peel": {"peel_id": "cxl-3p1-b2bbd2c1", "conversion_id": "…", "profile": "cxl",
         "validation": {"errors": 0, "review_items": 248}},
"documents_digest": "…", "corpus_digest": "…",
"skipped_sources": [{"name": "pending.pdf", "state": "not_extracted",
                     "reason": "not extracted yet; run Extract with peelfeed"}]
```

A changed source set, a rebuilt peel (new conversion id) or a chunker version
change makes the collection `stale`.

## Jobs, retries and cancellation

An index build is a persisted job (`$PEEKSEED_HOME/jobs/<id>.json`): `queued →
running → succeeded | failed | interrupted | cancelled`, with phase, counts and
fraction. 429/5xx/timeout responses are retried with exponential backoff and
jitter; permanent errors fail the job with a user-facing message (no API key, no
machine path). A cancel request is checked between batches: it stops the build,
leaves the previous generation serving and keeps the checkpoint for a later
resume. If the process dies, the job stays `running` on disk and is marked
`interrupted` on the next status/doctor read — with a Resume action in the UI,
not a false "failed".

One writer per collection is enforced by a lock file with pid and timestamp;
stale locks are detected and the user is told, not blocked forever.

## Re-embedding

Changing the embedder never mutates an existing space: it creates a new space.

```bash
peekseed reindex --all --link embed-local --dry-run   # scope, batches, estimated time
peekseed reindex --all --link embed-local             # build the new space
peekseed reindex --all --link embed-local --gc        # drop the old generation(s)
```

`--dry-run` prints the exact scope (collections, chunks, current vs target space,
estimated embed calls) and writes nothing. For a remote embedder, the cost and the
privacy implication (document text leaves the machine) are printed before anything
is sent.

## Limits and failure modes

- Uploads: 50 MB per file, 50 files, 200 MB total; streamed to disk with the
  limits enforced while reading, not after. Filenames are sanitized before any
  directory is created (zip-slip safe). Uploads stay available with peelfeed down.
- `/pdf/<dataset>/<file>` is served **inline** so `#page=N` opens the browser's
  native viewer; only the stored copies in the shared `sources/` pool are served,
  with traversal and symlink escapes rejected.
- One unreadable source does not hide the rest; one corrupt index does not hide
  the healthy ones.
- Extraction quality, OCR and table/figure fidelity are peelfeed concerns
  (profiles/gates), not peekseed's. See [formats](formats.md).

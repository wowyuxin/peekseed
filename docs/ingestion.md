# Ingestion and indexes

Ingestion turns a folder of documents into a searchable vector index without ever
modifying your files. The pipeline is incremental and idempotent:

```
documents/ ──scan──▶ manifest.json ──diff by sha256──▶ extraction cache
             │                                              │
             │                                              ▼
             │                                        chunk (stable ids)
             │                                              │
             │                                              ▼
             │                                   embed (batched, checkpointed)
             │                                              │
             └────────────────────────────────────▶ index/<space>/generations/<uuid>/
                                                            + CURRENT (written last)
                                                            ──▶ search
```

## Scanning

`peekseed scan` (or **Scan documents** in the UI, or `POST /api/scan`) walks
`documents/`, hashes each file and compares it with `manifest.json`:

```
walk documents/ (skip hidden, *.part, ~$*, .DS_Store; resolve symlinks, reject escapes)
  → for each file: sha256, bytes, mtime
  → diff against manifest: new | unchanged | changed | missing
  → extraction cache hit by sha256 (extractor version in the key)
  → a collection is dirty if its source hash set changed, or chunker/extractor changed
  → rebuild only dirty collections; one writer per collection
```

- **Identity is content-based.** A moved or renamed file is `unchanged` with an
  updated path; a modified file is `changed`.
- **Missing files are reported, not deleted.** Chunks are kept until
  `peekseed scan --prune`, which is explicit data loss.
- **Failure isolation.** One corrupt or encrypted file becomes `error` with a
  reason naming the file and page; the rest of the collection still indexes and
  the scan does not abort.
- **Idempotent.** A second scan of unchanged documents makes **zero** embedding
  calls.
- **Statuses** are per file and per collection: `new`, `unchanged`, `changed`,
  `missing`, `extracted`, `chunked`, `embedded`, `indexed`, `error`, `skipped`;
  a collection additionally reports `stale` (inputs changed) and `needs_reindex`
  (the index on disk is unreadable or invalid).

`manifest.json` records the schema, last scan time, extractor and chunker
versions, and per document `{path, collection, sha256, bytes, pages, status,
seen_at}`.

## Extraction

See [formats](formats.md). Extraction results are cached under
`.peekseed/extracted/<sha256>.json`, keyed by the document hash **and** the
extractor identity. A parser upgrade therefore invalidates the cache and the
chunks it fed — the index's provenance says exactly what produced it.

## Chunking and stable ids

The chunker is heading-aware: Markdown hashes, numbered headings, ALL CAPS lines
and short title-case lines start a section and maintain a breadcrumb. Body text is
packed into chunks between `target_chars` (1,200) and `max_chars` (2,000), at
paragraph boundaries where possible.

Every chunk carries provenance: `source`, `source_sha256`, `page_start`,
`page_end`, `section`, `chapter`, `numbering`, `path`, `doc` (the embedded text:
breadcrumb + body), `text` (the reader view with `[p.N]` markers) and
`heading_only`.

Chunk ids are **content-derived**:

```
id = sha256(source_sha256 | block_index | chunk_index | chunker_version)[:16]
```

Adding file B to a collection cannot renumber file A's chunks, which is what makes
incremental embedding safe. The human-facing `ordinal` is a separate display field
and may change freely; the id does not. Changing `chunker_version` deliberately
changes every id and marks the collection `stale`.

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
incompatible vectors.

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

## Jobs, retries and cancellation

A build is a persisted job (`.peekseed/jobs/<id>.json`): `queued → running →
succeeded | failed | interrupted | cancelled`, with phase, counts and fraction.
429/5xx/timeout responses are retried with exponential backoff and jitter;
permanent errors fail the job with a user-facing message (no API key, no machine
path). A cancel request is checked between batches: it stops the build, leaves the
previous generation serving and keeps the checkpoint for a later resume. If the
process dies, the job stays `running` on disk and is marked `interrupted` on the
next status/doctor read — with a Resume action in the UI, not a false "failed".

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

## Limits and failure modes (0.1.0)

- Uploads: 50 MB per file, 50 files, 200 MB total; streamed to disk with the
  limits enforced while reading, not after. Filenames are sanitized before any
  directory is created (zip-slip safe).
- `/pdf/<dataset>/<file>` is served **inline** so `#page=N` opens the browser's
  native viewer; only the dataset's own `original/` copies are served, with
  traversal and symlink escapes rejected.
- One corrupt document does not fail a scan; one corrupt index does not hide the
  healthy ones.
- No OCR by default, no table/figure fidelity, no watcher by default, no ANN/vector
  database in 0.1.0. These are documented limitations, not surprises.

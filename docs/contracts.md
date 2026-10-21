# Data contracts and module interfaces (0.1.0)

This is the working spec for the workspace + ingestion core. The authoritative
code is [`peekseed/contracts.py`](../peekseed/contracts.py); this page pins the
module boundaries so parallel work cannot drift. Tests are the gate:
`audit §6.10` items 1–6 and 9–11 (see `PEEKSEED_OSS_AUDIT.md` in the private
deployment repo).

## Workspace layout

```
<workspace>/                          one directory to back up (0700)
├── documents/                        USER-OWNED; peekseed never writes here
│   ├── cxl/                          one top-level folder = one collection
│   │   └── CXL_spec.pdf
│   └── manual.pdf                    loose files → collection "default"
├── .peekseed/                        DERIVED; rebuildable; safe to delete
│   ├── manifest.json                 scan state                     schema 1
│   ├── config/links.json             0600
│   ├── datasets/<collection>/        dataset.json schema 2, chunks.jsonl,
│   │                                 index/<space>/generations/<uuid>/ + CURRENT
│   ├── extracted/<sha256>.json       extraction cache
│   ├── history/
│   ├── jobs/<id>.json                job record schema 1
│   └── uploads/                      staging for an upload in progress
└── .gitignore                        `init` ensures an anchored /workspace/ entry
```

Resolution order: `--workspace` → `PEEKSEED_WORKSPACE` → `./workspace` →
`~/peekseed` (only when the cwd is unwritable, with a warning). `PEEKSEED_DATA`
overrides the derived root only. A legacy `./data` root is adopted with a printed
migration hint, never duplicated silently.

## Contracts (`peekseed/contracts.py`) — already implemented

| Contract | Schema | Notes |
|---|---|---|
| `manifest.json` | 1 | `created_at`, `last_scan`, `extractor`, `chunker`, `documents[{path,collection,sha256,bytes,pages,status,seen_at}]` |
| `dataset.json` | 2 | `id`, `name`, `sources[{name,path,sha256,bytes,pages,extractor,status}]`, optional collection `status` |
| chunk row | — | `id` (content-derived 16 hex), `ordinal`, `source`, `source_sha256`, `block_index`, `chunk_index`, `chunker_version`, pages, breadcrumb, `doc`, `text`, `heading_only` |
| `info.json` | 2 | `space`, `generation`, `n`, `dim`, `embedding{link_id,model,protocol,dim,normalized,doc_prefix,query_prefix,probe}`, `chunker`, `extractor`, `documents_digest`, `sources` |
| job record | 1 | `id`, `kind`, `collection`, `state`, `progress`, `cancel_requested`, timestamps |

Helpers: `chunk_id()`, `documents_digest()`, `space_key()` / `space_identity()`,
`probe_sha256()`, `stale_reasons()`, `new_manifest()`, `new_job()`,
`validate_*()`, `upgrade_dataset()`, `upgrade_info()`, `read_json()` /
`write_json_atomic()`.

Rules: readers refuse a **newer** schema (`SchemaTooNew`); older schemas are
upgraded explicitly (`peekseed migrate`); JSON writes are atomic.

## Module interfaces

### `peekseed.workspace` (W1)

```python
class Workspace:
    root, documents_dir, derived_dir, manifest_file, config_dir, links_file,
    settings_file, datasets_dir, extracted_dir, history_dir, jobs_dir, uploads_dir
    def as_dict(self) -> dict          # all absolute paths as strings

resolve(explicit=None) -> Workspace    # resolution order above
init(explicit=None) -> dict            # create tree, gitignore guard, abs paths
doctor(explicit=None) -> dict          # {"paths", "issues": [...], "git", "links", "schemas"}
status(explicit=None) -> dict          # workspace, last_scan, collections, counts
move(target) -> dict                   # move the tree, return new paths
guard_gitignore(root) -> dict          # ensure anchored /workspace/ entry
```

`config.paths()` must resolve to the workspace-derived root by default and keep
its existing `Paths` attributes (`PEEKSEED_DATA` still overrides the root;
existing tests must stay green). It gains `manifest_file`, `extracted_dir`,
`jobs_dir`, `workspace_root`, `documents_dir`.

### `peekseed.extract` (W3)

```python
EXTRACTOR_API = 1
SUPPORTED_EXTENSIONS = {...}
class OcrRequired(IngestError)   # message names file and page

extract(path, *, ocr=None) -> dict
    # {"pages": [{"page": 1-based int, "text": str}],
    #  "extractor": {"name": str, "version": str},
    #  "pages_count": int, "needs_ocr": bool}
extractor_identity_for(path) -> dict
cache_key(sha256, extractor) -> str   # cache filename: "<sha256>.json"
available_extractors() -> list[str]
ocr_available() -> bool
```

Core formats: PDF text (`pypdf`, `[pymupdf]`/`[ocr]` optional fallbacks),
Markdown, HTML, TXT, EPUB, `.eml` with stdlib only. Extractor identity is
provenance: a version change invalidates chunks. Optional extras (`office`,
`tables`, `ocr`, `layout`) are detected lazily and never imported in core.

### `peekseed.scan` (W2)

```python
scan(workspace=None, *, prune=False, link=None, progress=None) -> dict
```

Report: `{workspace, started_at, finished_at, counts{new,changed,unchanged,missing,errors},
files[...], collections{id: {status, sources, chunks, embed_calls, reasons}}, embed_calls}`.

Behaviour: walk `documents/` (skip hidden/`*.part`/`~$*`/`.DS_Store`; resolve
symlinks, reject escapes), sha256 each file, diff the manifest (moved/renamed = a
path update, not a new document), extraction cache keyed by
`(sha256, extractor identity)`, chunk new/changed files with content-derived ids,
write `dataset.json` (schema 2) + `chunks.jsonl`, then build the index for dirty
collections when a link is supplied. One corrupt file marks that source `error`
with a reason and continues. `--prune` removes missing sources (chunks too).
A second scan of unchanged documents makes **zero** embed calls.

### `peekseed.ingest.index` (W5)

```python
build_index(dataset_dir, link, *, target_chars=1200, max_chars=2000,
            progress=None, batch_size=32, job=None, cancel=None) -> dict  # info.json
load_index(dataset_dir, space) -> {"info", "vectors", "meta"}
index_state(dataset_dir, space, *, chunker=None, extractor=None) -> dict
    # {"status": "indexed|stale|needs_reindex|not_indexed", "reasons": [...], "generation": str}
probe_cosine(link, dataset_dir, space) -> float     # live embedder vs stored probe
verify_probe(link, dataset_dir, space) -> None      # raises ProbeMismatch (< 0.999)
reindex(dataset_dir, link, *, dry_run=False, gc=False) -> dict   # scope estimate
```

Layout: `index/<space>/generations/<uuid>/{vectors.npy,meta.json,info.json,probe.npy}`
and `CURRENT` (one line: uuid) written **last** with `os.replace`. A crash leaves
the previous generation serving. Loader validates schema, row alignment, unique
ids and dim; invalid → `needs_reindex`, never silent. Embedding is batched into a
preallocated `vectors.npy` (memmap), retries 429/5xx/timeouts with backoff,
checkpoints per batch, and resumes from the checkpoint. A job record is written to
`.peekseed/jobs/<id>.json` and updated (`running` → `succeeded|failed|interrupted`).
`cancel` is a callable checked between batches.

### `peekseed.security` (W8)

```python
token_enabled() -> bool                    # PEEKSEED_TOKEN set
install(app) -> None                       # middleware: token + Host/Origin
check_upload_stream(...) / limits          # per-file, file-count, total size
```

Mutating `/api/*` routes require the token when enabled and reject a hostile
`Origin`/`Host`; reads stay open. Uploads stream to disk with limits checked
**before** reading. `/pdf` is served inline and only from the dataset's
`original/` copies, with traversal and symlink escapes rejected.

## CLI (Lead)

`peekseed init [--workspace PATH]`, `scan [--prune] [--link ID]`, `status`,
`doctor`, `workspace path|move PATH`, `reindex --all --link ID [--dry-run] [--gc]`,
`migrate [--dry-run]`, `serve`, `version`, and the legacy `index`.
CLI wiring lives in `peekseed/__main__.py` (Lead-owned) and calls the module
functions above; teammates must not edit it.

## Module map

| Module | Responsibility |
|---|---|
| `peekseed/contracts.py` | Versioned schemas, content ids, space identity, probe, atomic JSON, upgrades |
| `peekseed/workspace.py`, `config.py` | Workspace resolution, `init`/`doctor`/`status`/`move`, derived-root paths |
| `peekseed/scan.py` | Manifest diff by sha256, extraction cache, chunk rebuild, dirty-collection builds |
| `peekseed/extract.py` | Format extraction and versioned extractor identity (core + optional extras) |
| `peekseed/ingest/chunk.py` | Heading-aware chunker with content-derived ids |
| `peekseed/ingest/index.py` | Generation-atomic indexes, probe, jobs, `load_index`/`index_state`/`reindex` |
| `peekseed/security.py`, `app.py` | Token/Host/Origin gate, streaming upload limits, inline `/pdf` |
| `peekseed/migrate.py` | Explicit schema migration (dry-run default) |
| `peekseed/__main__.py` | CLI wiring for all of the above |

## Acceptance (audit §6.10)

Tests 1–6 and 9–11 are verified end-to-end by `tests/test_acceptance.py`
(`GateOneTests` … `GateElevenTests`); each module also has its own unit suite
(`test_workspace`, `test_scan`, `test_extract`, `test_index`, `test_security`,
`test_contracts`, `test_migrate`, `test_chunk`, `test_engine`, `test_cli`).

1. `init --workspace /tmp/ws` creates the tree + anchored gitignore entry;
   `documents/` hashes/mtimes unchanged after a scan.
2. Drop PDF A → scan → indexed; scan again → `0 new, 0 changed`, **0** embed calls.
3. Add PDF B to an indexed collection → scan → B searchable, A's ids unchanged.
4. Cancel/kill mid-build → previous index still serves; restart resumes the
   incomplete generation; crash-left jobs are `interrupted`, not `failed`.
5. Corrupt `vectors.npy` → collection `needs_reindex` with a reason.
6. Change chunker version → collection `stale`; `reindex --dry-run` prints scope.
7. Embedder swap → **409** with an actionable message when the live probe no longer
   matches the stored one; a legacy space without `probe.npy` also 409s (rebuild).
9. Zip-slip/symlink escape and `/pdf` traversal rejected; `/pdf` is `inline`.
10. Hostile `Origin`/`Host` on mutating routes rejected; token required when set.
11. A 10 GB upload is rejected without RAM growth; file-count/total limits hold.

The query-time probe gate is wired: `/api/search`, `/api/search/stream`,
`/api/ask/stream` and `/api/agent/stream` re-embed the fixed probe text and return
**409** when the live embedder no longer reproduces `probe.npy` (cosine < 0.999),
or when a legacy space has no stored probe. The persisted job API (W7) is wired
too: `GET/DELETE /api/jobs`, `POST /api/jobs/{id}/resume`, and one writer per
collection via `.peekseed/jobs/locks/<collection>.lock` (stale-holder detection);
the API job is the record on disk, and startup marks crash-left `running` records
`interrupted`. The workspace/collections surface (`GET /api/workspace`,
`/api/collections`, `/api/datasets/{id}/sources`) and the UI Workspace panel are
wired; `links.py` runs the same `security.validate_url` SSRF guard as the API and
all streams emit `{"type": "error", "status", "detail"}`. Scanning (`POST
/api/scan`, CLI scan UX, `--watch`) and OCR remain 0.2.0.

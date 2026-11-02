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
│   ├── datasets/<collection>/        dataset.json schema 3, chunks.jsonl,
│   │                                 index/<space>/generations/<uuid>/ + CURRENT
│   ├── peels/<peel_id>/              peelfeed corpora (read-only for peekseed)
│   ├── extracted/<sha256>.json       legacy extraction cache (unused)
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
| `dataset.json` | 3 | `id`, `name`, `sources[{name,path,sha256,bytes,pages,extractor,status,extraction{state,peel_id,conversion_id,profile,engine,validation,...}}]`, optional collection `status` |
| chunk row | — | `id` (content-derived 16 hex), `ordinal`, `source`, `source_sha256`, `block_index`, `chunk_index`, `chunker_version`, pages, breadcrumb, `doc`, `text`, `heading_only`; optional peelfeed fields `kind`, `image_paths`, `source_pages`, `char_count`, `parts`/`part`, `prev`/`next`, `canonical_ids`, `engine`, `conversion_id`, `peel_id` |
| `info.json` | 3 | `space`, `generation`, `n`, `dim`, `embedding{...probe}`, `chunker` (peel contract), `extractor` (peelfeed), `documents_digest`, `corpus_digest`, `peel{peel_id,conversion_id,profile,validation}`, `skipped_sources`, `sources` |
| job record | 1 | `id`, `kind`, `collection`, `state`, `progress`, `cancel_requested`, timestamps |

Helpers: `chunk_id()`, `documents_digest()`, `corpus_digest()`,
`extraction_state()`/`extraction_default()`, `space_key()` / `space_identity()`,
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

### `peekseed.scan` (W2)

```python
scan(workspace=None, *, prune=False, link=None, progress=None) -> dict
```

Report: `{workspace, started_at, finished_at, counts{new,changed,unchanged,missing,errors},
files[...], collections{id: {status, sources, chunks, embed_calls, reasons}}, embed_calls}`.

Behaviour: walk `documents/` (skip hidden/`*.part`/`~$*`/`.DS_Store`; resolve
symlinks, reject escapes), sha256 each file, diff the manifest (moved/renamed = a
path update, not a new document), reconcile `dataset.json` (schema 3) source
entries with `status="uploaded"` and `extraction.state="not_extracted"`
(preserving an existing extraction block when content is unchanged), and report
index state read-only. It writes no chunks, builds no index and makes **zero**
embed calls. `link` is accepted for compatibility but ignored. One unreadable
file marks that source `error` with a reason and continues. `--prune` removes
missing sources (explicit data loss).

### `peekseed.ingest.index` (W5)

```python
build_index(dataset_dir, link, *, progress=None, batch_size=32, job=None,
            cancel=None, lock=None, force=False) -> dict  # info.json (+up_to_date)
# raises NoExtractedSources when no source has a usable peel corpus
peel_root() -> Path        # PEELFEED_ROOT (legacy PEEKSEED_PEELFEED_ROOT accepted) or <data_root>/peels
load_index(dataset_dir, space) -> {"info", "vectors", "meta"}
index_state(dataset_dir, space, *, chunker=None, extractor=None) -> dict
    # {"status": "indexed|stale|needs_reindex|not_indexed", "reasons": [...], "generation": str}
probe_cosine(link, dataset_dir, space) -> float     # live embedder vs stored probe
verify_probe(link, dataset_dir, space) -> None      # raises ProbeMismatch (< 0.999)
reindex(dataset_dir, link, *, dry_run=False, gc=False) -> dict   # scope estimate
```

Input: every source with `extraction.state == "extracted"` is read from
`<peel_root>/<peel_id>/extract/corpus/` and gated on realpath allowlist +
`manifest.source_sha256` + recorded `conversion_id` + `validation.errors == 0`.
There is no in-process extraction fallback; non-extracted sources are listed in
`skipped_sources`. A second build over the same corpora returns the live info
with zero embed calls unless `force=True` (reindex).

Layout: `index/<space>/generations/<uuid>/{vectors.npy,meta.json,info.json,probe.npy}`
and `CURRENT` (one line: uuid) written **last** with `os.replace`. A crash leaves
the previous generation serving. Loader validates schema, row alignment, unique
ids and dim; invalid → `needs_reindex`, never silent. Embedding is batched into a
preallocated `vectors.npy` (memmap), retries 429/5xx/timeouts with backoff,
checkpoints per batch, and resumes from the checkpoint. A job record is written to
`.peekseed/jobs/<id>.json` and updated (`running` → `succeeded|failed|interrupted`).
`cancel` is a callable checked between batches.

### `peekseed.peelfeed` (CLI seam)

```python
config_path() -> Path       # <data_root>/config/peelfeed.json (0600, seeded from env)
load() -> dict              # cli, python, repo, root, enabled, timeout, poll,
                            # llm_mode, chunk_profile (env wins over the file)
handshake(force=False) -> dict   # `peelfeed capabilities`, cached 60 s, never raises
status(probe=False) -> {"config", "handshake"}
client() -> PeelfeedClient       # capabilities/status/status_all/ingest/review/
                                 # decide/rebuild/event/archive
extraction_state_for_peel(state) / extraction_from_summary(summary, previous=…)
```

The engine is invoked as a subprocess (`PEEKSEED_PEELFEED_CLI`, or
`PEEKSEED_PEELFEED_PYTHON -m peelfeed.cli` with `PYTHONPATH=<repo>`) with
`--root <root>` and `--json`; stdout is parsed and a nonzero exit raises
`PeelfeedError` (`PeelfeedUnavailable` when the command cannot start). There is
no daemon, HTTP client, base URL or token. The shared peel root is
`PEELFEED_ROOT` (legacy `PEEKSEED_PEELFEED_ROOT` accepted), default
`<data_root>/peels`.

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

## CLI

`peekseed init [--workspace PATH]`, `scan [--prune] [--link ID]`, `status`,
`doctor`, `workspace path|move PATH`, `reindex --all --link ID [--dry-run] [--gc]`,
`migrate [--dry-run]`, `serve`, `version`, and `index` (storage-only upload).
The `import` command was removed with the old importer (M-1); extraction is a
peelfeed job. CLI wiring lives in `peekseed/__main__.py`.

## Module map

| Module | Responsibility |
|---|---|
| `peekseed/contracts.py` | Versioned schemas, content ids, space identity, probe, atomic JSON, upgrades |
| `peekseed/workspace.py`, `config.py` | Workspace resolution, `init`/`doctor`/`status`/`move`, derived-root paths |
| `peekseed/scan.py` | Manifest diff by sha256, dataset/source reconciliation, extraction/index state reporting (no extraction) |
| `peekseed/ingest/index.py` | Storage-only uploads, peelfeed corpus reader/gate, generation-atomic indexes, probe, jobs, `load_index`/`index_state`/`reindex` |
| `peekseed/ingest/lock.py` | Per-collection writer lock with stale-holder detection |
| `peekseed/peelfeed.py` | peelfeed CLI adapter: env-seeded config, capabilities handshake, ingest/review/decide/rebuild/event/archive subprocess calls |
| `peekseed/overlap.py` | Top-k ranking overlap for the cutover acceptance record |
| `peekseed/security.py`, `app.py` | Token/Host/Origin gate, streaming upload limits, inline `/pdf`, peelfeed routes |
| `peekseed/migrate.py` | Explicit schema migration (dry-run default) |
| `peekseed/__main__.py` | CLI wiring for all of the above |

## Acceptance (audit §6.10)

Tests 1–6 and 9–11 are verified end-to-end by `tests/test_acceptance.py`
(`GateOneTests` … `GateElevenTests`); each module also has its own unit suite
(`test_workspace`, `test_scan`, `test_index`, `test_security`,
`test_contracts`, `test_migrate`, `test_engine`, `test_cli`, `test_jobs`,
`test_peelfeed`, `test_overlap`, `test_packaging`).

1. `init --workspace /tmp/ws` creates the tree + anchored gitignore entry;
   `documents/` hashes/mtimes unchanged after a scan.
2. Drop PDF A → scan stores it (`uploaded`); scan again → `0 new, 0 changed`,
   **0** embed calls; after a fake peel + build → indexed.
3. Add PDF B, extract it (fake peel) → build → B searchable, A's ids unchanged.
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

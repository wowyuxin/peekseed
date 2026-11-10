# Data contracts and module interfaces (0.1.0)

This is the working spec for the home/shared layout + ingestion core. The
authoritative code is [`peekseed/contracts.py`](../peekseed/contracts.py); this
page pins the module boundaries so parallel work cannot drift. Tests are the
gate: `audit §6.10` items 1–6 and 9–11 (see `PEEKSEED_OSS_AUDIT.md` in the
private deployment repo).

## Data layout

Two required environment variables name the only writable locations. Both must
be absolute, existing directories; a missing setting raises `ConfigError` and
stops the program. There are no fallbacks (`./workspace`, `~/peekseed` or a
sibling folder).

```
$PEEKSEED_HOME/                     private runtime data (0700)
├── config/links.json               model links; 0600; api_key_env names only
├── config/settings.json            optional UI title/tagline/examples
├── config/peelfeed.json            peelfeed CLI config; 0600; seeded once
├── datasets/<dataset>/             dataset.json schema 4, chunks.jsonl,
│                                   index/<space>/generations/<uuid>/ + CURRENT
├── history/                        search + dialog history
├── jobs/<id>.json                  job record schema 1
└── uploads/                        staging for an upload in progress

$PEEKSEED_SHARED_DIR/               shared with peelfeed (0700)
├── sources/<sha256>/<name>.pdf     the one stored copy of each uploaded PDF
└── peels/<peel_id>/                peelfeed corpora (read-only for peekseed)
```

peelfeed's `PEELFEED_SHARED_DIR` is this same folder and its peel root is
`$PEEKSEED_SHARED_DIR/peels`; peekseed writes `sources/` and only reads
`peels/`. A dataset source entry's `path` is `sources/<sha256>/<name>`
(relative to the shared dir). A peel's `source/<name>.pdf` is a hardlink to the
stored `sources/` copy. There is no `manifest.json`, no `documents/` and no
`original/`.

## Contracts (`peekseed/contracts.py`) — already implemented

| Contract | Schema | Notes |
|---|---|---|
| `dataset.json` | 4 | `id`, `name`, `sources[{name,path (sources/<sha256>/<name>),sha256,bytes,pages,extractor,status,extraction{state,peel_id,conversion_id,profile,engine,review_jobs,validation,...}}]`, optional collection `status` |
| chunk row | — | `id` (content-derived 16 hex), `ordinal`, `source`, `source_sha256`, `block_index`, `chunk_index`, `chunker_version`, pages, breadcrumb, `doc`, `text`, `heading_only`; optional peelfeed fields `kind`, `image_paths`, `source_pages`, `char_count`, `parts`/`part`, `prev`/`next`, `canonical_ids`, `engine`, `conversion_id`, `peel_id` |
| `info.json` | 3 | `space`, `generation`, `n`, `dim`, `embedding{...probe}`, `chunker` (peel contract), `extractor` (peelfeed), `documents_digest`, `corpus_digest`, `peel{peel_id,conversion_id,profile,validation}`, `skipped_sources`, `sources` |
| job record | 1 | `id`, `kind`, `collection`, `state`, `progress`, `cancel_requested`, timestamps |

Helpers: `chunk_id()`, `documents_digest()`, `corpus_digest()`,
`extraction_state()`/`extraction_default()`, `space_key()` / `space_identity()`,
`probe_sha256()`, `stale_reasons()`, `new_job()`,
`validate_*()`, `upgrade_dataset()`, `upgrade_info()`, `read_json()` /
`write_json_atomic()`.

Rules: readers refuse a **newer** schema (`SchemaTooNew`); older schemas are
upgraded explicitly (`peekseed migrate`); JSON writes are atomic.

### Schema 4 (M2 CP1) — `extraction.review_jobs` and `corrections_conflict`

The dataset schema went from 3 to 4 in one migration, and `peekseed migrate`
upgrades older files in place (dry-run by default):

- every source's `extraction` block gains `review_jobs`, a mapping
  `{"<job_id>": {"type": "report|discuss|build|ocr", "created_at": …, "batch_id": …}}`.
  peekseed persists the peelfeed job ids here so a restarted service can re-query
  the real job status instead of re-running it (M2 Phase 2+). An older dataset
  gets `{}`; the field is validated as an object of objects.
- `EXTRACTION_STATES` additionally accepts `corrections_conflict`: a system
  rebuild was blocked because a historical correction no longer validates. The
  current corpus is left untouched (the source stays readable) and the conflict
  is surfaced for the user to resolve (M2 Phase 2). No other state changed.

`INFO_SCHEMA` stays 3: index metadata is unaffected by review state. Review
state itself is owned by peelfeed under `<peel>/extract/review/`; peekseed never
stores it, only proxies the CLI (see `peekseed/peelfeed.py` below). Review calls
require peelfeed `capabilities.review_schema == 2` and otherwise return `409`.

### Review plan entries and resolutions (CP1.1, contract v1.3)

A `POST …/review/plan` `select` of an R0 resolution strategy
(`confirm_no_header`, `keep_visual_fallback`, `keep_original_figure`) creates an
entry whose `action.type` is `resolution`. Its `confirm` writes the R0
resolution (the same shape as a defer) and sets the entry status directly to
`applied`: no batch build, no corpus change, no reindex. Operation entries
(`use_first_row_header`, `inherit_parent_header`, …) confirm to `confirmed` and
wait for an ApplyBatch. The review summary's `counts.confirmed`
("已确认待发布") therefore counts operation entries only; applied R0 entries are
`counts.applied`. The review payload's `derived_from.converter_code_hash` is a
diagnostic converter-code hash (`sha256(canonical converter_hashes)[:16]`), not
`normalize.engine_fingerprint()`, and is never used for stale/CAS.

## Module interfaces

### `peekseed.config`

```python
class Paths:
    root, shared_dir, sources_dir, peels_dir, config_dir, links_file,
    settings_file, datasets_dir, history_dir, jobs_dir, uploads_dir
    def as_dict(self) -> dict          # all absolute paths as strings

paths() -> Paths                       # cached; ConfigError when a var is missing
ensure() -> Paths                      # mkdir the layout, idempotent
data_root() -> Path                    # paths().root
```

### `peekseed.workspace`

```python
init(p=None) -> {"home", "shared_dir", "created"}
    # create the home/shared tree, chmod 0700, write an empty 0600 link store
status(p=None) -> {"paths", "collections", "links", "peelfeed", "counts"}
doctor(p=None) -> {"paths", "issues", "links", "schemas", "peelfeed"}
dataset_dirs(p) / index_info_files(dataset_dir)
```

There is no `Workspace` class, no `resolve()`, no `move()` and no gitignore
guard: the two folders are given by the deployment and the module only reports
on them.

### `peekseed.ingest.index` (W5)

```python
build_index(dataset_dir, link, *, progress=None, batch_size=32, job=None,
            cancel=None, lock=None, force=False) -> dict  # info.json (+up_to_date)
# raises NoExtractedSources when no source has a usable peel corpus
peel_root() -> Path        # $PEEKSEED_SHARED_DIR/peels (realpath allowlist)
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
`$PEEKSEED_HOME/jobs/<id>.json` and updated (`running` → `succeeded|failed|interrupted`).
`cancel` is a callable checked between batches.

### `peekseed.ingest` — shared sources

```python
create_dataset(paths, name, note="") -> dict
add_source(dataset, source_path) -> dict   # store once, record sha256/bytes
store_shared_source(source, digest) -> Path  # <shared>/sources/<sha256>/<name>, 0600
stored_source_path(dataset_dir, entry) -> Path | None
release_shared_source(sha) -> None         # delete when no dataset references it
find_source(dataset_dir, sha) / update_source(...) / remove_source(...)
```

An upload stores the bytes **once** in the shared pool; a second dataset that
uploads the same content gets its own source entry pointing at the same stored
file, and deleting one dataset releases the file only when no other dataset
references it.

### `peekseed.peelfeed` (CLI seam)

```python
config_path() -> Path       # $PEEKSEED_HOME/config/peelfeed.json (0600, seeded once)
load() -> dict              # cli, python, repo, root, enabled, timeout, poll,
                            # llm_mode, chunk_profile (env wins over the file)
handshake(force=False) -> dict   # `peelfeed capabilities`, cached 60 s, never raises;
                                 # also exposes review_schema/operations/resolutions/strategies
status(probe=False) -> {"config", "handshake"}
client() -> PeelfeedClient       # capabilities/status/status_all/ingest/review/
                                 # plan/review-report/rebuild/event/archive
extraction_state_for_peel(state) / extraction_from_summary(summary, previous=…)
REVIEW_SCHEMA (=2), REVIEW_GROUPS, PLAN_ACTIONS, DEFER_REASONS
```

The engine is invoked as a subprocess (`PEEKSEED_PEELFEED_CLI`, or
`PEEKSEED_PEELFEED_PYTHON -m peelfeed.cli` with `PYTHONPATH=<repo>`) with
`--root <root>` and `--json`; stdout is parsed and a nonzero exit raises
`PeelfeedError` (`PeelfeedUnavailable` when the command cannot start). There is
no daemon, HTTP client, base URL or token. The peel root is always
`$PEEKSEED_SHARED_DIR/peels`. The child gets an allowlisted environment: the
usual `PATH`/`HOME`/`USER`/`LANG`/`TMPDIR` set plus every `PEELFEED_*` variable
(and `PYTHONPATH` for the `-m` form); peekseed's token and API keys never reach
it.

M2 review calls: `review(peel_id, group=…, status=…, item=…, limit=…)` proxies
the structured schema-2 view; `plan(peel_id, payload)` runs
`plan <peel_id> <select|confirm|lock|unlock|defer> [--input -] [--entry … --base-plan-version …]`
and returns the complete ReviewPlan. A resolution entry becomes `applied` on
confirm while an operation entry becomes `confirmed`; a locked entry rejects
select/defer with `conflict` (409). `review_report(peel_id, item_ids=…,
cluster_ids=…)` runs the synchronous CP1 `review-report`. The old `decide()`
method was **removed**. A failed command's error is parsed from the stdout
`{"error": {"code", "message", "detail"}}` and/or the stderr
`error [<code>] <message>` line and mapped to an HTTP status: `busy → 423`,
`conflict | stale | review_schema | corrections_conflict → 409`,
`validation | privacy_confirmation_required → 400`, not found (exit 3 or
`not_found`) → `404`, anything else → `502`.

### `peekseed.security` (W8)

```python
token_enabled() -> bool                    # PEEKSEED_TOKEN set
install(app) -> None                       # middleware: token + Host/Origin
check_upload_stream(...) / limits          # per-file, file-count, total size
```

Mutating `/api/*` routes require the token when enabled and reject a hostile
`Origin`/`Host`; reads stay open. Uploads stream to disk with limits checked
**before** reading. `/pdf` is served inline from the shared `sources/` pool
(`$PEEKSEED_SHARED_DIR/sources`), with traversal and symlink escapes rejected.

## CLI

`peekseed init`, `status`, `doctor`, `reindex --all --link ID [--dry-run] [--gc]`,
`migrate [--dry-run|--apply]`, `serve [--host H] [--port P]`, `version`, and
`index --name NAME --file PATH` (storage-only upload).
The `import` command was removed with the old importer (M-1); extraction is a
peelfeed job. CLI wiring lives in `peekseed/__main__.py`.

## Module map

| Module | Responsibility |
|---|---|
| `peekseed/contracts.py` | Versioned schemas, content ids, space identity, probe, atomic JSON, upgrades |
| `peekseed/config.py` | Required `PEEKSEED_HOME`/`PEEKSEED_SHARED_DIR`, `Paths`, settings, bind host/port |
| `peekseed/workspace.py` | `init`/`status`/`doctor` over `config.Paths` |
| `peekseed/ingest/index.py` | Storage-only uploads, shared-source store, peelfeed corpus reader/gate, generation-atomic indexes, probe, jobs, `load_index`/`index_state`/`reindex` |
| `peekseed/ingest/lock.py` | Per-dataset writer lock with stale-holder detection |
| `peekseed/peelfeed.py` | peelfeed CLI adapter: env-seeded config, capabilities handshake (incl. `review_schema`), ingest/review/plan/review-report/rebuild/event/archive subprocess calls |
| `peekseed/overlap.py` | Top-k ranking overlap for the cutover acceptance record |
| `peekseed/security.py`, `app.py` | Token/Host/Origin gate, streaming upload limits, inline `/pdf`, peelfeed routes |
| `peekseed/migrate.py` | Explicit schema migration (dry-run default) |
| `peekseed/__main__.py` | CLI wiring for all of the above |

## Acceptance (audit §6.10)

Tests 1–6 and 9–11 are verified end-to-end by `tests/test_acceptance.py`
(`GateOneTests` … `GateElevenTests`); each module also has its own unit suite
(`test_workspace`, `test_config`, `test_ingest`, `test_index`, `test_security`,
`test_contracts`, `test_migrate`, `test_engine`, `test_cli`, `test_jobs`,
`test_peelfeed`, `test_overlap`, `test_packaging`).

1. `init` creates the home/shared tree and an empty 0600 link store; an upload
   stores one shared copy (`sources/<sha256>/<name>.pdf`) and creates no
   `original/`.
2. Upload PDF A → one stored copy (`uploaded`); re-uploading the same bytes is
   idempotent (one entry, one stored file); after a fake peel + build →
   indexed, and building again returns `up_to_date` with **0** embed calls.
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
dataset via `$PEEKSEED_HOME/jobs/locks/<dataset>.lock` (stale-holder detection);
the API job is the record on disk, and startup marks crash-left `running` records
`interrupted`. The data-folder surface (`GET /api/workspace`,
`/api/collections`, `/api/datasets/{id}/sources`) and the UI Workspace panel are
wired; `links.py` runs the same `security.validate_url` SSRF guard as the API and
all streams emit `{"type": "error", "status", "detail"}`. Folder scanning
(`POST /api/scan`, a CLI scan UX, `--watch`) and OCR remain 0.2.0.

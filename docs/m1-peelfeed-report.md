# M1 report — peelfeed read path, file management, env/capability handshake

Date: 2026-11-01/02. Scope: `source/` only. Task: `task-3`. Spec:
`peelfeed/docs/peelfeed-integration-design.md` §4.5–§4.7 + Lead's daemon contract.

## What changed

### New module `peekseed/peelfeed.py`
- Config store: `<data_root>/config/peelfeed.json` (0600), seeded once from
  `PEEKSEED_PEELFEED_{BASE,TOKEN,ROOT,ENABLED,TIMEOUT,POLL,LLM_MODE,CHUNK_PROFILE}`
  (a real env var wins over `.env`; `config.load_env_file` uses setdefault).
  Token is stored only in the file and masked in every response; bad base URL /
  token / mode are a `PeelfeedConfigError` that degrades to "unavailable", never
  a crash.
- `PeelfeedClient`: `GET /health`, `GET /capabilities`, `POST/GET/DELETE /peels`,
  `GET /peels/{id}/review`, `POST /peels/{id}/review/{item}`,
  `POST /peels/{id}/rebuild`, `POST /peels/{id}/events`,
  `GET /peels/{id}/assets/{path}`; Bearer token, timeout, typed errors
  (`PeelfeedUnavailable`, `PeelfeedError(status, detail)`, token redacted).
- `handshake()` caches health+capabilities for 60 s and never raises:
  unconfigured/unreachable → `available=False` + short reason. `status(probe=True,
  timeout=2)` is used by `peekseed status`/`doctor`; `status(probe=False)` by the
  API (cached handshake, no per-request network).
- `STATE_MAP` / `extraction_from_summary()` map peel states to
  `not_extracted|queued|running|extracted|failed|stale` and fill
  `peel_id/conversion_id/profile/validation/pages/error`.

### ingest/index.py
- Source management helpers: `find_source`, `stored_source_path`,
  `update_source` (atomic dataset.json merge), `remove_source` (entry + stored
  file). `build_index` now reads `build_summary.json` for the engine fingerprint
  (`info.extractor.version`) and pages, and stamps `engine` on every mapped chunk.
- The M-1 corpus reader/gates are unchanged: realpath allowlist under
  `PEEKSEED_PEELFEED_ROOT`, `manifest.source_sha256`, recorded `conversion_id`,
  `validation.errors == 0`; `skipped_sources`; `no_extracted_sources` 409.

### app.py
- `GET /api/peelfeed/status` — config (token masked), handshake/capabilities and
  queue summary.
- `POST /api/datasets/{id}/sources` — upload 1..N files into an existing dataset
  (same limits/zip-slip/magic checks; storage only; rollback on failure).
- `POST …/sources/{sha}/extract` — 503 when unavailable (source marked `failed`,
  `error="peelfeed unreachable"`), otherwise `POST /peels` → `queued` + `peel_id`.
- `GET …/sources/{sha}/review`, `POST …/review/{item}`, `POST …/rebuild`,
  `GET …/assets/{path}` — review/rebuild/asset proxies (503/404/409/502 mapped).
- `DELETE …/sources/{sha}?peel=keep|delete` — removes the source (+ stored copy);
  `keep` is the default, `delete` asks peelfeed to archive the peel.
- `GET …/sources` now refreshes `queued/running` peels from peelfeed (cheap when
  unavailable) and returns per-source `extraction` + `indexed`.
- `POST …/index` keeps the 409 `no_extracted_sources` gate; the build reads the
  peel corpora from disk, so it works even when the peelfeed daemon is down.
- `app.reset_state()` also resets the peelfeed config/handshake cache.

### workspace.py / __main__.py
- `peekseed status` and `doctor` include a `peelfeed` section (configured/
  reachable/version/corpus schema/formats or a clear "not configured" line).
  `doctor` is still read-only; extraction being unavailable is not an "issue".

### UI (`web/index.html`, single-file vanilla JS)
- Datasets dialog banner: peelfeed not configured / unreachable (Extract
  disabled) or "peelfeed <version> reachable · formats · profiles".
- Per-source rows: name + state chip (`not extracted|queued|running|extracted|
  failed|stale`), Extract/Re-extract (disabled with tooltip when peelfeed is
  unavailable or the stored file is missing), Review (N) for extracted peels.
- Extract polls `GET …/sources` at `PEEKSEED_PEELFEED_POLL` until terminal and
  reports pages/review count or the failure reason.
- Review modal: items with message/asset link, Accept/Reject decisions and
  "Apply & rebuild"; Build index stays disabled until at least one source is
  extracted (and an embed link is enabled). Existing views unchanged.

## Verification

- `ruff check .` → All checks passed.
- `pytest tests -q` → **278 passed** (new `tests/test_peelfeed.py`: config seed/
  0600/masking, handshake cache, typed client + Bearer, graceful unreachable,
  503 extract path with failed state, queued→extracted polling, review/rebuild/
  asset/delete proxies, index gate 409, fake-peel index + zero-embed second
  build, UI wiring).
- Real daemon end-to-end (`127.0.0.1:8031`, token `test-token-123`, peel root
  `workspace/.peekseed/peels`), peekseed on `:8039` with a temp data root and a
  local deterministic embed stub on `:8032`:
  - `GET /api/peelfeed/status` → configured, `available=true`, version 0.1.0,
    corpus_schema 2, formats pdf, queue `{ready:1}`; config file mode 0600 and
    the token never appears in the JSON.
  - uploaded the 12 MB CXL PDF (`sha256 b2bbd2c1…`, same as the daemon's peel) →
    `POST …/extract` → 200, `peel_id=cxl-…-b2bbd2c1`, `reused=true`; source poll
    → `extracted`, `conversion_id 6d7dbf4c…`, pages 1166, validation errors 0,
    review_items 248.
  - review proxy → 248 items; asset proxy → 200 `image/png` (233,812 bytes).
  - `POST /api/datasets/cxl/index` → job succeeded with **2829 chunks** (CXL
    baseline 2829), dim 8; embed stub saw **90** requests (89 batches + probe);
    `info.extractor={name:"peelfeed",version:"dabd9d55ece26c3f"}` (fingerprint
    from `build_summary.json`), `chunker={name:"peel",version:1,…}`,
    `peel={peel_id,conversion_id,profile:"cxl",validation:{errors:0,
    review_items:248}}`, `skipped_sources=[]`; mapped chunks carry
    `block_index/chunk_index/chunker_version=1/kind/image_paths/source_pages/
    char_count/parts/part/canonical_ids/engine/conversion_id/peel_id`.
  - second `POST …/index` → succeeded, embed stub still **90** requests (zero
    additional embed calls); `POST /api/search` → 200 with 3 real CXL sections
    (e.g. `7.7.10.1 … p.440`).
  - `peekseed status` (same env) → collection `indexed`, 2829 chunks; peelfeed
    section "configured … reachable: peelfeed 0.1.0, corpus schema 2, formats
    pdf".
- With no peelfeed configured: upload/list/open/delete work; Extract → 503 with
  a clear reason; `GET /api/peelfeed/status` → configured false; the UI disables
  Extract and shows the banner (covered by tests).

## Decisions / risks

1. `config/peelfeed.json` is seeded once (links.py pattern); later env changes do
   not override the file. A real env var wins over `.env` at seed time.
2. `handshake` caches failures too (60 s) so a down daemon does not slow every
   page; `Extract` explicitly invalidates the cache after a successful submit.
3. The index build reads the filesystem corpus and never requires the daemon, so
   search keeps working when peelfeed is down (design §0.5 failure domains).
4. `POST /peels` sends the stored absolute path (the daemon copies the file into
   the peel); sha256/name are sent for identity checks and reuse.
5. Deleting a source does not rewrite chunks/index; the next build reports the
   source-set change as `stale` (never silently serving a removed file as
   current). `peel=delete` archives, not destroys (peelfeed owns GC).
6. Review/rebuild decisions are proxied verbatim; peekseed never edits the corpus.

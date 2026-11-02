# HTTP API (0.1.0)

The server is one FastAPI app over the workspace's derived data root. It is meant
to run **loopback, single user**; the optional token and Host/Origin allowlist are
the deliberate-exposure guard (see [security](security.md)). Every route below is
part of the 0.1.0 contract; `POST /api/scan` and the scanning UX are **0.2.0** and
are not listed.

## Conventions

- **Base URL** — `http://127.0.0.1:8030` by default (`PEEKSEED_HOST`,
  `PEEKSEED_PORT`).
- **Errors** — non-streaming failures are `{"detail": "…"}` with the usual 4xx/5xx
  status. Messages are short, path-free and secret-free.
- **Auth** — when `PEEKSEED_TOKEN` is set, every mutating `POST`/`PUT`/`PATCH`/
  `DELETE` under `/api/*` needs `Authorization: Bearer <token>` or
  `X-Peekseed-Token: <token>`. Reads stay open so a browser can still browse.
  Missing/wrong token → `401 {"detail": "…"}`; the token is never echoed.
- **Host/Origin** — when `PEEKSEED_ALLOWED_HOSTS` is set, mutating requests need a
  matching `Host`; a present `Origin` must equal the request host or be listed.
  A hostile `Origin`/`Host` → `403`.
- **Caching** — `/api/*` responses are `Cache-Control: no-store`; `/` is
  `no-cache` with an ETag.
- **Streaming** — `/api/search/stream`, `/api/ask/stream` and
  `/api/agent/stream` are `application/x-ndjson`: one JSON object per line, then
  the connection closes. A failure after the headers is delivered as an error
  event with the same shape on every stream:
  `{"type": "error", "status": <int>, "detail": "…"}`. Clients must treat an
  error event as terminal.
- **Dataset ids** — `^[a-z0-9][a-z0-9._-]{0,63}$`; an invalid id is `400`, a
  missing one `404`.
- **Job records** — the API's job is the persisted record in
  `.peekseed/jobs/<id>.json`; `GET /api/jobs/{id}` reads that file, not memory.
  See [Jobs](#jobs).

## Route index

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI (single HTML file) |
| GET | `/health` | Liveness + dataset count |
| GET | `/api/workspace` | Served absolute paths, last scan, collection state |
| GET | `/api/collections` | Collection list with status/staleness |
| GET | `/api/datasets` | Dataset list |
| GET | `/api/datasets/{id}/sources` | Per-source storage + extraction/index state (refreshes queued/running peels) |
| POST | `/api/datasets` | Upload PDFs (storage only), create a dataset |
| POST | `/api/datasets/{id}/sources` | Upload PDFs into an existing dataset (storage only) |
| POST | `/api/datasets/{id}/sources/{sha}/extract` | Ask peelfeed to build a peel (503 when unavailable) |
| GET | `/api/datasets/{id}/sources/{sha}/review` | Proxy peelfeed's review queue |
| POST | `/api/datasets/{id}/sources/{sha}/review/{item}` | Proxy one review decision |
| POST | `/api/datasets/{id}/sources/{sha}/rebuild` | Proxy peelfeed's rebuild (apply corrections) |
| POST | `/api/datasets/{id}/sources/{sha}/events` | Append an `embed`/`feedback` event to the peel |
| GET | `/api/datasets/{id}/sources/{sha}/assets/{path}` | Proxy one peel/corpus asset (crop, page image, table) |
| DELETE | `/api/datasets/{id}/sources/{sha}` | Remove a source; `?peel=keep|delete` |
| GET | `/api/peelfeed/status` | peelfeed CLI config, capabilities handshake, queue counts |
| DELETE | `/api/datasets/{id}` | Delete a dataset and its derived data |
| POST | `/api/datasets/{id}/index` | Start an index build job (409 `no_extracted_sources` when none extracted) |
| GET | `/api/jobs` | List job records (newest first) |
| GET | `/api/jobs/{id}` | One job record |
| DELETE | `/api/jobs/{id}` | Request cancellation between batches |
| POST | `/api/jobs/{id}/resume` | Resume an interrupted/cancelled/failed build |
| GET | `/pdf/{dataset}/{filename}` | Serve the original PDF inline |
| GET | `/api/links` | Model links (masked) |
| POST | `/api/links` | Create a link |
| PUT | `/api/links/{id}` | Update a link |
| DELETE | `/api/links/{id}` | Delete a link |
| GET | `/api/links/{id}/indexes` | Datasets indexed through this embed link |
| DELETE | `/api/links/{id}/indexes` | Delete those indexes (to change chunk sizes) |
| POST | `/api/links/{id}/use` | Enable/disable a link |
| POST | `/api/links/{id}/test` | Probe a link's endpoint |
| GET | `/api/links/status` | Reachability of every link |
| GET | `/api/control-status` | Panel poll: local stages, models, links |
| POST | `/api/links/{id}/start` | Start a local link's process |
| POST | `/api/links/{id}/stop` | Stop a local link's process |
| POST | `/api/search` | Search one dataset |
| POST | `/api/search/stream` | Search, streaming stage events |
| GET | `/api/ask-status` | Answer-model status and limits |
| POST | `/api/ask/stream` | Grounded answer, streaming |
| POST | `/api/agent/stream` | Research/auto mode, streaming |
| GET | `/api/history` | Saved search/run history |
| GET | `/api/history/{id}` | One history entry |
| POST | `/api/history/clear` | Clear history (keeps running runs) |
| GET | `/api/settings` | UI settings |
| PUT | `/api/settings` | Update UI settings |

---

## Workspace

### `GET /api/workspace`

Describes the workspace **this server actually serves** (resolved from
`PEEKSEED_WORKSPACE`/`PEEKSEED_DATA` at startup, not from the request's cwd):

```json
{
  "workspace": {
    "root": "/Users/me/peekseed-workspace",
    "documents_dir": "/Users/me/peekseed-workspace/documents",
    "derived_dir": "/Users/me/peekseed-workspace/.peekseed",
    "manifest_file": "…", "config_dir": "…", "links_file": "…",
    "settings_file": "…", "datasets_dir": "…", "extracted_dir": "…",
    "history_dir": "…", "jobs_dir": "…", "uploads_dir": "…"
  },
  "last_scan": "2026-10-20T20:05:00Z" ,
  "collections": {
    "cxl": {
      "name": "CXL", "status": "indexed", "scan_status": null,
      "sources": 4, "chunks": 1166,
      "indexes": ["qwen3-vl-embedding-2b-8bit-mlx"],
      "reasons": []
    }
  },
  "counts": {"documents": 42, "missing": 0, "collections": 3, "chunks": 12345},
  "manifest": {"present": true, "error": null, "documents": 42}
}
```

`status` is the live index verdict (`indexed`, `stale`, `needs_reindex`,
`not_indexed`, `chunked`, `error`, `missing`); `reasons` names why. `last_scan`
is `null` until the 0.2.0 scan workflow runs. All paths are absolute.

### `GET /api/collections`

`{"collections": [{...collection row..., "id": "cxl"}], "last_scan": …,
"counts": {…}}` — the same rows as `/api/workspace`, sorted by id.

## Datasets

### `GET /api/datasets`

`{"datasets": [{"id", "name", "created_at", "notes", "sources": ["a.pdf"],
"chunks", "indexes": [{"space", "model", "protocol", "dim", "n",
"created_at"}]}]}`.

### `GET /api/datasets/{id}/sources`

Per-source provenance from `dataset.json`, plus whether the stored copy is still
on disk:

```json
{"dataset": "cxl",
 "sources": [{"name": "CXL_spec.pdf",
              "path": "/…/datasets/cxl/original/CXL_spec.pdf",
              "sha256": "a6694b69…", "bytes": 12400000, "pages": 0,
              "extractor": "", "status": "uploaded",
              "extraction": {"state": "not_extracted", "peel_id": "",
                             "conversion_id": "", "profile": "", "engine": "",
                             "validation": {"errors": 0, "review_items": 0},
                             "started_at": null, "finished_at": null,
                             "error": null},
              "present": true}]}
```

`status`/`extraction.state` is one of `uploaded|not_extracted|queued|running|
extracted|failed|stale` (the per-source chip). Extraction is a peelfeed job;
uploading never parses the file.

### `POST /api/datasets`

`multipart/form-data` with `name`, optional `note`, and one or more `files`
(PDFs only). Limits: 50 MB per file, 50 files, 200 MB per request; the first
bytes must be `%PDF-`. Zip-slip names are sanitised, a rejected upload leaves no
dataset behind, and the dataset directory is created only after every file
passes. Returns `201 {"dataset": {…}}` (same shape as the list row).

### `DELETE /api/datasets/{id}`

Removes the dataset directory (documents included — uploads are copies).
Returns `{"ok": true, "deleted": "<id>"}`.

### `GET /pdf/{dataset}/{filename}`

Serves the stored original **inline** (`Content-Disposition: inline`,
`application/pdf`) so the browser's own viewer opens it and `#page=N` jumps to a
chunk's page. The name is basename-checked and the resolved file must stay inside
the dataset's `original/`; traversal and symlink escapes are `400`.

## peelfeed (extraction engine, CLI only)

peekseed stores uploads; **peelfeed** extracts. The seam is the peelfeed command
line: peekseed runs `peelfeed …` as a subprocess and reads JSON from stdout — no
import of peelfeed's code, no daemon, no base URL, no token. Configuration lives
in `<data_root>/config/peelfeed.json` (0600), seeded once from
`PEEKSEED_PEELFEED_{CLI,PYTHON,REPO,ENABLED,TIMEOUT,POLL,LLM_MODE,CHUNK_PROFILE}`
plus `PEELFEED_ROOT`; environment values win over the file. `peekseed doctor`/
`status` only read it (for the workspace they inspect) and never seed it.

### `GET /api/peelfeed/status`

```json
{"config": {"cli": "/…/peelfeed/.venv/bin/peelfeed", "python": "",
            "repo": "/…/peelfeed", "root": "/…/workspace/.peekseed/peels",
            "enabled": true, "configured": true, "timeout": 7200.0,
            "poll": 2.0, "llm_mode": "off", "chunk_profile": "default",
            "path": "/…/config/peelfeed.json", "present": true, "seeded": true},
 "handshake": {"available": true, "engine": "peelfeed", "version": "0.1.0",
               "cli_protocol": 1, "corpus_schema": 2, "chunk_contract": 1,
               "chunker_version": 1, "formats": ["pdf"],
               "profiles": ["cxl", "generic", …],
               "chunk_profiles": ["default", "large", "small"],
               "llm_modes": ["off", "review", "aggressive"],
               "docling": {"python": "…", "python_present": true,
                           "models_present": true},
               "peel_root": "/…/peels", "error": null, "checked_at": "…"},
 "queue": {"count": 3, "states": {"extracted": 2, "not_extracted": 1}}}
```

The handshake runs `peelfeed capabilities` and is cached 60 s. When unconfigured
or the CLI cannot start the response says `available: false` with a short
reason; uploads, listing, opening the original PDF and deletion keep working,
and Extract is disabled (HTTP 503). `queue` counts every stored source by its
`extraction.state`.

### `POST /api/datasets/{id}/sources/{sha}/extract`

Body (all optional): `{profile?, llm_mode?, chunk_profile?, force?}`. Records
`extraction.state="queued"` and starts a background thread that runs
`peelfeed ingest <stored path> --root <root> --profile … --llm … --chunk-profile
…` and polls `peelfeed status` every `PEEKSEED_PEELFEED_POLL` seconds (looking
the peel up by source sha until the id is known). The response is
`{source, peel{peel_id,state,reused}, capabilities}`; the chip moves
`queued → running → extracted|failed` as the poller and the ingest result
arrive. 503 when the CLI is unavailable (the source is marked `failed` with a
CLI-related error so Retry is honest); 409 when the stored file is missing.

### Review / rebuild / events / assets

- `GET …/review` runs `peelfeed review <peel_id> --json` and returns the queue
  (`items` with asset crops/evidence, suggestions, LLM availability).
- `POST …/review/{item}` with `{decision: "accept"|"reject"|"override", note?,
  evidence?, model?, confidence?}` runs `peelfeed decide` (append-only
  corrections log).
- `POST …/rebuild` returns `202` and runs `peelfeed rebuild` in a thread while
  polling status; the source chip returns to `extracted` when it finishes.
- `POST …/events` with `{event: "embed"|"feedback", data?}` runs `peelfeed
  event` and appends the event to the peel's `.peel` log.
- `GET …/assets/{path}` serves a peel-relative (`<root>/<peel_id>/<path>`) or
  corpus-relative (`<root>/<peel_id>/extract/corpus/<path>`) asset from disk.
  Every candidate is realpath-checked under the configured peel root
  (traversal is `403`) and served with the guessed content type.

### `DELETE /api/datasets/{id}/sources/{sha}?peel=keep|delete`

Removes the source entry (and its stored copy) from the dataset. The default
`peel=keep` leaves the peel for audit/rollback; `peel=delete` runs `peelfeed
archive` best-effort. The index/chunks are not rewritten: the next build reports
the source-set change as staleness.

### Per-source extraction state

`GET /api/datasets/{id}/sources` returns each source with
`extraction={state, peel_id, conversion_id, profile, engine, validation,
started_at, finished_at, error}` and `indexed`. The state chip is one of
`not_extracted | queued | running | extracted | failed | stale`; queued/running
sources are refreshed from `peelfeed status` when the list is read (cheap when
unavailable: the 60 s handshake cache answers without a CLI call).

## Jobs

An index build is a background job. The record on disk is the truth:

```json
{
  "schema": 1, "id": "job-1a2b3c4d5e6f", "kind": "index",
  "collection": "cxl",
  "state": "running",
  "created_at": "2026-10-21T17:00:00Z",
  "updated_at": "2026-10-21T17:00:12Z",
  "started_at": "2026-10-21T17:00:00Z",
  "finished_at": null,
  "progress": {"phase": "embedding", "done": 64, "total": 1166,
               "fraction": 0.35},
  "error": null,
  "cancel_requested": false
}
```

`state` is one of `queued`, `running`, `interrupted`, `cancelled`, `failed`,
`succeeded`. At startup the server marks records left `running` by a crash as
`interrupted` (not failed) so they can be resumed. Only one build per collection
may run: a second `POST /api/datasets/{id}/index` while the lock is held is
`409` with the holder's pid/timestamp; a lock whose holder is gone is detected as
stale and does not block.

### `POST /api/datasets/{id}/index`

Body: `{"link": "<embed link id>"}` (optional; default is the enabled embed
link). The route first checks that at least one source has
`extraction.state == "extracted"` with a `peel_id`: with none it returns
`409 {"detail": "no_extracted_sources: …"}` listing the skipped sources and
never creates a job. Otherwise it acquires the collection's writer lock, writes
a `queued` record, returns `202 {"job": "<id>", "state": "queued"}` and starts
the build on a worker thread. `409` also when no enabled embed link or when the
collection is already being written.

The build reads each extracted source's peelfeed corpus (gate: peel-root
realpath allowlist, source sha256, recorded conversion id,
`validation.errors == 0`), writes `chunks.jsonl`, embeds and flips `CURRENT`.
Non-extracted sources are listed in `info.json` `skipped_sources`. Building an
already-current index makes **zero** embed calls (`up_to_date`).

### `GET /api/jobs`

`?state=running,cancelled&collection=cxl&limit=200` (all optional; `state` is a
comma list). Returns `{"jobs": [record, …]}`, newest `updated_at` first. Corrupt
records are skipped, not fatal.

### `GET /api/jobs/{id}`

The record, `404` when unknown, `400` for an invalid id.

### `DELETE /api/jobs/{id}`

Sets `cancel_requested: true` on a `queued`/`running` job and returns
`{"job": {…}, "cancelling": true}`. The builder checks the latch **between
batches**: the current batch is committed, the record becomes `cancelled`, and
the previous generation stays live (the incomplete generation can be resumed).
`409` for terminal jobs and for `interrupted` jobs (resume those instead).

### `POST /api/jobs/{id}/resume`

Reuses the same record id and continues from the generation checkpoint, so a
restart does not re-embed committed rows. `202 {"job": "<id>", "resumed": true,
"state": "queued"}`. `409` when the job is running/succeeded, the kind is not
`index`, the collection is gone, or another writer holds the lock.

## Model links

Links are the user's own endpoints. Secrets are write-only: responses carry
`api_key_set`, never the key.

### `GET /api/links`

`{"links": [{"id", "role", "label", "protocol", "base_url", "model",
"enabled", "api_key_set", "chunk", "chunk_set"}], "path": "…/config/links.json",
"seeded": true}`. `chunk` is the embed link's chunk sizes
(`{"target_chars", "max_chars", "image_cap"}` or `null`); `chunk_set` names the
peelfeed chunk set they select (`c8000-12000-i3`, `""` when unset).

### `POST /api/links` / `PUT /api/links/{id}`

Body: `id` (create; generated from the label if omitted), `role`
(`embed|rerank|answer`), `label`, `protocol`, `base_url`, `api_key`, `model`,
`enabled`, `chunk` (embed links only: `0 < target_chars ≤ max_chars`,
`1 ≤ image_cap ≤ 3`, or `null`). Validation rejects unknown fields, bad ids, missing labels, bad
protocols, whitespace/control characters in keys, and **SSRF-unsafe URLs**
(cloud metadata, link-local, non-http(s), embedded credentials). Loopback,
RFC1918 and tailnet targets stay allowed. `201 {"link": {…masked…}}` on create;
`200` on update. `404` for an unknown id, `400` for validation. Changing the
`chunk` sizes of a link that already has indexes is `409 chunk_sizes_locked`
(the indexes were embedded from the old chunk set): delete them first.

### `GET` / `DELETE /api/links/{id}/indexes`

`GET` lists the datasets with an index built through this link
(`{"link", "space", "datasets": [...]}`); `DELETE` removes those index
directories (each under its collection lock) and returns `{"removed": [...]}`.

### `DELETE /api/links/{id}`

`{"deleted": "<id>"}`; `404` when unknown.

### `POST /api/links/{id}/use`

Body `{"enabled": true|false}`. Enabling an embed/rerank link disables its
siblings (one active stage link). Returns the masked link.

### `POST /api/links/{id}/test`

Probes the endpoint: embed returns `{"ok", "dim", "detail"}`, answer returns
`{"ok", "models", "detail"}`, rerank returns a probe result. Provider bodies are
never echoed.

### `GET /api/links/status`

`{"links": {"<id>": {"ok"| "reachable", "detail", …}}, "checked_at": <epoch>}`.
Best-effort; a failed probe is data, not an error.

### `GET /api/control-status`

One poll for a local control panel:
`{"service", "models": [{"model", "label"}], "stages": {"embed": {...},
"rerank": {...}}, "links": [...], "answer": {"configured", "default_provider"}}`.
Each stage entry is `{running, model, label, detail, link_id, base_url, enabled,
capabilities: {"start_stop", "models"}}`. `start_stop` is true only when
`config/local_models.json` configures that role and the link points at the same
`capabilities: {"start_stop", "models"}}`. `start_stop` is true only when
`config/local_models.json` configures that role and the link points at the same
loopback port; a missing config file is the supported "no local models" state.
A config file that cannot be trusted does NOT fail this poll: the stage entries
carry `start_stop: false` and the reason in `detail`.

### `POST /api/links/{id}/start` / `POST /api/links/{id}/stop`

Start or stop the local process behind one link. The link must be locally
controllable (see [local-models.md](local-models.md)); otherwise the call is
`400`. The body may name `{"model": "<id>"}` for the embed stage; start returns
the same running/model/label shape as a control-status stage entry. A script
failure is `502` with the script's last output line. Both are mutating `/api/*`
requests, so they need the service token when `PEEKSEED_TOKEN` is set.

## Search

### `POST /api/search`

```json
{"dataset": "cxl", "query": "coherency protocol", "topk": 10,
 "space": null, "rerank": true}
```

Returns:

```json
{"dataset": "cxl", "query": "…", "space": "qwen3-…",
 "model": "Qwen3-VL-Embedding-2B-8bit-mlx", "link": "embed-local",
 "timings": {"retrieve_sec": 0.01, "rerank_sec": 0.2, "probe_sec": 0.02,
             "embed_sec": 0.01, "total_sec": 0.24},
 "reranked": true,
 "results": [{"rank": 1, "score": 0.87, "id": "…", "dataset": "cxl",
              "space": "…", "label": "…", "section": "…", "chapter": "…",
              "numbering": "…", "page_start": 12, "page_end": 13,
              "source": "CXL_spec.pdf",
              "pdf_url": "/pdf/cxl/CXL_spec.pdf#page=12",
              "snippet": "…"}],
 "history_id": "…"}
```

Before embedding the query, the route re-embeds the fixed probe text and
compares it with the stored `probe.npy`. A cosine below **0.999**, a dimension
change, or a legacy index without a probe returns `409` with an actionable
message (rebuild/reindex) instead of comparing vectors from different spaces.
The probe costs one extra short embed call; `probe_sec` reports it.

### `POST /api/search/stream`

Same body. NDJSON events, in order:

- `{"stage": "embed", "status": "start"|"done", "sec": <float|null>}`
  (also `retrieve`/`rerank` stages as they complete),
- `{"result": {…the /api/search response…}}`,
- or the terminal error event `{"type": "error", "status", "detail"}`.

## Ask and research

### `GET /api/ask-status`

`{"configured", "base", "model", "models", "links", "default_provider",
"max_chunks", "max_chunk_chars", "images": {"enabled": false}, "agent":
{"enabled", "max_steps", "detail"}}`. Research mode is opt-in via
`PEEKSEED_AGENT=1`.

### `POST /api/ask/stream`

Body: `dataset`, `query`/`question`, optional `results` (the rows on screen),
`refs`, `max_chunks`, `link`, `model`, `history_id`, `thread_context`. The server
packs numbered excerpts (or searches first), calls the answer link, and streams:

- `{"type": "context", "mode": "ask", "dataset", "excerpts": [{n, id, label,
  section, page_start, page_end, pdf_url, source}], "run_id", "history_id"}`,
- `{"type": "text"|"reasoning", "text": "…"}` deltas,
- `{"type": "done", "mode": "ask", "model", "answer", "chars", "usage",
  "finish_reason", "truncated", "first_token_sec", "total_sec", "run_id",
  "history_id"}`,
- or `{"type": "error", "status", "detail"}`.

The prompt asks the model to cite excerpts as `[n]`. Ask mode does not validate
those in-text numbers; the context excerpts beside the answer are the authoritative
references and link to the PDFs. (Research mode does validate citations against
its ledger.)

### `POST /api/agent/stream`

Same body plus `auto` (search across datasets) and `max_steps`; requires
`PEEKSEED_AGENT=1` (`404` otherwise). Events include `context`, `step`, `note`,
`text`, `text_retract`, `narration`, `reasoning`, `tool_call`, `tool_result`,
`done` (with `answer`, `steps`, `tool_calls`, `ledger`, and the citation audit
`cited`/`unknown` from `AgentSession`), and the terminal `error` event.

## History and settings

### `GET /api/history`

`{"entries": [{id, kind, title, subtitle, created_at, …}], "max_entries": N}`;
best-effort (a missing store returns an empty list).

### `GET /api/history/{id}`

One entry; `?view=0` skips the view counter. `404` when unknown.

### `POST /api/history/clear`

`{"cleared": N, "kept_running": M, "entries": [...]}` — runs still in progress keep
their record.

### `GET /api/settings` / `PUT /api/settings`

UI title/tagline/examples plus the running `version` (read-only in responses).
`PUT` returns `400` on invalid values.

### `GET /health`

`{"ok": true, "datasets": N}` — no auth, no model calls.

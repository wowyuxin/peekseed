# Architecture

peekseed is one Python package and one configurable data directory. There is no
database, no message broker and no external service: a single FastAPI process
serves the UI and the API, reads datasets from disk, and calls whatever model
endpoints the user configured.

```
            browser (single-file UI)
                     |
                     v
   +----------------- app.py (FastAPI) -----------------+
   |  search | ask | agent | datasets | links | localmodels | history |
   +----+---------+---------+----------+---------+------+
        |         |         |          |         |
        v         v         v          v         v
    engine.py  answer.py  agent.py   ingest/    links.py   localmodels.py
        |         |         |          |         |
        +---------+---------+----------+---------+----> httpx --> user models
                                |
                                v
               config.py -> contracts.py -> <workspace>/.peekseed
               workspace.py / scan.py / extract.py -> documents/
```

## Modules

| Module | Responsibility |
|---|---|
| `contracts.py` | Versioned data contracts: manifest/dataset/info/job schemas, content-derived chunk ids, embedding-space identity, probe, atomic JSON, schema upgrades. |
| `workspace.py` | Resolve and manage the document workspace: `init`, `doctor`, `status`, `move`, gitignore guard. |
| `scan.py` | Reconcile `documents/` with the manifest: hashing, diffing, extraction cache, staleness, dirty-collection builds. |
| `extract.py` | Format extraction (PDF, Markdown, HTML, TXT, EPUB, `.eml`; optional extras) with a versioned extractor identity. |
| `config.py` | Resolve the workspace-derived root and every path under it; settings; bind host/port. |
| `links.py` | Model-link config store: schema, validation, masking, atomic 0600 write. |
| `localmodels.py` | Optional machine-local control of local model processes: config, probes, start/stop. |
| `engine.py` | Pure retrieval: tokenizer, BM25, cosine, reciprocal-rank fusion, optional rerank hook, `DatasetIndex`/`IndexStore`. |
| `answer.py` | OpenAI-compatible streaming chat client and generic grounded-answer prompt builders. |
| `prompts.py` | Generic prompt strings used by the answer and research paths. |
| `agent.py` | Transport-free research loop: budgets, observation ledger, repetition guard, forced answer. |
| `tools.py` | Dataset tools the agent calls: list, outline, search, grep, read chunk/section. |
| `history.py` | Persistent search/dialog history (per-entry JSON + index). |
| `ingest/` | Chunking, embedding and generation-atomic index writing. |
| `security.py` | Token + Host/Origin checks for mutating routes, upload limits, URL validation. |
| `migrate.py` | Explicit schema migration (`peekseed migrate`, dry-run default). |
| `app.py` | The HTTP layer that wires all of the above together. |
| `web/index.html` | The single-file UI. |

The dependency direction is intentionally acyclic: `app.py` imports everything;
`engine.py`, `agent.py`, `tools.py`, `history.py`, `scan.py` and `ingest/` import
no web framework and no config, so they can be tested with plain files and fakes.
`links.py`, `config.py`, `contracts.py` and `migrate.py` are small stdlib-only
stores.

## One workspace

`PEEKSEED_WORKSPACE` (default `./workspace`) owns the user's documents and every
derived path; `PEEKSEED_DATA` overrides only the derived root
(`<workspace>/.peekseed`) for advanced layouts:

```
<workspace>/documents/          user-owned sources; never written by peekseed
<workspace>/.peekseed/
    manifest.json               scan state
    config/links.json           model endpoints + masked keys (0600)
    config/settings.json        optional UI copy
    datasets/<collection>/      dataset.json, chunks.jsonl, index/<space>/
    extracted/<sha256>.json     extraction cache
    history/                    history entries + index
    jobs/<id>.json              persistent jobs
    uploads/                    upload staging
```

Nothing outside the workspace is written at runtime (except the `init` gitignore
guard). Code updates, a new clone, or `pip install --upgrade` cannot touch user
data. See [workspace](workspace.md) for resolution rules and invariants.

## Request lifecycle

**Search.** The API resolves the enabled embed link, finds the `DatasetIndex`
whose `space` equals the link's embedding space, embeds the query, runs cosine +
BM25 + RRF, optionally reranks through the enabled rerank link, and maps the
result rows to citations. If no index exists for the active embedding space the
request fails with a clear "build an index" message rather than comparing
vectors from two different spaces.

**Ask.** The browser sends the question plus the result rows it is looking at
(or none, in which case the server searches first). The server selects an answer
link, builds a grounded prompt with numbered excerpts, streams the model's
response, and records the run in history. The prompt asks the model to cite
excerpt numbers as `[n]`, but Ask mode does **not** validate those in-text
citations; the numbered excerpt chips beside the answer are the authoritative
references and link to the PDFs. (Research mode does validate citations against
its ledger.)

**Research.** The same model gets tools over the dataset (`search`, `grep`,
`read_chunk`, `read_section`, `outline`, `list_datasets`). The deterministic
search result set is the seed, not the ceiling. Every observation is numbered in
one ledger and the answer cites those numbers; invented numbers are reported as
unknown, not rendered as links.

**Index build.** `POST /api/datasets/{id}/index` starts a persisted background job
(the folder `scan` workflow is a 0.2.0 feature). The worker extracts text (or
reuses `chunks.jsonl`), chunks it with stable ids, embeds in batches with
retry/backoff through the enabled embed link, writes into
`index/<space>/generations/<uuid>/` and flips `CURRENT` last. The job record under
`.peekseed/jobs/` is the API's source of truth (`GET /api/jobs`, `DELETE` to
cancel between batches, `POST …/resume`); the index store is reloaded when the
job finishes, so a new dataset becomes searchable without restarting the process,
and a failed or cancelled build leaves the previous generation serving.

## Index format

One index per embedding space, written directly (no Chroma, no SQLite):

```
datasets/<id>/index/<space>/
  CURRENT                       one line: the live generation uuid (written last)
  generations/<uuid>/
    vectors.npy   float32 [n, dim], unnormalised (normalised at load)
    meta.json     array of chunk metadata, row-aligned with vectors
    info.json     schema 2: embedding space identity, provenance, probe, sources
    probe.npy     stored probe vector for the query-time space check
```

The `space` key is `"<link id>__<model id>"` (sanitised). The engine only
compares vectors inside one index, so mixing model dimensions is structurally
impossible; the UI shows a dataset as "needs index" when the active space has no
index yet. A `CURRENT` that names a missing generation, or an index that fails
row/dimension validation, is reported as `needs_reindex`, never served.

## Security

- Loopback bind by default; single-user local-first threat model.
- Per-install bearer token (`PEEKSEED_TOKEN`) and Host/Origin allowlist
  (`PEEKSEED_ALLOWED_HOSTS`) required for mutating routes when enabled; reads
  stay open on loopback.
- Keys live only in `.peekseed/config/links.json`, mode 0600, masked in every
  response; derived data is 0700.
- Uploads stream to disk with per-file, file-count and total-size limits enforced
  while reading; filenames are sanitized before any directory is created.
- `/pdf` serves only the dataset's own `original/` copies, inline, with traversal
  and symlink escapes rejected.
- The server never executes shell scripts or starts model processes.

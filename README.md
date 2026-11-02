# peekseed

**Local-first AI research over your own documents.**

peekseed is a single-user, self-hosted web app and Python package that turns a
set of PDFs into a searchable, citable knowledge base you can search, ask
questions about and research with a tool-using agent. It bundles no models: you
point it at the embedding and chat endpoints you already run or subscribe to.
Documents and indexes stay on your machine unless a configured model endpoint
receives text you send it deliberately.

> **Status: 0.1.0.** The 0.1.0 API, workspace layout and data schemas are
> frozen for the release; folder scanning and OCR are 0.2.0 features. See
> [CHANGELOG.md](CHANGELOG.md) and [docs/release.md](docs/release.md).

## Contents

- [What peekseed is](#what-peekseed-is)
- [Requirements](#requirements)
- [Install and quick start](#install-and-quick-start)
- [The 0.1.0 workflow](#the-010-workflow)
- [The workspace model](#the-workspace-model)
- [Model links](#model-links)
- [0.1.0 vs 0.2.0](#010-vs-020)
- [Known limitations](#known-limitations)
- [Documentation](#documentation)
- [Security posture](#security-posture)
- [License](#license)

## What peekseed is

peekseed has three moving parts:

1. **Model links** — the embedder, an optional reranker, and one or more chat
   models, all configured in the browser (Settings) or seeded from environment
   variables. No model is bundled and no key is ever baked into the code.
2. **Datasets** — PDFs you upload (stored only). Extraction is provided by the
   optional **peelfeed** engine, one peel per file; a Build-index job reads those
   corpora, embeds each canonical chunk with the enabled embedder and writes a
   compact vector index. Everything is derived from one workspace root.
3. **Work modes** — keyword+vector search, one-shot grounded answers, and an
   optional research agent that uses tools to read the dataset itself.

The whole stack is Python plus one static HTML file; there is no database, no
message queue and no JavaScript build step. peekseed's dependency set is the web
stack only: it never imports `pypdf`, `docling` or `torch`. Extraction is a
separate engine (peelfeed) with its own process and venv, so uploads and file
management keep working with no extraction engine configured.

## Requirements

- Python **3.11+** (CI runs the suite on 3.11, 3.12, 3.13 and 3.14).
- `pip` (or `uv`) and a POSIX shell for the optional helper script.
- An embedding endpoint and a chat endpoint. Local servers (Ollama, LM Studio,
  llama.cpp, vLLM, ...) or any OpenAI-compatible provider work.
- Disk space for your documents plus the derived index (roughly the text size,
  not a copy of the PDFs, which are stored once).

The only optional extra is the test suite:

| Extra | Adds | Why |
|---|---|---|
| `test` | `pytest` | the offline test suite |

Extraction formats/dependencies belong to peelfeed, not peekseed (see
[docs/formats.md](docs/formats.md)).

## Install and quick start

```bash
python -m venv .venv && . .venv/bin/activate   # optional but recommended
pip install peekseed

peekseed init      # create ./workspace (documents/ + .peekseed/)
peekseed serve     # http://127.0.0.1:8030
```

`peekseed init` prints the resolved absolute paths and refuses to treat a
tracked checkout as a safe workspace. `peekseed serve` binds loopback by
default. Open <http://127.0.0.1:8030>, then:

1. **Settings** → add an embedding link and a chat link (an OpenAI-compatible
   or custom `/embed` endpoint; a reranker is optional).
2. **Datasets** → name the dataset, choose one or more PDFs, click
   *Add dataset*.
3. If peelfeed is configured, click **Extract** on the file row (state chip
   `not_extracted → queued → running → extracted`). Then click **Build index**
   and watch progress (the job is persisted, cancellable and resumable). With no
   peelfeed, upload/list/delete work and Build index returns a clear 409.
4. **Search** the dataset; tick results and click **Ask about these**; switch
   to *Research* when the agent mode is enabled.

From a source checkout, install in editable mode and run the tests:

```bash
pip install -e '.[test]'
python -m pytest -q
```

For headless machines, `peekseed index --name "My docs" --file a.pdf` stores a
dataset without a browser (extraction is triggered through the API/UI once
peelfeed is configured), and `peekseed status` / `peekseed doctor` inspect the
workspace.

## The 0.1.0 workflow

The core loop is **upload → (Extract) → index → search → ask → research**:

1. **Upload.** POST a PDF to `POST /api/datasets` (the UI does this); the
   original file is stored once under
   `<workspace>/.peekseed/datasets/<id>/original/`.
2. **Extract (optional engine).** `POST /api/datasets/{id}/sources/{sha}/extract`
   asks peelfeed to build a peel for that file; the UI polls the per-source state
   until `extracted`/`failed` and offers Review + Apply & rebuild. With no
   peelfeed, Extract is disabled (API 503) and uploads still work.
3. **Index.** `POST /api/datasets/{id}/index` starts a persisted background job
   that reads each extracted peel corpus (gated on the peel-root allowlist, the
   source sha256, the conversion id and zero validation errors), maps the
   canonical chunks, embeds them and writes the vector index for the active
   embedding space. Non-extracted sources are listed as skipped; no extracted
   source is a 409 `no_extracted_sources`. A second job for the same dataset
   returns `409` while the writer lock is held; `DELETE /api/jobs/{id}` cancels
   between batches and `POST /api/jobs/{id}/resume` continues the same record.
4. **Search.** `POST /api/search` fuses keyword and vector retrieval, then
   optionally reranks the pool. Results carry dataset, file, page range and
   excerpt so the UI can open the original PDF at the right page.
5. **Ask.** `POST /api/ask/stream` retrieves a bounded set of excerpts and
   streams a grounded answer from the configured chat model. The answer cites
   excerpts as `[n]`; the numbered chips beside it are the authoritative links
   (the in-text numbers are the model's and are not validated).
6. **Research.** `POST /api/agent/stream` (opt-in via `PEEKSEED_AGENT=1`) runs
   a tool-using agent that can search, read chunks, list datasets and finish
   with a cited answer, bounded by step, tool-call and wall-clock limits.

Search, ask and research are gated on a working embedding probe for the active
space: if the embed link is disabled or the index was built with a different
model, the UI tells you to rebuild instead of silently degrading. Every
`/api/*` response is `Cache-Control: no-store`. The full route list and the
persisted job record are in [docs/api.md](docs/api.md).

## The workspace model

Every mutable path derives from one workspace root (default `./workspace`):

| Variable | Default | Meaning |
|---|---|---|
| `PEEKSEED_WORKSPACE` | `./workspace` | documents/, derived `.peekseed/` data, history, link config |
| `PEEKSEED_DATA` | `<workspace>/.peekseed` | advanced override for the derived root only |
| `PEEKSEED_HOST` | `127.0.0.1` | bind address (loopback by default) |
| `PEEKSEED_PORT` | `8030` | bind port |
| `PEEKSEED_TOKEN` | unset | shared secret required on mutating `/api/*` calls |
| `PEEKSEED_ALLOWED_HOSTS` | unset | comma-separated Host/Origin names the server may answer |
| `PEEKSEED_ENV_FILE` | `./.env` | alternative dotenv file for defaults |

A `.env` in the working directory (template: `.env.example`) is read by
`peekseed` as defaults; a real environment variable always wins.

```
<workspace>/documents/                      user-owned; one top-level folder = one collection
<workspace>/.peekseed/config/links.json     model endpoints; keys mode 0600, masked in the API
<workspace>/.peekseed/datasets/<id>/        original PDFs, chunks.jsonl, index/<space>/
<workspace>/.peekseed/peels/<peel_id>/      peelfeed corpora (source, extract/corpus, .peel)
<workspace>/.peekseed/jobs/                 persisted, cancellable job records
<workspace>/.peekseed/history/              saved ask/research history
```

The workspace is the one directory to back up. `.peekseed/` is derived and
rebuildable; `peekseed workspace move PATH` moves the whole tree. See
[docs/workspace.md](docs/workspace.md) for manifest and migration details.

## Model links

A **link** is one configured endpoint, stored in
`<workspace>/.peekseed/config/links.json`. There are no bundled models: run
your own server and point a link at it. The UI is the normal way to create
links; the same links can be seeded headlessly with `PEEKSEED_EMBED_*`,
`PEEKSEED_RERANK_*` and `PEEKSEED_ANSWER_*` variables (see `.env.example`).
A deployment that runs its models as local processes can optionally start and
stop them from the panel ([docs/local-models.md](docs/local-models.md)).

The four endpoint contracts are summarised here; the full schema, validation
rules and examples are in [docs/model-links.md](docs/model-links.md).

- **Embedding (OpenAI-compatible):** `POST {base}/embeddings`
  `{"model": ..., "input": ["text", ...]}` → `{"data": [{"embedding": [...]}]}`.
  Set the base URL to the API root that `/embeddings` hangs off.
- **Embedding (custom):** `POST {base}/embed`
  `{"inputs": [{"text": "..."}]}` → `{"embeddings": [[...]]}`.
- **Rerank (custom):** `POST {base}/rerank`
  `{"query": {"text": "..."}, "documents": [{"text": "..."}]}`
  → `{"scores": [float, ...]}` aligned with `documents`.
- **Answer (OpenAI-compatible):** `POST {base}/chat/completions` with
  `stream: true`; SSE deltas under `choices[0].delta`, optional
  `reasoning_content`, tool calls and `stream_options.include_usage`.

An index belongs to one embedding space (the enabled embed link's id and
model). Changing either requires rebuilding the dataset's index; the UI shows
when no index exists for the active space.

## 0.1.0 vs 0.2.0

**0.1.0 (this release)** — the upload-driven product:

- Workspace init/status/doctor/workspace move, versioned data schemas
  (`dataset.json`/`info.json` schema 3, manifest/job schema 1) with
  `peekseed migrate` upgrades.
- PDF upload via UI/API or `peekseed index` — **storage only** (no pypdf/docling
  in peekseed). Extraction is a peelfeed job; with no engine configured uploads,
  file management and deletes still work and Extract is disabled.
- Background index jobs: persisted records, cancel, resume, writer lock;
  embedding-space identity and reindex with dry-run/GC.
- Hybrid keyword+vector search, optional rerank, streaming ask with citations,
  opt-in research agent.
- History, settings, model-link management/validation, loopback-first security
  guards, JSON HTTP API and the single-file web UI.

**0.2.0 (planned)** — folder-driven ingestion:

- A `documents/` folder scan workflow on top of the storage-only scan
  (`peekseed scan` already discovers/hashes/reconciles; watching and automatic
  extraction jobs are planned).
- Broader peelfeed profiles/formats and multimodal embedding on top of the same
  workspace + corpus contract.

## Known limitations

- **Single user.** No accounts, tenants, roles or audit log. Do not expose it
  to untrusted users.
- **No TLS and loopback by default.** Bind to loopback or put it behind a
  reverse proxy; use `PEEKSEED_TOKEN` and `PEEKSEED_ALLOWED_HOSTS` if you must
  reach it over a network.
- **Extraction is external.** peekseed does not parse documents; without a
  configured peelfeed engine there is no extraction, Review or search over new
  files (upload/list/delete still work).
- **Scan is discovery-only.** `peekseed scan` reconciles `documents/` and
  `dataset.json`; it does not build indexes.
- **No bundled models** and no model lifecycle management: answer quality and
  embedding compatibility are the configured endpoints' responsibility.
- **Index rebuilds are required** when the embedding link's id or model
  changes; there is no cross-space search.
- **PDF viewer is the browser's**, not a vendored PDF.js; page citations open
  the original file served by the local API.
- **Not tested on Windows**: the test suite and helper script assume a POSIX
  environment.

## Documentation

New here? Start with [docs/install.md](docs/install.md).

- [docs/install.md](docs/install.md) — install, first workspace, model links,
  the first dataset and troubleshooting.
- [docs/deploy.md](docs/deploy.md) — run it as a single-user service: exposure,
  smoke checks, backups, upgrades and rollback.
- [docs/api.md](docs/api.md) — HTTP routes, request/response shapes, stream
  events and the persisted job record (new in 0.1.0).
- [docs/workspace.md](docs/workspace.md) — workspace layout, manifest,
  collections and migrations.
- [docs/model-links.md](docs/model-links.md) — link schema and endpoint
  contracts.
- [docs/local-models.md](docs/local-models.md) — optional start/stop control
  for local model processes.
- [docs/ingestion.md](docs/ingestion.md), [docs/formats.md](docs/formats.md) —
  upload → peelfeed corpus → index, the chunk contract, embedding spaces and
  format/profile notes.
- [docs/contracts.md](docs/contracts.md), [docs/architecture.md](docs/architecture.md)
  — module boundaries and the invariants the tests enforce.
- [docs/security.md](docs/security.md) — threat model and upload limits.
- [docs/release.md](docs/release.md) — version/tag policy and release
  checklist.
- [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md),
  [SUPPORT.md](SUPPORT.md), [SECURITY.md](SECURITY.md) — project process.

## Security posture

peekseed is designed to run **locally, for one user**. It binds to loopback by
default and must not be exposed to an untrusted network as-is. Optional guards:
`PEEKSEED_TOKEN` requires a shared secret on mutating `/api/*` calls, and
`PEEKSEED_ALLOWED_HOSTS` pins the Host/Origin names the service may answer.
API keys live only in `<workspace>/.peekseed/config/links.json` (mode 0600),
are never returned to the browser (the UI only learns `api_key_set`), and
model links are SSRF-checked when saved. Uploads are size/type/name checked;
`/pdf` is inline and read-only.

See [docs/security.md](docs/security.md) for the threat model and
[SECURITY.md](SECURITY.md) for how to report a vulnerability.

## License

peekseed is licensed under the [MIT License](LICENSE). Third-party dependency
licenses are recorded in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md); CI
checks the default dependency closure has no AGPL/GPL packages. peekseed itself
does not depend on PyMuPDF/docling/torch — those belong to the separate peelfeed
engine.

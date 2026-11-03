# peekseed

**Local-first AI research over your own documents.**

peekseed is a single-user, self-hosted web app and Python package that turns a
set of PDFs into a searchable, citable knowledge base you can search, ask
questions about and research with a tool-using agent. It bundles no models: you
point it at the embedding and chat endpoints you already run or subscribe to.
Documents and indexes stay on your machine unless a configured model endpoint
receives text you send it deliberately.

> **Status: 0.1.0.** The 0.1.0 API, data-folder layout and data schemas are
> frozen for the release; folder scanning and OCR are 0.2.0 features. See
> [CHANGELOG.md](CHANGELOG.md) and [docs/release.md](docs/release.md).

## Contents

- [What peekseed is](#what-peekseed-is)
- [Requirements](#requirements)
- [Install and quick start](#install-and-quick-start)
- [The 0.1.0 workflow](#the-010-workflow)
- [The data folders](#the-data-folders)
- [Model links](#model-links)
- [0.1.0 vs 0.2.0](#010-vs-020)
- [Known limitations](#known-limitations)
- [Documentation](#documentation)
- [Security posture](#security-posture)
- [License](#license)

## What peekseed is

peekseed has three moving parts:

1. **Model links** — the embedder, an optional reranker, and one or more chat
   models, all configured in the browser (Settings). No model is bundled, no key
   is ever baked into the code, and a link names the environment variable that
   holds its key instead of storing it.
2. **Datasets** — PDFs you upload (stored only). Extraction is provided by the
   optional **peelfeed** engine, one peel per file; a Build-index job reads those
   corpora, embeds each canonical chunk with the enabled embedder and writes a
   compact vector index. Everything is derived from two required data folders
   (`PEEKSEED_HOME` and `PEEKSEED_SHARED_DIR`).
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

export PEEKSEED_HOME="$HOME/peekseed-data/home"
export PEEKSEED_SHARED_DIR="$HOME/peekseed-data/shared"
mkdir -p "$PEEKSEED_HOME" "$PEEKSEED_SHARED_DIR"
peekseed init      # create the home/shared tree and print the paths
peekseed serve     # http://127.0.0.1:8030
```

Both variables are **required** and must be absolute, existing directories; there
are no fallbacks. A deployment sets them in an env file (mode 0600), for example
`deploy/env/peekseed.env`. `peekseed init` creates the subfolders, chmods them
0700 and writes an empty 0600 link store; a missing setting stops the command
with a clear error. `peekseed serve` binds loopback by default. Open
<http://127.0.0.1:8030>, then:

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
data folders.

## The 0.1.0 workflow

The core loop is **upload → (Extract) → index → search → ask → research**:

1. **Upload.** POST a PDF to `POST /api/datasets` (the UI does this); the file is
   stored once in the shared sources pool at
   `$PEEKSEED_SHARED_DIR/sources/<sha256>/<name>.pdf`.
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
   excerpt so the UI can open the stored PDF at the right page.
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

## The data folders

Every mutable path derives from two required folders — there are no defaults and
no fallbacks (`./workspace`, `~/peekseed` or a sibling folder):

| Variable | Meaning |
|---|---|
| `PEEKSEED_HOME` | **required** — private state: config (links, settings, peelfeed), datasets, history, jobs, uploads, logs |
| `PEEKSEED_SHARED_DIR` | **required** — shared with peelfeed: `sources/<sha256>/<name>.pdf` and `peels/<peel_id>/` |
| `PEEKSEED_HOST` | bind address (loopback by default) |
| `PEEKSEED_PORT` | bind port (`8030`) |
| `PEEKSEED_TOKEN` | shared secret required on mutating `/api/*` calls |
| `PEEKSEED_ALLOWED_HOSTS` | comma-separated Host/Origin names the server may answer |
| `PEEKSEED_ENV_FILE` | explicit dotenv file for defaults |

The env file (e.g. `deploy/env/peekseed.env`, mode 0600) sets them; a real
environment variable always wins over the file.

```
$PEEKSEED_HOME/config/links.json         model endpoints; api_key_env names only (0600)
$PEEKSEED_HOME/config/settings.json      optional UI copy
$PEEKSEED_HOME/config/peelfeed.json      peelfeed CLI config (0600, seeded once)
$PEEKSEED_HOME/datasets/<id>/            dataset.json, chunks.jsonl, index/<space>/
$PEEKSEED_HOME/jobs/                     persisted, cancellable job records
$PEEKSEED_HOME/history/                  saved ask/research history
$PEEKSEED_HOME/logs/                     serve.log, serve.pid (deploy scripts)
$PEEKSEED_SHARED_DIR/sources/<sha256>/   the one stored copy of each uploaded PDF
$PEEKSEED_SHARED_DIR/peels/<peel_id>/    peelfeed corpora (source, extract/corpus, .peel)
```

Back up `$PEEKSEED_HOME` (minus venvs/caches, logs and upload staging) and
`$PEEKSEED_SHARED_DIR/sources`; peels are rebuildable by extracting again. See
[docs/workspace.md](docs/workspace.md) for the folder contract, modes and
invariants.

## Model links

A **link** is one configured endpoint, stored in `$PEEKSEED_HOME/config/links.json`.
There are no bundled models: run your own server and point a link at it. A link
never stores an API key: it names the environment variable that holds one
(`api_key_env`), and the key itself lives only in the env file; the UI cannot
enter keys. The UI is the normal way to create links.
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

- Data-folder init/status/doctor, versioned data schemas
  (`dataset.json`/`info.json` schema 3, job schema 1) with `peekseed migrate`
  upgrades.
- PDF upload via UI/API or `peekseed index` — **storage only** (no pypdf/docling
  in peekseed; each PDF is stored once in the shared sources pool). Extraction is
  a peelfeed job; with no engine configured uploads, file management and deletes
  still work and Extract is disabled.
- Background index jobs: persisted records, cancel, resume, writer lock;
  embedding-space identity and reindex with dry-run/GC.
- Hybrid keyword+vector search, optional rerank, streaming ask with citations,
  opt-in research agent.
- History, settings, model-link management/validation, loopback-first security
  guards, JSON HTTP API and the single-file web UI.

**0.2.0 (planned)** — folder-driven ingestion:

- A folder-scan workflow (`POST /api/scan` and a CLI scan UX; watching and
  automatic extraction jobs are planned).
- Broader peelfeed profiles/formats and multimodal embedding on top of the same
  data-folder + corpus contract.

## Known limitations

- **Single user.** No accounts, tenants, roles or audit log. Do not expose it
  to untrusted users.
- **No TLS and loopback by default.** Bind to loopback or put it behind a
  reverse proxy; use `PEEKSEED_TOKEN` and `PEEKSEED_ALLOWED_HOSTS` if you must
  reach it over a network.
- **Extraction is external.** peekseed does not parse documents; without a
  configured peelfeed engine there is no extraction, Review or search over new
  files (upload/list/delete still work).
- **Uploads only.** Folder scanning (`POST /api/scan`, a CLI scan UX) and OCR are
  0.2.0 features; PDFs enter through upload in 0.1.0.
- **No bundled models** and no model lifecycle management: answer quality and
  embedding compatibility are the configured endpoints' responsibility.
- **Index rebuilds are required** when the embedding link's id or model
  changes; there is no cross-space search.
- **PDF viewer is the browser's**, not a vendored PDF.js; page citations open
  the stored file served by the local API.
- **Not tested on Windows**: the test suite and helper script assume a POSIX
  environment.

## Documentation

New here? Start with [docs/install.md](docs/install.md).

- [docs/install.md](docs/install.md) — install, the data folders, model links,
  the first dataset and troubleshooting.
- [docs/deploy.md](docs/deploy.md) — run it as a single-user service: exposure,
  smoke checks, backups, upgrades and rollback.
- [docs/api.md](docs/api.md) — HTTP routes, request/response shapes, stream
  events and the persisted job record (new in 0.1.0).
- [docs/workspace.md](docs/workspace.md) — the data-folder layout, modes and
  invariants.
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
API keys live only in the 0600 env file; `links.json` stores the variable name
(`api_key_env`), never a key, and the browser only learns `api_key_env` and
`api_key_set`. Model links are SSRF-checked when saved. Uploads are
size/type/name checked; `/pdf` is inline and read-only.

See [docs/security.md](docs/security.md) for the threat model and
[SECURITY.md](SECURITY.md) for how to report a vulnerability.

## License

peekseed is licensed under the [MIT License](LICENSE). Third-party dependency
licenses are recorded in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md); CI
checks the default dependency closure has no AGPL/GPL packages. peekseed itself
does not depend on PyMuPDF/docling/torch — those belong to the separate peelfeed
engine.

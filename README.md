# peekseed

**Local-first AI research over your own documents.**

peekseed is a single-user, self-hosted web app and Python package that turns a
set of PDFs into a searchable, citable knowledge base you can search, ask
questions about and research with a tool-using agent. It bundles no models: you
point it at the embedding and chat endpoints you already run or subscribe to.
Documents and indexes stay on your machine unless a configured model endpoint
receives text you send it deliberately.

> **Status: 0.1.0rc1 (release candidate).** The 0.1.0 API, workspace layout and
> data schemas are frozen for the release; folder scanning and OCR are 0.2.0
> features. See [CHANGELOG.md](CHANGELOG.md) and [docs/release.md](docs/release.md).

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
2. **Datasets** — PDFs you upload. A background job extracts text, chunks it,
   embeds each chunk with the enabled embedder and writes a compact vector
   index. Everything is derived from one workspace root.
3. **Work modes** — keyword+vector search, one-shot grounded answers, and an
   optional research agent that uses tools to read the dataset itself.

The whole stack is Python plus one static HTML file; there is no database, no
message queue and no JavaScript build step. PDF text extraction uses `pypdf`,
so a default install needs no system libraries.

## Requirements

- Python **3.11+** (CI runs the suite on 3.11, 3.12, 3.13 and 3.14).
- `pip` (or `uv`) and a POSIX shell for the optional helper script.
- An embedding endpoint and a chat endpoint. Local servers (Ollama, LM Studio,
  llama.cpp, vLLM, ...) or any OpenAI-compatible provider work.
- Disk space for your documents plus the derived index (roughly the text size,
  not a copy of the PDFs, which are stored once).

Optional extras are opt-in and never imported at startup:

| Extra | Adds | Why |
|---|---|---|
| `office` | `python-docx`, `python-pptx` | DOCX/PPTX extraction |
| `tables` | `openpyxl` | XLSX extraction |
| `ocr` | `pytesseract`, `Pillow` | OCR for scanned PDFs (**0.2.0 workflow**) |
| `layout` / `pymupdf` | `pymupdf` | alternative PDF extractor/renderer |
| `test` | `pytest` | the offline test suite |

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
3. On the new row, click **Build index** and watch progress (the job is
   persisted, cancellable and resumable).
4. **Search** the dataset; tick results and click **Ask about these**; switch
   to *Research* when the agent mode is enabled.

From a source checkout, install in editable mode and run the tests:

```bash
pip install -e '.[test]'
python -m pytest -q
```

For headless machines, `peekseed index --name "My docs" --file a.pdf` uploads
and indexes without a browser, and `peekseed status` / `peekseed doctor`
inspect the workspace.

## The 0.1.0 workflow

The core loop is **upload → index → search → ask → research**:

1. **Upload.** POST a PDF to `POST /api/datasets` (the UI does this); the
   original file is stored once under
   `<workspace>/.peekseed/datasets/<id>/original/`.
2. **Index.** `POST /api/datasets/{id}/index` starts a persisted background job
   that extracts text, chunks it (~1,200 characters, page ranges and section
   breadcrumbs preserved), embeds the chunks and writes the vector index for
   the active embedding space. A second job for the same dataset returns `409`
   while the writer lock is held; `DELETE /api/jobs/{id}` cancels between
   batches and `POST /api/jobs/{id}/resume` continues the same record.
3. **Search.** `POST /api/search` fuses keyword and vector retrieval, then
   optionally reranks the pool. Results carry dataset, file, page range and
   excerpt so the UI can open the original PDF at the right page.
4. **Ask.** `POST /api/ask/stream` retrieves a bounded set of excerpts and
   streams a grounded answer from the configured chat model. The answer cites
   excerpts as `[n]`; the numbered chips beside it are the authoritative links
   (the in-text numbers are the model's and are not validated).
5. **Research.** `POST /api/agent/stream` (opt-in via `PEEKSEED_AGENT=1`) runs
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
<workspace>/.peekseed/jobs/                 persisted, cancellable job records
<workspace>/.peekseed/history/              saved ask/research history
```

The workspace is the one directory to back up. `.peekseed/` is derived and
rebuildable; `peekseed workspace move PATH` moves the whole tree. See
[docs/workspace.md](docs/workspace.md) for manifest and migration details.

## Model links

A **link** is one configured endpoint, stored in
`<workspace>/.peekseed/config/links.json`. There are no bundled models, and
model processes are never started or stopped by peekseed — run your own server
and point a link at it. The UI is the normal way to create links; the same
links can be seeded headlessly with `PEEKSEED_EMBED_*`, `PEEKSEED_RERANK_*`
and `PEEKSEED_ANSWER_*` variables (see `.env.example`).

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

**0.1.0 (this release candidate)** — the upload-driven product:

- Workspace init/status/doctor/workspace move, versioned data schemas
  (`dataset.json`/`info.json` schema 2, manifest/job schema 1) with
  `peekseed migrate` upgrades.
- PDF upload via UI/API or `peekseed index`; optional `office`, `tables` and
  `pymupdf`/`layout` extraction extras are installed but belong to the 0.2.0
  folder-scan flow — the 0.1.0 upload path is PDF-only.
- Background index jobs: persisted records, cancel, resume, writer lock;
  embedding-space identity and reindex with dry-run/GC.
- Hybrid keyword+vector search, optional rerank, streaming ask with citations,
  opt-in research agent.
- History, settings, model-link management/validation, loopback-first security
  guards, JSON HTTP API and the single-file web UI.

**0.2.0 (planned)** — folder-driven ingestion:

- The `documents/` folder scan workflow: watching, hashing/diffing, staleness
  and dirty-collection rebuilds, exposed as `POST /api/scan` and in the UI.
  (`peekseed scan` exists in 0.1.0 as a preview command; it writes derived state
  under `.peekseed/` but is not part of the 0.1.0 promise.)
- OCR for scanned PDFs (the `ocr` extra and Tesseract plumbing are prepared
  but not wired into the 0.1.0 flow).
- Broader ingestion ergonomics on top of the same workspace contract.

## Known limitations

- **Single user.** No accounts, tenants, roles or audit log. Do not expose it
  to untrusted users.
- **No TLS and loopback by default.** Bind to loopback or put it behind a
  reverse proxy; use `PEEKSEED_TOKEN` and `PEEKSEED_ALLOWED_HOSTS` if you must
  reach it over a network.
- **No folder scanning yet** (0.2.0): documents enter through upload or
  `peekseed index`; `peekseed scan` is a preview.
- **No OCR yet** (0.2.0): scanned/image-only PDFs extract little or no text.
- **No bundled models** and no model lifecycle management: answer quality and
  embedding compatibility are the configured endpoints' responsibility.
- **Index rebuilds are required** when the embedding link's id or model
  changes; there is no cross-space search.
- **PDF viewer is the browser's**, not a vendored PDF.js; page citations open
  the original file served by the local API.
- **Not tested on Windows**: the test suite and helper script assume a POSIX
  environment.

## Documentation

- [docs/api.md](docs/api.md) — HTTP routes, request/response shapes, stream
  events and the persisted job record (new in 0.1.0).
- [docs/workspace.md](docs/workspace.md) — workspace layout, manifest,
  collections and migrations.
- [docs/model-links.md](docs/model-links.md) — link schema and endpoint
  contracts.
- [docs/ingestion.md](docs/ingestion.md), [docs/formats.md](docs/formats.md) —
  extraction, chunking, embedding spaces and format/licence notes.
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
licenses and the optional PyMuPDF (AGPL-3.0 or commercial) caveat are recorded
in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md); CI checks the default
dependency closure has no AGPL/GPL packages.

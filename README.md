# peekseed

peekseed is a local-first framework for searching, asking about and researching
a set of documents you provide. It has three moving parts:

1. **Model links** -- the embedder, an optional reranker, and one or more chat
   models, all configured in the browser (Settings). No model is bundled.
2. **Datasets** -- PDFs you upload. A background job extracts text, chunks it,
   embeds each chunk with the enabled embedder and writes a compact vector
   index. Everything lives under one data root.
3. **Work modes** -- keyword+vector search, one-shot grounded answers, and an
   agent that uses tools to read the dataset itself.

## Quick start

```bash
cd peekseed
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

python -m peekseed init          # create ./workspace (documents/ + .peekseed/)
python -m peekseed serve         # http://127.0.0.1:8030
```

Then, in the browser:

1. **Settings** -> add an embedding link (OpenAI-compatible or custom `/embed`)
   and a chat link (OpenAI-compatible).
2. **Datasets** -> give it a name, choose one or more PDFs, click *Add dataset*.
3. On the new row, click **Build index** and watch the progress.
4. Search the dataset. Tick results and click **Ask about these**; switch to
   *Research* when the agent mode is enabled.

## Workspace

Every mutable path is derived from one workspace root:

| Variable | Default | Meaning |
|---|---|---|
| `PEEKSEED_WORKSPACE` | `./workspace` | documents/, derived `.peekseed/` data, history, link config |
| `PEEKSEED_DATA` | `<workspace>/.peekseed` | advanced override for the derived root only |
| `PEEKSEED_HOST` | `127.0.0.1` | bind address (loopback by default) |
| `PEEKSEED_PORT` | `8030` | bind port |

A `.env` in the working directory or beside the project (template:
`.env.example`) is read by `python -m peekseed` and `scripts/serve.sh` as
defaults; a real environment variable always wins. Use `PEEKSEED_ENV_FILE` to
point at a different file.

`init` prints the resolved absolute paths and refuses to treat a tracked
workspace as safe. The workspace is the one directory to back up; `.peekseed/` is
derived and rebuildable. See [workspace](docs/workspace.md) and
[API](docs/api.md).

```
<workspace>/documents/         user-owned; one top-level folder = one collection
<workspace>/.peekseed/config/links.json    model endpoints; keys 0600 and masked
<workspace>/.peekseed/datasets/<id>/       original PDFs, chunks.jsonl, index/<space>/
<workspace>/.peekseed/jobs/                persisted, cancellable job records
```

Model links may also be seeded headlessly with `PEEKSEED_EMBED_BASE` / `_MODEL`
/ `_PROTOCOL` / `_API_KEY` and the same `PEEKSEED_RERANK_*` and
`PEEKSEED_ANSWER_*` variables. The UI is the normal path; no endpoint, model or
key is baked into the code.

## Model contracts

- **Embedding (OpenAI-compatible):** `POST {base}/embeddings`
  `{"model": ..., "input": ["text", ...]}` -> `{"data": [{"embedding": [...]}]}`.
  Set the link's base URL to the API root that `/embeddings` hangs off (many
  gateways, local servers and providers follow this shape).
- **Embedding (custom):** `POST {base}/embed`
  `{"inputs": [{"text": "..."}]}` -> `{"embeddings": [[...]]}`.
- **Rerank (custom):** `POST {base}/rerank`
  `{"query": {"text": "..."}, "documents": [{"text": "..."}]}`
  -> `{"scores": [float, ...]}` aligned with `documents`, higher is better.
- **Answer (OpenAI-compatible):** `POST {base}/chat/completions` with
  `stream: true`; SSE deltas under `choices[0].delta`, optional
  `reasoning_content`, tool calls, and `stream_options.include_usage`.

An index belongs to one embedding space: the enabled embed link's id and model.
If you change either, rebuild the dataset's index (the UI shows when the
dataset has no index for the active space).

## Ingestion

PDFs are extracted with `pypdf` (pure Python, no system dependencies). Text is
split on headings and paragraph boundaries into chunks of roughly 1,200
characters, and each chunk records its page range, section and breadcrumb. The
chunks and vectors are written directly -- no Chroma, no intermediate database.

Optional: install `pymupdf` (`pip install -e '.[pymupdf]'`) to use it as the
extractor instead. PyMuPDF is AGPL-3.0 or commercial, so it is not a default
dependency of this MIT-licensed project.

The original PDF is served at `/pdf/<dataset>/<file>` so search results and the
numbered excerpt chips beside an answer open the browser's own PDF viewer at the
right page. Ask answers cite excerpts as `[n]` in the text; those in-text numbers
are the model's, not validated links — the excerpt chips are the authoritative
references. No PDF.js is vendored.

Scanning a `documents/` folder and OCR are **0.2.0** features; in 0.1.0 you add
documents through the upload API/UI or `peekseed import`.

## API

The full route list, request/response shapes, stream events and the persisted job
record are in [docs/api.md](docs/api.md). Every `/api/*` response is
`Cache-Control: no-store`.

## Security posture

peekseed is designed to run **locally, for one user**. It binds to loopback by
default and must not be exposed to an untrusted network as-is. Optional guards:
`PEEKSEED_TOKEN` requires a shared secret on mutating `/api/*` calls, and
`PEEKSEED_ALLOWED_HOSTS` pins the Host/Origin names the service may answer. API
keys live only in `<workspace>/.peekseed/config/links.json` (mode 0600) and are
never returned to the browser; the UI only learns `api_key_set`. Model links are
SSRF-checked when saved. See [security](docs/security.md).

The server never starts or stops model processes. Local model servers (Ollama,
LM Studio, llama.cpp, an OpenAI-compatible inference server, ...) are started by
the user's own service manager, and peekseed only calls their HTTP endpoints.

## Tests

```bash
cd peekseed
python -m unittest discover -s tests -v      # offline; no models needed
```

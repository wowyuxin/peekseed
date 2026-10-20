# peekseed maintenance notes

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

python -m peekseed init          # create ./data
python -m peekseed serve         # http://127.0.0.1:8030
```

Then, in the browser:

1. **Settings** -> add an embedding link (OpenAI-compatible or custom `/embed`)
   and a chat link (OpenAI-compatible).
2. **Datasets** -> give it a name, choose one or more PDFs, click *Add dataset*.
3. On the new row, click **Build index** and watch the progress.
4. Search the dataset. Tick results and click **Ask about these**; switch to
   *Research* when the agent mode is enabled.

## Data root

Every mutable path is derived from one root:

| Variable | Default | Meaning |
|---|---|---|
| `PEEKSEED_DATA` | `./data` | datasets, indexes, history, link config |
| `PEEKSEED_HOST` | `127.0.0.1` | bind address (loopback by default) |
| `PEEKSEED_PORT` | `8030` | bind port |

A `.env` in the working directory or beside the project (template:
`.env.example`) is read by `python -m peekseed` and `scripts/serve.sh` as
defaults; a real environment variable always wins. Use `PEEKSEED_ENV_FILE` to
point at a different file.

The root is created on first use. A checkout can be replaced or upgraded
without touching user data; back up or move the root as one directory.

```
<data>/config/links.json      model endpoints; API keys are 0600 and masked
<data>/datasets/<id>/         original PDFs, chunks.jsonl, index/<space>/
<data>/history/               search and dialog history
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

The original PDF is served at `/pdf/<dataset>/<file>` so page citations open in
the browser's own PDF viewer. No PDF.js is vendored.

## API

See `docs/architecture.md` and the `docs/` folder for the endpoint list. Every
`/api/*` response is `Cache-Control: no-store`.

## Security posture

peekseed is designed to run **locally, for one user**. It binds to loopback by
default, has no authentication, and must not be exposed to an untrusted network
as-is. API keys live only in `<data>/config/links.json` (mode 0600) and are
never returned to the browser; the UI only learns `api_key_set`.

The server never starts or stops model processes. Local model servers (Ollama,
LM Studio, llama.cpp, an OpenAI-compatible inference server, ...) are started by
the user's own service manager, and peekseed only calls their HTTP endpoints.

## Tests

```bash
cd peekseed
python -m unittest discover -s tests -v      # offline; no models needed
```

# Install and first run

This page takes a clean machine to a working peekseed: install the package,
create a workspace, connect your model endpoints, run the local server and
ingest your first PDFs. It is written for a single-user local install. If the
machine will stay on and serve other devices, continue with
[deploy.md](deploy.md).

> **Version note.** This page targets peekseed **0.1.0**. While 0.1.0 is still
> a release candidate, publishing is the last step of the release checklist
> ([release.md](release.md)): until `pip install peekseed` resolves from PyPI,
> build and install the wheel or sdist from this repository.

## Requirements

| Need | Detail |
|---|---|
| Python | **3.11+** (the suite runs on 3.11–3.14) |
| Installer | `pip`, or `uv` if you prefer |
| Shell | a POSIX shell for the optional `scripts/serve.sh` helper |
| Embedding endpoint | OpenAI-compatible `POST {base}/embeddings` or custom `POST {base}/embed` |
| Chat endpoint | OpenAI-compatible streaming `POST {base}/chat/completions`; needed for Ask and Research |
| Reranker endpoint | optional custom `POST {base}/rerank` |
| Disk | your documents plus the derived index; no database server |
| System packages | none for the default PDF path (`pypdf`); OCR needs Tesseract and belongs to the 0.2.0 workflow |

peekseed bundles no models. You point its **links** at endpoints you already
run or subscribe to; the four endpoint contracts are in
[model-links.md](model-links.md).

## Install

A virtual environment keeps peekseed's dependencies away from anything else you
run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install peekseed
peekseed version
```

The same with `uv`:

```bash
uv venv .venv
. .venv/bin/activate
uv pip install peekseed
peekseed version
```

Optional extras are opt-in and never imported at startup:

```bash
pip install 'peekseed[office]'   # DOCX/PPTX extraction
pip install 'peekseed[tables]'   # XLSX extraction
pip install 'peekseed[layout]'   # alternative PDF extractor (PyMuPDF)
pip install 'peekseed[ocr]'      # OCR plumbing (0.2.0 workflow; needs Tesseract)
```

The default install needs no system libraries: PDF text extraction uses
`pypdf`. See [formats.md](formats.md) for the licence notes, especially the
optional PyMuPDF (AGPL) extra.

From a source checkout:

```bash
python -m venv .venv && . .venv/bin/activate
pip install .              # regular install
pip install -e '.[test]'   # developer install with pytest
python -m pytest -q
```

## Create a workspace

A workspace is one directory with two halves: `documents/` (your files) and
`.peekseed/` (derived state: datasets, indexes, history and model links).
Keeping everything under one directory is what makes backup and restore a
single copy.

```bash
peekseed init            # creates ./workspace
peekseed workspace path  # prints the resolved absolute path
peekseed doctor          # paths, gitignore guard, links, schemas
```

Resolution order is `--workspace PATH` or `PEEKSEED_WORKSPACE`, then
`./workspace`, then `~/peekseed` only when the current directory is not
writable (printed with a warning). `init` prints every resolved absolute path,
writes an empty 0600 link store, initialises the manifest, and adds an anchored
`.gitignore` guard (`/workspace/`) to the enclosing checkout so documents cannot
be committed by accident.

A custom location:

```bash
peekseed init --workspace ~/research
```

Do not use a path that a git checkout tracks: `init` writes the anchored
`.gitignore` guard, and `doctor` warns when workspace files are already tracked
by git. The full contract is in [workspace.md](workspace.md).

## Connect model endpoints

The normal path is the UI. Start the server (next section), open **Settings**,
and add an embedding link and a chat link; a reranker is optional. API keys are
stored in `<workspace>/.peekseed/config/links.json` (mode 0600), are never sent
back to the browser, and a link's `base_url` is SSRF-checked when saved.

For a headless first run, links can also be seeded from environment variables
(one link per role, applied on first start):

| Role | Variables |
|---|---|
| Embedding (OpenAI-compatible) | `PEEKSEED_EMBED_BASE`, `PEEKSEED_EMBED_PROTOCOL=openai`, `PEEKSEED_EMBED_MODEL`, `PEEKSEED_EMBED_API_KEY` |
| Embedding (custom) | `PEEKSEED_EMBED_BASE`, `PEEKSEED_EMBED_PROTOCOL=custom`, `PEEKSEED_EMBED_MODEL`, `PEEKSEED_EMBED_API_KEY` |
| Reranker (custom) | `PEEKSEED_RERANK_BASE`, `PEEKSEED_RERANK_MODEL`, `PEEKSEED_RERANK_API_KEY` |
| Answer (OpenAI-compatible) | `PEEKSEED_ANSWER_BASE`, `PEEKSEED_ANSWER_MODEL`, `PEEKSEED_ANSWER_API_KEY` |

The commented template is [`.env.example`](../.env.example); copy it to `.env`.
A real environment variable always wins over the file. The CLI reads `.env`
from the current directory and then from the project root, or from
`PEEKSEED_ENV_FILE` when that is set.

An **index belongs to one embedding space** (the embed link id and its model).
Changing either requires rebuilding the dataset's index; search and ask
re-embed a fixed probe and return `409` with an actionable message when the
live endpoint no longer reproduces the stored probe (cosine ≥ 0.999) instead of
silently comparing vectors from different spaces.

## Start the server

```bash
peekseed serve
# peekseed: http://127.0.0.1:8030/  (workspace: /…/workspace)
```

`peekseed serve --host H --port P --workspace PATH` overrides the defaults.
The server binds loopback and serves plain HTTP; it is not meant to be exposed
to an untrusted network as-is (see [Security](#security) below).

Open <http://127.0.0.1:8030>. Environment variables that matter here:

| Variable | Default | Meaning |
|---|---|---|
| `PEEKSEED_WORKSPACE` | `./workspace` | documents plus derived state |
| `PEEKSEED_DATA` | `<workspace>/.peekseed` | advanced override of the derived root only |
| `PEEKSEED_HOST` | `127.0.0.1` | bind address |
| `PEEKSEED_PORT` | `8030` | bind port |
| `PEEKSEED_TOKEN` | unset | shared secret required on mutating `/api/*` calls |
| `PEEKSEED_ALLOWED_HOSTS` | unset | comma-separated `host[:port]` allowlist for `Host`/`Origin` |
| `PEEKSEED_ENV_FILE` | unset | explicit dotenv file (otherwise `cwd/.env`, then project root) |
| `PEEKSEED_AGENT` | off | enables the Research mode (`POST /api/agent/stream`) |

Answer limits and agent budgets (`PEEKSEED_ASK_*`, `PEEKSEED_RERANK_*`,
`PEEKSEED_AGENT_*`) are listed in [`.env.example`](../.env.example).

## Your first dataset

1. **Settings** — add and enable an embedding link; add a chat link if you want
   Ask/Research. Use *Test* to check reachability before continuing.
2. **Datasets** — name a dataset, choose one or more PDFs, click *Add dataset*.
   The originals are copied under the workspace; the upload limits (50 MB per
   file, 50 files, 200 MB per request) are enforced before the body is parsed.
3. On the row, click **Build index** and watch the persisted job: it extracts
   text, chunks it (~1,200 characters with page ranges and section breadcrumbs),
   embeds the chunks and writes a compact vector index.
4. **Search** the dataset. Results carry dataset, file, page range and excerpt;
   a citation opens the stored PDF at the right page (`/pdf` is served inline).
5. Tick results and click **Ask about these** for a grounded, streamed answer.
   The numbered chips beside the answer are the authoritative links; the `[n]`
   numbers inside the model's text are not validated in Ask mode.
6. Switch to **Research** to let the tool-using agent search, read sections and
   chunks, and finish with a citation-audited answer. Research is opt-in via
   `PEEKSEED_AGENT=1`.

The full HTTP contract (routes, stream events, persisted job record) is in
[api.md](api.md).

## Headless and bulk workflows

Everything the UI does on the upload path is available without a browser:

```bash
# Upload PDFs and build one dataset synchronously (needs an enabled embed link).
peekseed index --name "My docs" --file a.pdf --file b.pdf

# Inspect the workspace and the last scan.
peekseed status
peekseed doctor

# Re-embed an existing dataset in another embedding space.
peekseed reindex --collection my-docs --link embed-local --dry-run
peekseed reindex --collection my-docs --link embed-local
peekseed reindex --all --gc          # drop generations older than CURRENT

# Move a corpus between machines or reproduce a prebuilt corpus without
# re-embedding (versioned manifest; probe canary; idempotent).
peekseed import --manifest corpus.json --dry-run
peekseed import --manifest corpus.json

# Move the whole workspace as one unit.
peekseed workspace move ~/new-workspace
```

`peekseed import` is documented in [import.md](import.md); it needs an enabled
embed link for the probe check and never calls an answer or rerank model.
`peekseed scan` exists as a **preview** of the 0.2.0 folder-scan workflow and is
not part of the 0.1.0 promise ([README](../README.md) has the 0.1.0/0.2.0
split). `peekseed reindex` needs an enabled embed link; `--dry-run` prints the
scope and writes nothing.

## The workspace

```
<workspace>/documents/                      user-owned sources
<workspace>/.peekseed/config/links.json     model endpoints and keys (0600)
<workspace>/.peekseed/config/settings.json  optional UI title/tagline/examples
<workspace>/.peekseed/manifest.json         scan state
<workspace>/.peekseed/datasets/<id>/        originals, chunks.jsonl, index/<space>/
<workspace>/.peekseed/extracted/            extraction cache
<workspace>/.peekseed/history/              saved search/ask/research history
<workspace>/.peekseed/jobs/                 persisted job records
<workspace>/.peekseed/uploads/              temporary upload staging
```

- **Back up the workspace as one directory** (`tar`, restic, Time Machine, …).
  The derived half is rebuildable; `config/links.json` (API keys) is the one
  derived file worth keeping, and documents are user-owned.
- **Deleting `.peekseed/`** loses indexes, history, jobs and links but no
  documents. You must re-add link keys before rebuilding indexes.
- Datasets are copies: deleting a dataset deletes the stored PDFs too.
- Directories are private (0700); history files and the link store are 0600.

## Upgrade, migrate, uninstall

```bash
pip install --upgrade peekseed
peekseed migrate            # dry-run: report any data-schema upgrades
peekseed migrate --apply    # write them (back up the workspace first)
peekseed version            # confirm the version the server will report
```

Restart the server after an upgrade. Schema changes are **forward-only**: there
is no cross-schema downgrade by code alone. To roll back, restore the workspace
backup you took before the upgrade (see the upgrade and rollback section of
[deploy.md](deploy.md)).

Uninstalling the package does not delete your data:

```bash
pip uninstall peekseed
# the workspace directory is left in place; remove it yourself when you no
# longer need the documents, after taking a backup.
```

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `401` on upload/search/ask | `PEEKSEED_TOKEN` is set on the server. Paste it in **Settings → Service token**, or reload the page once as `/#token=…` (the fragment never reaches the server). `GET` requests stay open. |
| `403` on a mutating request | Host or Origin mismatch. Add the name you browse through (`host[:port]`) to `PEEKSEED_ALLOWED_HOSTS`; loopback names and the bind host are always accepted. |
| `409` from search/ask | The embed link changed, the endpoint no longer reproduces the probe, or the index has no probe. Rebuild the index (`Build index` or `peekseed reindex`) with the same embed link/model. |
| `409` from Build index | Another build holds the collection's writer lock. Wait for it, cancel it, or resume an interrupted job; stale locks are detected and do not block. |
| PDF extracts little or no text | It is probably a scanned/image-only PDF. OCR is a 0.2.0 workflow; for 0.1.0 use text-based PDFs or extract text yourself. |
| `peekseed init` refuses | The workspace path is tracked by a git checkout. Choose a path outside the checkout or one the checkout ignores. |
| Port already in use | `peekseed serve --port 8031` (or set `PEEKSEED_PORT`); only one app process may use a workspace. |
| Browser shows stale UI | Reload; the UI is served `no-cache` with an ETag. A hard reload clears a stale bundle. |
| Model endpoint unreachable | Check the endpoint and key, use **Test** in Settings or `GET /api/links/status`; loopback, RFC1918 and tailnet addresses are allowed, cloud-metadata addresses are rejected. |
| Jobs stuck after a restart | Records left `running` become `interrupted`; resume with `POST /api/jobs/{id}/resume` or the UI's Resume action. |

`peekseed doctor` and `peekseed status` are the first two commands to run when
something looks wrong; both are read-only.

## Security

peekseed is a **single-user local** tool: no accounts, tenants or audit log, no
built-in TLS, and a loopback bind by default. Do not expose it to untrusted
users. For a network-reachable deployment, set `PEEKSEED_TOKEN` and
`PEEKSEED_ALLOWED_HOSTS` and put TLS in front (Tailscale or a reverse proxy):
see [deploy.md](deploy.md). The threat model, upload limits and file modes are
in [security.md](security.md).

## Next

- [deploy.md](deploy.md) — run it as a background service on a home server.
- [api.md](api.md), [workspace.md](workspace.md), [model-links.md](model-links.md),
  [local-models.md](local-models.md) — the contracts behind the UI.
- [import.md](import.md) — versioned corpus imports without re-embedding.
- [release.md](release.md), [CHANGELOG.md](../CHANGELOG.md) — version and
  upgrade policy.

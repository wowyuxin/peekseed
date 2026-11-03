# Install and first run

This page takes a clean machine to a working peekseed: install the package,
create its two data folders, connect your model endpoints, run the local server
and ingest your first PDFs. It is written for a single-user local install. If the
machine will stay on and serve other devices, continue with
[deploy.md](deploy.md).

> **Version note.** This page targets peekseed **0.1.0**, the first published
> release. Version and tag policy are in [release.md](release.md); the source
> and release artifacts live in the public GitHub repository.

## Requirements

| Need | Detail |
|---|---|
| Python | **3.11+** (the suite runs on 3.11–3.14) |
| Installer | `pip`, or `uv` if you prefer |
| Shell | a POSIX shell for the optional `scripts/serve.sh` helper |
| Embedding endpoint | OpenAI-compatible `POST {base}/embeddings` or custom `POST {base}/embed` |
| Chat endpoint | OpenAI-compatible streaming `POST {base}/chat/completions`; needed for Ask and Research |
| Reranker endpoint | optional custom `POST {base}/rerank` |
| Disk | your PDFs stored once in the shared sources pool, plus the derived index; no database server |
| Data folders | two absolute, existing directories named by `PEEKSEED_HOME` and `PEEKSEED_SHARED_DIR` |
| Extraction engine | optional: **peelfeed** CLI (separate repo/venv; see [formats.md](formats.md)). Without it uploads/file management still work, Extract is disabled |

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

peekseed's dependency set is the web stack only (FastAPI/uvicorn/httpx/numpy/
pydantic/python-multipart). It never imports `pypdf`, `docling` or `torch`:
extraction is provided by the separate **peelfeed** engine, which has its own
venv and dependencies. See [formats.md](formats.md).

From a source checkout:

```bash
python -m venv .venv && . .venv/bin/activate
pip install .              # regular install
pip install -e '.[test]'   # developer install with pytest
python -m pytest -q
```

## Create the data folders

peekseed keeps every changeable runtime file under **two required folders**:

- `PEEKSEED_HOME` — private state: `config/` (links, settings, peelfeed config),
  `datasets/`, `history/`, `jobs/`, `uploads/` and `logs/`.
- `PEEKSEED_SHARED_DIR` — the folder shared with peelfeed: `sources/<sha256>/`
  holds the one stored copy of each uploaded PDF and `peels/<peel_id>/` holds
  peelfeed's output.

Both must be absolute paths that already exist. There are no fallbacks
(`./workspace`, `~/peekseed` or a sibling folder): a missing setting stops the
program with a clear error. A deployment sets them in an env file (mode 0600),
for example `deploy/env/peekseed.env`:

```dotenv
PEEKSEED_HOME=<ROOT>/livefield/.peekseed
PEEKSEED_SHARED_DIR=<ROOT>/livefield/shared
```

For a first local run, export them and create the directories:

```bash
export PEEKSEED_HOME="$HOME/peekseed-data/home"
export PEEKSEED_SHARED_DIR="$HOME/peekseed-data/shared"
mkdir -p "$PEEKSEED_HOME" "$PEEKSEED_SHARED_DIR"
peekseed init      # create the subfolders, chmod 0700, write an empty 0600 link store
peekseed doctor    # paths, permissions, link store, schemas, peelfeed
```

`peekseed init` never overwrites an existing link store or dataset. Both folders
must be on local disk, outside any code checkout and not on a synced or network
filesystem. The full contract is in [workspace.md](workspace.md).

## Connect model endpoints

The normal path is the UI. Start the server (next section), open **Settings**,
and add an embedding link and a chat link; a reranker is optional. A link never
stores an API key: it names the environment variable that holds one
(`api_key_env`, for example `PEEKSEED_ANSWER_API_KEY`), and the key itself lives
only in the env file (mode 0600) that starts the service. The UI cannot enter a
key; it only shows the variable name and whether the key is set. A link's
`base_url` is SSRF-checked when saved.

The env file is read by the CLI as defaults (a real environment variable always
wins). The CLI loads `.env` from the current directory, then from the project
root, or from `PEEKSEED_ENV_FILE` when that is set.

An **index belongs to one embedding space** (the embed link id and its model).
Changing either requires rebuilding the dataset's index; search and ask
re-embed a fixed probe and return `409` with an actionable message when the
live endpoint no longer reproduces the stored probe (cosine ≥ 0.999) instead of
silently comparing vectors from different spaces.

## Start the server

```bash
peekseed serve
# peekseed: http://127.0.0.1:8030/  (home: /…/peekseed-data/home)
```

`peekseed serve --host H --port P` overrides the bind defaults; the data folders
come from the environment. The server binds loopback and serves plain HTTP; it
is not meant to be exposed to an untrusted network as-is (see [Security](#security)
below).

Open <http://127.0.0.1:8030>. Environment variables that matter here:

| Variable | Default | Meaning |
|---|---|---|
| `PEEKSEED_HOME` | **required** | private runtime data: config, datasets, history, jobs, uploads, logs |
| `PEEKSEED_SHARED_DIR` | **required** | shared folder with peelfeed: `sources/` and `peels/` |
| `PEEKSEED_HOST` | `127.0.0.1` | bind address |
| `PEEKSEED_PORT` | `8030` | bind port |
| `PEEKSEED_TOKEN` | unset | shared secret required on mutating `/api/*` calls |
| `PEEKSEED_ALLOWED_HOSTS` | unset | comma-separated `host[:port]` allowlist for `Host`/`Origin` |
| `PEEKSEED_ENV_FILE` | unset | explicit dotenv file (otherwise `cwd/.env`, then project root) |
| `PEEKSEED_AGENT` | off | enables the Research mode (`POST /api/agent/stream`) |
| `PEEKSEED_LOCAL_MODELS_FILE` | unset | optional local-model control config; a deployment keeps it at `deploy/env/local_models.json` |
| `PEEKSEED_PEELFEED_CLI` | unset | absolute path to the peelfeed console script (preferred); unset disables extraction unless a Python path is set |
| `PEEKSEED_PEELFEED_PYTHON` | unset | absolute path to the peelfeed venv interpreter; fallback invocation is `python -m peelfeed.cli` |
| `PEEKSEED_PEELFEED_REPO` | derived | peelfeed repo root for `PYTHONPATH` (derived from the CLI/python path when unset) |
| `PEELFEED_HOME` | **required by peelfeed** | peelfeed's own home: venvs, Docling/`torch` caches |
| `PEELFEED_SHARED_DIR` | **required by peelfeed** | the same folder as `PEEKSEED_SHARED_DIR`; peekseed forces it when it calls peelfeed |
| `PEEKSEED_PEELFEED_ENABLED` | auto | `0` explicitly disables extraction even when a CLI/python path is set |
| `PEEKSEED_PEELFEED_TIMEOUT` | `7200` | seconds; ingest/rebuild may run for the whole Docling conversion |
| `PEEKSEED_PEELFEED_POLL` | `2` | seconds; how often the UI/worker polls `peelfeed status` |
| `PEEKSEED_PEELFEED_LLM_MODE` | `off` | default `--llm` mode passed to ingest (`off\|review\|aggressive`) |
| `PEEKSEED_PEELFEED_CHUNK_PROFILE` | `default` | default `--chunk-profile` passed to ingest |

Extraction runs the **peelfeed CLI** as a subprocess; there is no daemon, base
URL or token. Point `PEEKSEED_PEELFEED_CLI` at the console script, or
`PEEKSEED_PEELFEED_PYTHON` at the peelfeed venv interpreter (peekseed then runs
`python -m peelfeed.cli` with `PYTHONPATH` set to the repo). peelfeed itself
requires `PEELFEED_HOME` and `PEELFEED_SHARED_DIR`, and the latter must equal
`PEEKSEED_SHARED_DIR` so both sides see the same `sources/` and `peels/`. The
first run seeds `$PEEKSEED_HOME/config/peelfeed.json` (0600) from these values;
environment values win over the file, and the peel root is always
`$PEEKSEED_SHARED_DIR/peels`. `peekseed status`/`doctor` print the CLI path and
whether it answers `capabilities`.

Answer limits and agent budgets (`PEEKSEED_ASK_*`, `PEEKSEED_RERANK_*`,
`PEEKSEED_AGENT_*`) are listed in [`.env.example`](../.env.example).

## Your first dataset

1. **Settings** — add and enable an embedding link; add a chat link if you want
   Ask/Research. Use *Test* to check reachability before continuing.
2. **Datasets** — name a dataset, choose one or more PDFs, click *Add dataset*.
   Each PDF is stored once in the shared sources pool
   (`$PEEKSEED_SHARED_DIR/sources/<sha256>/`); the upload limits (50 MB per file,
   50 files, 200 MB per request) are enforced before the body is parsed.
3. If peelfeed is configured, click **Extract** on the file row (the state chip
   moves `not_extracted → queued → running → extracted`; Review is available for
   extracted peels). Then click **Build index**: peekseed reads the peel corpus,
   embeds the canonical chunks and writes a compact vector index. With no
   peelfeed, upload/list/delete still work and Build index returns a clear 409
   (`no_extracted_sources`).
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

Everything the UI does on the upload path is available without a browser. The
CLI reads the same env file, so export `PEEKSEED_HOME`/`PEEKSEED_SHARED_DIR` (or
set `PEEKSEED_ENV_FILE`) first:

```bash
# Store PDFs as a dataset (storage only; no extraction, no embedding).
peekseed index --name "My docs" --file a.pdf --file b.pdf

# Inspect the data folders and the datasets.
peekseed status
peekseed doctor

# Re-embed an existing dataset in another embedding space.
peekseed reindex --collection my-docs --link embed-local --dry-run
peekseed reindex --collection my-docs --link embed-local
peekseed reindex --all --gc          # drop generations older than CURRENT
```

Extraction is a per-file peelfeed job triggered from the UI/API; there is no
`peekseed import` command any more. `peekseed reindex` needs an enabled embed
link; `--dry-run` prints the scope and writes nothing.

## The data folders

```
$PEEKSEED_HOME/config/links.json         model endpoints; api_key_env names only (0600)
$PEEKSEED_HOME/config/settings.json      optional UI title/tagline/examples
$PEEKSEED_HOME/config/peelfeed.json      peelfeed CLI config (0600, seeded once)
$PEEKSEED_HOME/datasets/<id>/            dataset.json, chunks.jsonl, index/<space>/
$PEEKSEED_HOME/history/                  saved search/ask/research history
$PEEKSEED_HOME/jobs/                     persisted job records
$PEEKSEED_HOME/uploads/                  temporary upload staging
$PEEKSEED_HOME/logs/                     serve.log, serve.pid (deploy scripts)
$PEEKSEED_SHARED_DIR/sources/<sha256>/   the one stored copy of each uploaded PDF
$PEEKSEED_SHARED_DIR/peels/<peel_id>/    peelfeed corpora (source, extract/corpus, .peel)
```

- **Back up** `$PEEKSEED_HOME` (except `env/venv`, `env/uv-cache`, `logs/` and
  `uploads/`) and `$PEEKSEED_SHARED_DIR/sources`. Peels are rebuildable by
  extracting again. See [deploy.md](deploy.md) for the backup script.
- **Deleting the home** loses indexes, history, jobs and links but no uploaded
  bytes (they live in the shared sources). You must re-create links before
  rebuilding indexes.
- A dataset is a set of source entries: deleting it removes its entries and
  releases the stored PDF when no other dataset references it.
- Directories are private (0700); history files, the link store and stored PDFs
  are 0600. API keys live in the env file, never in `links.json`.

## Upgrade, migrate, uninstall

```bash
pip install --upgrade peekseed
peekseed migrate            # dry-run: report any data-schema upgrades
peekseed migrate --apply    # write them (back up the data folders first)
peekseed version            # confirm the version the server will report
```

Restart the server after an upgrade. Schema changes are **forward-only**: there
is no cross-schema downgrade by code alone. To roll back, restore the data-folder
backup you took before the upgrade (see the upgrade and rollback section of
[deploy.md](deploy.md)).

Uninstalling the package does not delete your data:

```bash
pip uninstall peekseed
# the data folders are left in place; remove them yourself when you no longer
# need the uploads, after taking a backup.
```

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `401` on upload/search/ask | `PEEKSEED_TOKEN` is set on the server. Paste it in **Settings → Service token**, or reload the page once as `/#token=…` (the fragment never reaches the server). `GET` requests stay open. |
| `403` on a mutating request | Host or Origin mismatch. Add the name you browse through (`host[:port]`) to `PEEKSEED_ALLOWED_HOSTS`; loopback names and the bind host are always accepted. |
| `409` from search/ask | The embed link changed, the endpoint no longer reproduces the probe, or the index has no probe. Rebuild the index (`Build index` or `peekseed reindex`) with the same embed link/model. |
| `409` from Build index | Another build holds the collection's writer lock. Wait for it, cancel it, or resume an interrupted job; stale locks are detected and do not block. |
| File is `not_extracted` / Build index 409 | Configure the peelfeed CLI (`PEEKSEED_PEELFEED_CLI`, or `PEEKSEED_PEELFEED_PYTHON`) with `PEELFEED_HOME`/`PEELFEED_SHARED_DIR` and click Extract on the file row; extraction is not a peekseed feature. |
| `PEEKSEED_HOME is not set` (or the same for `PEEKSEED_SHARED_DIR`) | Export both variables, or point `PEEKSEED_ENV_FILE` at the env file, before `peekseed init`/`serve`/`status`. They must be absolute and already exist. |
| Port already in use | `peekseed serve --port 8031` (or set `PEEKSEED_PORT`); only one app process may use the same home/shared folders. |
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
- [release.md](release.md), [CHANGELOG.md](../CHANGELOG.md) — version and
  upgrade policy.

# Changelog

All notable changes to peekseed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[PEP 440](https://peps.python.org/pep-0440/) versions. Release candidates are
published as `0.1.0rcN`; see [docs/release.md](docs/release.md) for the
version/tag policy.

## 0.1.0 - 2026-10-23

First public release: local-first AI research over your own documents. This
section rolls up the four release candidates; the rc sections below record the
deployment-validation history.

### Added

- **Workspace and ingestion.** One workspace root (`documents/` plus derived
  `.peekseed/`) with `peekseed init`, `status`, `doctor`, `workspace move` and
  schema `migrate`; PDF upload via the UI/API or `peekseed index`; persisted,
  cancellable and resumable index jobs; generation-atomic indexes with a probe
  canary; `/pdf` served inline for page citation links.
- **Search, ask and research.** Hybrid keyword+vector search, optional rerank,
  streaming grounded answers with numbered excerpt chips and saved history, and
  an opt-in tool-using research agent (`PEEKSEED_AGENT=1`) that audits its
  citations against a ledger.
- **Import contract.** `peekseed import --manifest` reproduces prebuilt or
  Chroma-exported corpora without re-embedding (versioned manifest, deterministic
  ids, probe verification, all-or-nothing per collection).
- **Browser UI and model links.** Single-file web UI, Settings for model links
  and browser service-token provisioning (`#token=...` / Settings -> Service
  token), and optional local-model start/stop control driven by a machine-local
  0600 config file.
- **Documentation and packaging.** Public install ([install.md](docs/install.md))
  and deployment ([deploy.md](docs/deploy.md)) guides, API/contracts/workspace
  docs, PEP 639 metadata, CI on Python 3.11-3.14, community files and a
  tag-triggered release workflow (TestPyPI -> PyPI, PEP 740 attestations,
  CycloneDX SBOM).

### Changed

- rc2 fixed browser writes against the service token; rc3 added optional local
  model control; rc4 added the public documentation (D3). The rc sections below
  have the details.

### Security

- Single-user local-first posture: loopback bind by default, per-install
  `PEEKSEED_TOKEN` for mutating API calls, Host/Origin allowlisting for
  deliberate network exposure, streaming upload limits, filename sanitising,
  SSRF checks on model links, 0700/0600 data modes and `no-store` API responses.
  See [docs/security.md](docs/security.md).

## 0.1.0rc4 - 2026-10-23

Fourth release candidate: public installation and deployment documentation
(D3). There are no runtime behaviour changes beyond the version string.

### Added

- **Public install and deployment guides.** `docs/install.md` takes a new user
  from a clean machine to a first dataset: requirements, install, workspace,
  model links, headless workflows, backup semantics and troubleshooting.
  `docs/deploy.md` covers a single-user server deployment: loopback bind plus
  TLS termination, a pinned install, a manual process or optional
  systemd/launchd units, Tailscale or reverse-proxy exposure, an HTTP smoke
  sequence, backups with a restore drill, forward-only upgrades and rollback.
- **PyPI project links.** `[project.urls]` now carries Homepage, Repository,
  Issues and Changelog for the public repository created at publish time.

### Changed

- README links the new guides, and the model-link section now describes the
  optional local-model start/stop control instead of claiming peekseed never
  manages model processes.
- `docs/security.md` documents the browser token provisioning (`#token=…` /
  Settings -> Service token) and the current workspace/derived defaults.
- `.env.example` reflects the workspace root, `PEEKSEED_TOKEN` and
  `PEEKSEED_ALLOWED_HOSTS`; the stale "no authentication" note is gone.
- The CI sdist check and packaging tests assert that the new docs ship.

## 0.1.0rc3 - 2026-10-22

Third release candidate: opt-in local model process control.

### Added

- **Local model control.** A deployment that runs its embedder/reranker as
  local processes can now start and stop them from a panel through peekseed
  itself: `GET /api/control-status` returns each stage's health and whether the
  active link is locally controllable, and `POST /api/links/{id}/start|stop`
  runs the stage's configured serve script. The machine-local config lives in
  `config/local_models.json` (0600, never committed) and is entirely optional:
  without it nothing changes and no command can run. Only absolute, executable
  scripts named in that file are ever invoked (never through a shell), the
  health URL must be loopback, and the link's loopback port must match, so a
  remote link cannot be handed a local command. Both the config file and
the serve script must be owned by the service user and must not be
writable by group or others; the config is opened with `O_NOFOLLOW`,
re-checked with `fstat`, and the script is re-validated immediately before
every execution.

## 0.1.0rc2 - 2026-10-21

Second release candidate: deployment-validated fixes on top of rc1. Still
pre-publish; validated from a pinned clone (see docs/release.md).

### Added

- **Browser service-token provisioning.** When the server requires
  `PEEKSEED_TOKEN`, the UI previously had no way to send it, so search, ask,
  uploads and link changes failed with `401` behind a trusted proxy. Settings
  now has a **Service token** field (kept only in this browser), the page
  accepts `#token=…` on first load, and every non-GET `/api/*` request carries
  the Bearer header; a `401` explains how to provision the token.

### Fixed

- **Overlap acceptance harness.** `scripts/overlap_check.py` can now send a
  different body key per side (`--baseline-body-field`, `--candidate-body-field`)
  and a separate baseline dataset and token (`--baseline-dataset`,
  `--baseline-token`), keeping `--dataset`/`--token` as fallbacks for both
  sides; it also builds a valid `httpx.Timeout`. This makes the P4 top-k
  overlap record reproducible against a baseline whose search endpoint names
  its collection `corpus` instead of `dataset`.

### Changed

- Deployment-only wording was generalized (the UI token help and the overlap
  harness example), so the published tree carries no deployment paths or
  internal corpus ids.

## 0.1.0rc1 - 2026-10-21

First release candidate of the 0.1.0 line. Not yet published to PyPI; the
release is validated from a pinned clone (see docs/release.md). This entry
summarises the complete 0.1.0 surface.

### Added

- **Workspace-first layout.** `peekseed init` creates `./workspace` with
  user-owned `documents/` and a derived `.peekseed/` root; every mutable path
  descends from `PEEKSEED_WORKSPACE`. Workspace status/doctor/path/move
  commands; a gitignore guard that refuses to treat a tracked checkout as a
  safe workspace; versioned data schemas (`dataset.json`/`info.json` schema 2,
  manifest/job schema 1) with `peekseed migrate` (dry-run by default).
- **Datasets and ingestion.** PDF upload through the UI and
  `POST /api/datasets`, or `peekseed index --name ... --file ...` headlessly.
  PDF extraction with `pypdf` (no system dependencies), heading/paragraph-aware
  chunking (~1,200 characters) with page ranges, sections and breadcrumbs, and
  original-file serving at `/pdf/<dataset>/<file>`. Optional `office`, `tables`
  and `pymupdf`/`layout` extras are installed for the 0.2.0 folder-scan formats;
  the 0.1.0 upload path is PDF-only.
- **Persisted index jobs.** Background embedding jobs are written under
  `.peekseed/jobs/`: `GET /api/jobs`, `GET /api/jobs/{id}`,
  `DELETE /api/jobs/{id}` (cancel between batches) and
  `POST /api/jobs/{id}/resume`. A per-collection writer lock returns `409` for
  concurrent builds, crash-left jobs are reported interrupted on restart, and
  progress survives reconnects.
- **Embedding-space identity and reindex.** An index belongs to one embedding
  space (link id + model); changing the link marks the dataset stale and the
  UI asks for a rebuild. `peekseed reindex` supports `--all`, `--collection`,
  `--dry-run` and `--gc`, with call estimates.
- **Search, ask and research.** Hybrid keyword+vector retrieval (reciprocal
  rank fusion), optional custom reranker, streaming grounded answers with
  numbered excerpt citations, history persistence, and an opt-in research agent
  (`PEEKSEED_AGENT=1`) with bounded steps, tool calls and wall clock.
- **Model links.** A validated `links.json` schema for `embed` (OpenAI or
  custom), `rerank` (custom) and `answer` (OpenAI-compatible) links; keys are
  write-only, stored mode 0600 and masked in the API; links can be probed and
  seeded from `PEEKSEED_*` environment variables.
- **HTTP API and web UI.** The full JSON API documented in
  [docs/api.md](docs/api.md) (workspace, collections, datasets, jobs, links,
  search, ask/research streams, history, settings, health), every `/api/*`
  response marked `Cache-Control: no-store`, and a dependency-free,
  single-file browser UI.
- **Security posture.** Loopback bind by default, CSRF/Origin checks, optional
  `PEEKSEED_TOKEN` for mutating calls, optional `PEEKSEED_ALLOWED_HOSTS`
  allowlist, upload limits, `/pdf` traversal/symlink protection and SSRF checks
  on model links; documented in [docs/security.md](docs/security.md).
- **Project infrastructure.** PEP 639 metadata (`license = "MIT"`,
  `license-files`), `MANIFEST.in`/package-data so the web UI, docs and tests
  ship in the sdist, a GitHub Actions CI workflow (Python 3.11–3.14, ruff,
  dependency-licence check, artifact-content checks and an sdist-install smoke),
  packaging tests, and release process docs.

### Known limitations

- Single user; no accounts or audit log. Loopback-only by default and no TLS.
- Folder scanning and OCR are 0.2.0 features; `peekseed scan` is a preview that
  writes derived state under `.peekseed/` but is not part of the 0.1.0 promise.
- No models are bundled; answer quality and embedding compatibility depend on
  the configured endpoints.
- Not tested on Windows.

## Unreleased

### Added

- **Corpus import contract.** `peekseed import --manifest corpus.json` reproduces
  a workspace from a versioned manifest (schema 1). It is idempotent, uses
  deterministic dataset ids, copies prebuilt indexes/chunks/vectors unchanged
  (zero re-embed) or imports a portable Chroma JSONL export, verifies the stored
  probe against the live embedder (cosine ≥ 0.999 before trusting a copied
  space), and stages each collection so a failed import leaves the previous data
  untouched. `--dry-run` prints the plan without writing or embedding. The P4
  overlap harness lives in `peekseed.overlap` / `scripts/overlap_check.py`. See
  [docs/import.md](docs/import.md).

### Planned for 0.2.0

- The `documents/` folder scan workflow (`POST /api/scan`, staleness/diffing,
  dirty-collection rebuilds) on top of the existing schema.
- OCR for scanned PDFs via the prepared `ocr` extra.

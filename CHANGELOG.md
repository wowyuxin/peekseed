# Changelog

All notable changes to peekseed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[PEP 440](https://peps.python.org/pep-0440/) versions. Release candidates are
published as `0.1.0rcN`; see [docs/release.md](docs/release.md) for the
version/tag policy.

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

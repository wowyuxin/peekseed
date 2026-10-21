# Support

Thanks for using peekseed. This page explains where to get help and what to
include so issues can be resolved quickly.

## Read the documentation first

Most questions are answered by the docs in this repository:

- [README.md](README.md) — install, quick start, workspace model and scope.
- [docs/api.md](docs/api.md) — HTTP API, stream events and job records.
- [docs/workspace.md](docs/workspace.md) — workspace layout, manifests and
  migrations.
- [docs/model-links.md](docs/model-links.md) — endpoint contracts for
  embeddings, rerankers and chat models.
- [docs/ingestion.md](docs/ingestion.md) and [docs/formats.md](docs/formats.md)
  — extraction, chunking and format/licence notes.
- [docs/security.md](docs/security.md) — threat model, upload limits and
  deployment guidance.
- [docs/release.md](docs/release.md) — version/tag policy.
- [CHANGELOG.md](CHANGELOG.md) — what changed between releases.

## Where to ask

- **Bugs and feature requests** — open an issue on the project's issue tracker
  (the public repository is not published yet; templates under
  `.github/ISSUE_TEMPLATE/` describe what to include).
- **Usage questions and ideas** — use the project's discussions/community
  channel once it exists. In the meantime, an issue is fine.
- **Security vulnerabilities** — **never** a public issue; follow
  [SECURITY.md](SECURITY.md).
- **Contributing code** — see [CONTRIBUTING.md](CONTRIBUTING.md).

There is no commercial support offering and no guaranteed response time. This
is a small, single-user local project maintained in the open; reports that
include a minimal reproduction are handled first.

## Before opening an issue

1. Check you are on a supported version (`peekseed version`) and Python 3.11+.
2. Read [docs/workspace.md](docs/workspace.md) and make sure the workspace is
   the one you think it is (`peekseed status`, `peekseed doctor`).
3. Re-run the failing path with `PEEKSEED_*` secrets removed from the output.
4. Reproduce with the minimum configuration, and note whether a model endpoint
   was involved (the test suite runs fully offline with fake endpoints; most
   "no answer" reports come from a link/model mismatch).

## What to include in a report

- peekseed version and how it was installed (`pip`, editable checkout, wheel);
- Python version, operating system and whether the server was loopback-only;
- the exact commands / UI steps and the observed result;
- relevant logs with API keys, tokens and document contents redacted;
- the workspace state: `peekseed status` and `peekseed doctor` output (paths
  can be redacted, but note whether `documents/` and `.peekseed/` are tracked);
- the embedding link id/model if search/index is involved (never the key).

Do **not** attach real documents, API keys, `.peekseed/config/links.json`
contents or `PEEKSEED_TOKEN` values. A synthetic PDF that reproduces the
problem is ideal.

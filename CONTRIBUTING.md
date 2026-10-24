# Contributing to peekseed

Thanks for improving peekseed. This project is a local-first, single-user tool:
the most valuable contributions are bug reports with reproductions, tests for
edge cases, documentation fixes and focused patches that keep the offline test
suite green.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

peekseed supports Python 3.11+ (CI runs 3.11, 3.12, 3.13 and 3.14). Everything
needed for development is an extra; the test suite is offline and needs no
models.

```bash
git clone <the repository>
cd peekseed
python -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[test]'

# optional, same versions CI uses
python -m pip install "ruff==0.17.1" build wheel "setuptools>=77"
```

No system packages are required for the default PDF path. Optional formats are
opt-in extras (`office`, `tables`, `ocr`, `layout`/`pymupdf`); do not make them
import-time requirements of the core package.

## Branches and releases

- `main` is the release line: it only moves when a release is cut, and every
  release is a signed `vX.Y.Z` tag. Do not open feature PRs against `main`.
- `dev` is the integration branch: open every feature/fix/docs PR against
  `dev`. CI runs on pushes to `dev` and `main`, and on all pull requests.
- Cut short-lived feature branches from `dev` (`feat/...`, `fix/...`,
  `chore/...`); dependabot also targets `dev`.
- Version bumps, tags and PyPI publishing happen only in the release process
  described in [docs/release.md](docs/release.md). A released `main` stays
  frozen until the next release; urgent fixes land on `dev` first.

## Tests

The suite is deliberately offline: `tests/fixtures.py` implements fake
embedding/rerank/chat servers and writes real synthetic PDFs, so no network or
model is needed.

```bash
python -m pytest -q                  # full suite, what CI runs
python -m unittest discover -s tests -v   # equivalent stdlib entry point
python -m pytest tests/test_packaging.py -q   # fast packaging-only slice
```

Rules that the existing tests enforce and new code must keep:

- tests never open a real network connection and never require an API key;
- tests use temporary workspaces and clean up `PEEKSEED_*` environment
  variables (see `tests/fixtures.py`);
- the public contracts in [docs/contracts.md](docs/contracts.md) have tests; if
  you change a contract, change the doc and the test in the same PR;
- the packaging tests (`tests/test_packaging.py`) assert the metadata and CI
  wiring, so keep `pyproject.toml`, `MANIFEST.in` and
  `.github/workflows/ci.yml` in sync.

## Lint, licences and packaging

```bash
ruff check .                          # config lives in pyproject [tool.ruff]
python scripts/check_licenses.py      # default dependency closure: no AGPL/GPL
python -m build                       # sdist + wheel
```

CI additionally checks that `peekseed/web/index.html` is in both artifacts, that
the `peekseed` console-script entry point is in the wheel metadata, and that a
scratch install of the sdist can run `peekseed version` and read the UI file.
Do not add AGPL/GPL dependencies to the default install; optional extras with
copyleft terms (for example PyMuPDF) must stay opt-in and documented in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Pull requests

- Keep PRs small and focused: one problem, one change. Split refactors from
  behaviour changes.
- Describe **why** in the PR body, list the commands you ran and their results,
  and link the issue it fixes when there is one.
- Add or update tests for every behaviour change; a bug fix should fail before
  the fix and pass after it.
- Update the docs when a user-visible behaviour, API route, environment
  variable or workspace layout changes. [docs/api.md](docs/api.md) is the API
  contract; [CHANGELOG.md](CHANGELOG.md) is updated at release time by the
  release manager, not per PR.
- **Never commit** a workspace (`documents/`, `.peekseed/`), API keys, tokens,
  model output containing private documents, or generated build artifacts.
  `PEEKSEED_*` secrets belong in `.env` (untracked) or the environment.
- **Do not bump the version or create tags** in a normal PR. Version bumps and
  tags happen only in the release process described in
  [docs/release.md](docs/release.md).
- Do not reformat files unrelated to your change; CI lints the whole tree, and
  unrelated churn makes review harder.
- Prefer a linear, readable commit history. Squash fixup commits before asking
  for review; do not force-push someone else's branch.

## Review and merging

At least one maintainer review is required. A PR is mergeable when:

- the full test matrix (3.11–3.14), ruff, licence check and the build/sdist
  smoke job are green;
- the change is covered by tests and documented where needed;
- it does not weaken the security posture in
  [docs/security.md](docs/security.md) or add a copyleft default dependency;
- the diff stays inside the PR's stated scope.

## Licence

peekseed is MIT licensed (see [LICENSE](LICENSE)). By submitting a contribution
you agree that it is your original work and that it is licensed under the same
MIT terms ("inbound = outbound"); no CLA or copyright assignment is required.
If your contribution includes third-party code, assets or data, state its
licence and add an entry to [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

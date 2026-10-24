# Release policy

This document is the version/tag policy for peekseed and the checklist the
release manager follows. It is deliberately conservative: **no release
artifact may be produced from an unreviewed or dirty tree**, and a published
tag is never moved.

## Versioning

- Versions follow [PEP 440](https://peps.python.org/pep-0440/). The 0.1.0 line
  is released as prereleases `0.1.0rc1`, `0.1.0rc2`, ... followed by the final
  `0.1.0`. 0.2.0 is the next minor line (folder scan, OCR).
- The version has exactly two sources of truth and they must match:
  `[project].version` in `pyproject.toml` and `__version__` in
  `peekseed/__init__.py`. `tests/test_packaging.py` asserts this equality and
  the expected value on every test run.
- A version bump is a dedicated release-prep change made by the release
  manager. Feature and bug-fix PRs **do not** bump the version, create tags or
  publish (see [CONTRIBUTING.md](../CONTRIBUTING.md)).
- An `rc` is immutable once published. If a fix is needed, publish `rcN+1`;
  never edit or re-upload a release candidate's files.

## Pre-publish: `0.1.0rcN` wheels from a pinned clone

"Pre-publish" means the release candidate exists only as local/TestPyPI
artifacts while the surface is validated. For each `rcN`:

1. Choose the exact commit to release. It must be a commit that has passed the
   full CI workflow on the `main` branch (all jobs, see below), with a clean
   working tree.
2. Build from a **pinned clone** at that commit, never from a developer's dirty
   checkout:
   ```bash
   git clone <repository> peekseed-release
   cd peekseed-release
   git checkout <exact commit hash>     # detached HEAD, no local edits
   git status --porcelain               # must be empty
   python -m venv .venv && . .venv/bin/activate
   python -m pip install build wheel "setuptools>=77"
   python -m build
   ```
   Record the commit hash in the release notes. A candidate built from a tree
   that is not exactly that commit is not a release candidate.
3. Verify the artifacts as CI does (see the "CI jobs" section): the wheel and
   sdist both contain `peekseed/web/index.html`; the wheel metadata contains
   the `peekseed` console-script entry point; the sdist installs into a scratch
   venv and `peekseed version` prints the expected version.
4. Upload to **TestPyPI** only, then install from TestPyPI in a fresh venv and
   smoke-test (`peekseed version`, import `peekseed`, read the UI file). RC
   artifacts are not uploaded to PyPI.
5. Pre-publish tags are optional and private. While the repo is on the private
   mirror (before PyPI/GitHub publication), an rc commit may be tagged
   `vX.Y.ZrcN` so the deployment can pin it; such a tag is not a release
   artifact and may be re-pointed to the final chosen rc commit until the
   public release. Only the final release gets a **signed** tag at publish
   time — never publish an unsigned tag.

## Final `0.1.0`: build and tag the exact deployed commit

The final release artifacts must be built and tagged from the **exact commit
that was deployed and validated**, not from a later commit and not from the rc
artifacts:

1. Identify the deployed commit (the one running the validated 0.1.0 build).
   It must be the same commit the release manager intends to tag.
2. Bump `pyproject.toml` and `peekseed/__init__.py` to `0.1.0` in a dedicated
   release-prep change **before** the final build, get it reviewed and merged,
   and let CI pass on that exact merge commit. (Do not bump the version in an
   unrelated PR.)
3. Build from a pinned clone of that commit as in the prerelease flow.
4. Create an **annotated, signed tag only at publish time**:
   ```bash
   git tag -s v0.1.0 -m "peekseed 0.1.0"
   git verify-tag v0.1.0
   ```
   The tag must point at the commit whose artifacts were built and validated.
   Do not tag before the release is approved and the artifacts are verified.
5. Upload the final artifacts to TestPyPI first, re-run the smoke test from a
   fresh venv, then upload to PyPI (TestPyPI → PyPI order; never PyPI first).

## Never move a published tag

A published tag and its release artifacts are immutable. **Never delete, move
(force-update) or re-point a published tag**, and never re-upload files for an
existing version. If a release is defective:

- yank/withdraw the release on PyPI (if supported) and say so in
  [CHANGELOG.md](../CHANGELOG.md);
- publish a new patch/prerelease version (`0.1.0.postN`, `0.1.1`, `rcN+1`, ...)
  built from a new commit;
- keep the broken tag in history and document why it was superseded.

Signed tag only at publish: do not create `v*` tags in pull requests, during
development, or for release candidates.

## CI jobs a release depends on

The workflow in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) has
four jobs; all must be green on the exact release commit before building:

| Job | What it proves |
|---|---|
| `pytest (Python 3.11/3.12/3.13/3.14)` | The offline suite passes on every supported interpreter. |
| `ruff` | `ruff check .` passes with the `pyproject.toml` configuration. |
| `dependency licences` | `scripts/check_licenses.py` passes: no AGPL/GPL in the default dependency closure. |
| `build, artifact checks, sdist smoke` | sdist+wheel build; `peekseed/web/index.html` ships in both; the `peekseed` console script is in the wheel metadata; a scratch sdist install runs `peekseed version` and reads the UI file. |

Locally, the same gates are:

```bash
python -m pytest -q
ruff check .
python scripts/check_licenses.py
python -m build                      # artifacts checked by CI-style asserts
```

## Release checklist

Copy this into the release PR or tracking issue and tick each item.

- [ ] Release scope agreed; `CHANGELOG.md` entry written for the version.
- [ ] Version bumped in `pyproject.toml` **and** `peekseed/__init__.py`;
      `tests/test_packaging.py` passes locally.
- [ ] Full offline suite passes: `python -m pytest -q`.
- [ ] CI matrix green on 3.11, 3.12, 3.13 and 3.14 for the exact commit.
- [ ] `ruff check .` matches CI and passes.
- [ ] `python scripts/check_licenses.py` passes (no AGPL/GPL in the default
      closure); optional copyleft extras remain opt-in and documented.
- [ ] `python -m build` succeeds; artifact checks pass (UI file in wheel and
      sdist, console-script entry point in wheel metadata).
- [ ] sdist installs in a scratch venv; `peekseed version` prints the expected
      version; importing `peekseed` and opening `peekseed/web/index.html`
      works.
- [ ] Pinned clone at the exact commit; `git status --porcelain` empty; build
      hash recorded.
- [ ] TestPyPI upload and fresh-venv smoke test pass.
- [ ] Final release only: signed tag `vX.Y.Z` created at publish, verified with
      `git verify-tag`, pointing at the deployed/built commit.
- [ ] PyPI upload after TestPyPI; artifacts' hashes recorded/reproducible.
- [ ] SBOM (CycloneDX JSON) generated and attached next to `SHA256SUMS`.
- [ ] PEP 740 attestations visible on PyPI; the TestPyPI install smoke passed.
- [ ] GitHub Release published from the matching CHANGELOG section.
- [ ] Published tag is never moved; `CHANGELOG.md` and release notes published.
- [ ] Post-release: `main` remains green; next `rcN+1`/patch plan documented if
      anything was yanked.

## Release workflow (`.github/workflows/release.yml`)

A final `vX.Y.Z` tag (one without `rc`) triggers the release workflow:

1. **build** (read-only) — checks out the tag, builds sdist+wheel from that
   exact commit, runs `scripts/verify_release.py`, writes `SHA256SUMS` and a
   CycloneDX JSON SBOM into a separate `release-meta` artifact, and uploads the
   distributions as `release-dist`. The publishing jobs consume only
   `release-dist`, so PyPI receives exactly the wheel and sdist (PyPI rejects
   any non-distribution file in the upload directory).
2. **publish-testpypi** — `environment: testpypi` (manual approval), Trusted
   Publishing to TestPyPI. Install from TestPyPI in a clean venv and run the
   post-publish smoke before approving the next environment.
3. **publish-pypi** — `environment: pypi` (manual approval), Trusted Publishing
   to PyPI with PEP 740 attestations (on by default; do not disable them).
4. **github-release** — creates the GitHub Release from the tag using the
   matching CHANGELOG section, attaching the distributions, `SHA256SUMS` and the
   CycloneDX SBOM.

No repository secrets or long-lived tokens are involved: publishing and
attestations use OIDC through trusted publishers configured on TestPyPI and PyPI
(`owner`/`repository`/`workflow`/`environment`). `rc` tags are skipped by every
job, so pre-release tags stay private and unpublished.

## This repository's release setup

- True source and public repository: `https://github.com/wowyuxin/peekseed`.
  While pre-publish, the private iCloud bare mirror is the transport; the GitHub
  remote is added at publication.
- Deployment pinning happens before publication: the Mac mini installs a wheel
  built from a pinned clone at the deployed commit (`deploy/upgrade.sh`), so the
  final `0.1.0` tag points at a commit that has already been deployed and
  smoke-tested.
- `scripts/serve.sh` is a local operator helper, not part of the release
  artifact contract beyond shipping in the sdist.
- Packaging metadata is PEP 639: `license = "MIT"`, `license-files =
  ["LICENSE"]`, `[build-system] requires = ["setuptools>=77"]`.

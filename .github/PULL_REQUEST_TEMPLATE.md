## Summary

<!-- What does this PR change, and why? Link the issue it closes, if any. -->

Closes #

## Changes

<!-- Bullet list of the concrete changes. Keep this PR focused on one problem. -->

-

## Testing

<!-- Exact commands and results. The offline suite needs no models or network. -->

```bash
python -m pytest -q
ruff check .
python scripts/check_licenses.py
python -m build
```

Output / counts:

-

## Release / compatibility notes

<!-- Does this touch a documented contract, workspace schema, env var or API route?
     Add a CHANGELOG entry only if the release manager asks for one. -->

-

## Checklist

- [ ] The change is focused, and unrelated files were left alone.
- [ ] Tests were added or updated for the behaviour change (bug fixes should
      fail before the fix).
- [ ] `python -m pytest -q` passes locally (full suite).
- [ ] `ruff check .` passes with the configuration in `pyproject.toml`.
- [ ] `scripts/check_licenses.py` passes; no AGPL/GPL default dependency was added.
- [ ] Packaging is intact: `peekseed/web/index.html` still ships, and the
      console script entry point is unchanged (see `tests/test_packaging.py`).
- [ ] Documentation is updated for user-visible changes (`docs/api.md` for API
      changes, `docs/security.md` for security posture changes).
- [ ] No secrets, API keys, workspace data (`documents/`, `.peekseed/`) or
      build artifacts are included.
- [ ] No version bump, tag or publishing step in this PR (the release manager
      owns the process in `docs/release.md`).
- [ ] The change respects the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reviewer notes

<!-- Anything reviewers should look at first, known trade-offs or follow-ups. -->

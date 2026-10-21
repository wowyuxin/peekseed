#!/usr/bin/env python3
"""Check the licences of peekseed's installed default dependency closure.

Run it in an environment where peekseed's default dependencies are installed::

    python scripts/check_licenses.py

The script starts from ``[project].dependencies`` in ``pyproject.toml``, walks
the installed transitive requirements (skipping optional extras and packages
that are not installed), and fails if any package:

* declares an AGPL or GPL licence (including the GNU/Affero names), or
* declares a licence outside the allowed permissive set below, or
* declares no recognisable licence at all (so a silent licence change cannot
  slip through).

Allowed SPDX identifiers for the default closure
-------------------------------------------------
MIT, BSD-2-Clause, BSD-3-Clause, 0BSD, Apache-2.0, ISC, MPL-2.0, PSF-2.0,
Python-2.0, Zlib, CC0-1.0, Unlicense, BlueOak-1.0.0

Weak-copyleft licences such as MPL-2.0 are allowed because they do not impose
copyleft on peekseed itself. LGPL is *not* automatically allowed (it is not in
the set); add it deliberately if a dependency ever needs it. Copyleft extras
(PyMuPDF, AGPL-3.0 or commercial) are opt-in and are not part of the default
install, so they are not checked here.

The GitHub Actions ``licenses`` job runs this against a fresh
``pip install -e .`` environment. Keep the allowed set aligned with
THIRD-PARTY-NOTICES.md and docs/security.md.
"""

import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 is unsupported
    sys.exit("peekseed requires Python 3.11+ (tomllib is unavailable)")

try:
    import importlib.metadata as metadata
except ModuleNotFoundError:  # pragma: no cover - Python < 3.8 is unsupported
    sys.exit("importlib.metadata is required")


ROOT = Path(__file__).resolve().parent.parent
ALLOWED_SPDX = {
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "0BSD",
    "Apache-2.0",
    "ISC",
    "MPL-2.0",
    "PSF-2.0",
    "Python-2.0",
    "Zlib",
    "CC0-1.0",
    "Unlicense",
    "BlueOak-1.0.0",
}

# Text patterns that always fail, whatever the declared SPDX identifiers say.
DENIED_PATTERNS = (
    re.compile(r"\bAGPL(?:[-\s]?\d+(?:\.\d+)?)?\b", re.IGNORECASE),
    re.compile(r"\bGPL(?:[-\s]?\d+(?:\.\d+)?)?\b", re.IGNORECASE),
    re.compile(r"\bAffero\s+General\s+Public\s+License\b", re.IGNORECASE),
    re.compile(r"\bGNU\s+General\s+Public\s+License\b", re.IGNORECASE),
)

# License classifiers predate License-Expression; map the ones that appear in
# the default closure (or are likely to) to SPDX identifiers.
CLASSIFIER_TO_SPDX = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Python Software Foundation License 2.0": "PSF-2.0",
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
}
SPDX_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize(name):
    """PEP 503 normalisation: ``Foo_Bar`` and ``foo.bar`` become ``foo-bar``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def project_dependencies():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    return list(pyproject["project"]["dependencies"])


def requirement_is_extra(requirement):
    _, _, marker = requirement.partition(";")
    return bool(re.search(r"\bextra\s*==", marker))


def requirement_name(requirement):
    match = REQUIREMENT_NAME.match(requirement.strip())
    return match.group(0) if match else None


def dependency_closure():
    """Installed, non-extra transitive closure of the default dependencies."""
    pending = list(project_dependencies())
    seen = set()
    while pending:
        requirement = pending.pop(0)
        if requirement_is_extra(requirement):
            continue
        name = requirement_name(requirement)
        if not name:
            continue
        key = normalize(name)
        if key in seen:
            continue
        seen.add(key)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            # Marker-false requirements are not installed; that is fine.
            continue
        pending.extend(requires)
    return sorted(seen)


def license_text(distribution):
    fields = (
        distribution.metadata.get("License-Expression"),
        distribution.metadata.get("License"),
        " ".join(distribution.metadata.get_all("Classifier") or []),
    )
    return " ".join(field for field in fields if field)


def license_tokens(distribution):
    """Best-effort SPDX identifiers declared by a distribution."""
    expression = distribution.metadata.get("License-Expression")
    if expression:
        tokens = SPDX_TOKEN.findall(expression)
        return {token for token in tokens if token.upper() not in {"AND", "OR", "WITH"}}

    classifiers = distribution.metadata.get_all("Classifier") or []
    mapped = {
        CLASSIFIER_TO_SPDX[classifier]
        for classifier in classifiers
        if classifier in CLASSIFIER_TO_SPDX
    }
    if mapped:
        return mapped

    license_field = distribution.metadata.get("License") or ""
    # A short field such as "MIT" or "BSD-3-Clause AND MIT" is an expression;
    # a long one is prose, and scanning prose for SPDX ids is too fragile.
    if license_field and len(license_field) <= 80:
        tokens = SPDX_TOKEN.findall(license_field)
        return {token for token in tokens
                if token.upper() not in {"AND", "OR", "WITH", "LICENSE"}}
    return set()


def main():
    closure = dependency_closure()
    if not closure:
        sys.exit("no dependencies found; run this after `pip install -e .`")

    failures = []
    unknown = []
    rows = []

    for key in closure:
        try:
            distribution = metadata.distribution(key)
        except metadata.PackageNotFoundError:  # pragma: no cover - defensive
            continue
        name = distribution.metadata["Name"]
        version = distribution.version
        text = license_text(distribution)
        tokens = license_tokens(distribution)

        denied = [pattern.pattern for pattern in DENIED_PATTERNS
                  if pattern.search(text)]
        if denied:
            failures.append(
                "%s %s declares a copyleft licence (%s)" % (name, version, denied[0]))
            rows.append((name, version, "DENIED"))
            continue

        if not tokens:
            unknown.append("%s %s: no recognisable licence metadata" % (name, version))
            rows.append((name, version, "UNKNOWN"))
            continue

        unexpected = sorted(token for token in tokens if token not in ALLOWED_SPDX)
        if unexpected:
            failures.append(
                "%s %s declares %s, not in the allowed set"
                % (name, version, ", ".join(unexpected)))
            rows.append((name, version, "NOT ALLOWED: " + ", ".join(unexpected)))
            continue

        rows.append((name, version, ", ".join(sorted(tokens))))

    # peekseed's own metadata should be an SPDX MIT expression as well.
    try:
        own = metadata.distribution("peekseed")
        expression = own.metadata.get("License-Expression")
        if expression != "MIT":
            failures.append(
                "peekseed %s: expected License-Expression 'MIT', found %r"
                % (own.version, expression))
    except metadata.PackageNotFoundError:
        print("note: peekseed itself is not installed in this environment")

    width = max(len(name) for name, _, _ in rows) if rows else 4
    print("%-*s %-10s %s" % (width, "package", "version", "licence"))
    print("%-*s %-10s %s" % (width, "-" * width, "-" * 10, "-" * 40))
    for name, version, licence in rows:
        print("%-*s %-10s %s" % (width, name, version, licence))
    print()
    print("checked %d package(s) in the default dependency closure" % len(rows))

    for message in unknown:
        print("UNKNOWN LICENCE: %s" % message, file=sys.stderr)
        failures.append(message)
    for message in failures:
        print("LICENCE CHECK FAILED: %s" % message, file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

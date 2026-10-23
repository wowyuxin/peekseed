#!/usr/bin/env python3
"""Verify built sdist/wheel artifacts against the release contract.

Usage:
    python scripts/verify_release.py dist/ --expected-version 0.1.0

The checks are stdlib-only and offline, so the same script runs locally before
tagging and in the release workflow before uploading anything.

It verifies:
- exactly one wheel and one sdist, with the same version;
- wheel contents: the web UI, console entry point, PEP 639 metadata and the
  four Project-URL entries;
- sdist contents: docs, tests, scripts, community files and the web UI;
- SHA256SUMS (when present) matches the distributions.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import pathlib
import tarfile
import zipfile

PROJECT_URL_KEYS = ("Homepage", "Repository", "Issues", "Changelog")
ENTRY_POINT = "peekseed = peekseed.__main__:main"
REQUIRED_SDIST = (
    "peekseed/web/index.html",
    "docs/api.md",
    "docs/install.md",
    "docs/deploy.md",
    "docs/local-models.md",
    "docs/release.md",
    "docs/security.md",
    "docs/workspace.md",
    "docs/import.md",
    "tests/test_packaging.py",
    "scripts/serve.sh",
    "scripts/check_licenses.py",
    "scripts/verify_release.py",
    "scripts/changelog_section.py",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "MANIFEST.in",
    "THIRD-PARTY-NOTICES.md",
    ".env.example",
)


def fail(problems, message):
    problems.append(message)
    print("FAIL  " + message)


def ok(message):
    print("ok    " + message)


def metadata_of(text):
    return email.parser.Parser().parsestr(text)


def check_wheel(wheel, expected, problems):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not any(n.endswith("peekseed/web/index.html") for n in names):
            fail(problems, "wheel is missing peekseed/web/index.html")
        else:
            ok("wheel contains the web UI")

        entry_points = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        if not entry_points:
            fail(problems, "wheel has no entry_points.txt")
        else:
            text = archive.read(entry_points[0]).decode("utf-8")
            if ENTRY_POINT not in text:
                fail(problems, "wheel console script entry point is missing")
            else:
                ok("wheel console script entry point present")

        meta_files = [n for n in names if n.endswith(".dist-info/METADATA")]
        if not meta_files:
            fail(problems, "wheel has no METADATA")
            return None
        meta = metadata_of(archive.read(meta_files[0]).decode("utf-8"))

    name = meta.get("Name", "")
    version = meta.get("Version", "")
    if name.lower() != "peekseed":
        fail(problems, "wheel name is %r" % name)
    if expected and version != expected:
        fail(problems, "wheel version %r != expected %r" % (version, expected))
    else:
        ok("wheel version %s" % version)

    if meta.get("License-Expression") != "MIT":
        fail(problems, "wheel missing PEP 639 License-Expression: MIT")
    else:
        ok("wheel carries PEP 639 license metadata")
    if meta.get("Requires-Python") != ">=3.11":
        fail(problems, "wheel Requires-Python is %r" % meta.get("Requires-Python"))
    else:
        ok("wheel Requires-Python is >=3.11")
    if any(c.startswith("License ::") for c in meta.get_all("Classifier") or []):
        fail(problems, "wheel still carries legacy license classifiers")
    else:
        ok("wheel has no legacy license classifier")

    project_urls = meta.get_all("Project-URL") or []
    seen = {}
    for line in project_urls:
        label, _, url = line.partition(",")
        seen[label.strip()] = url.strip()
    missing = [key for key in PROJECT_URL_KEYS if key not in seen]
    if missing:
        fail(problems, "wheel Project-URLs missing: %r" % missing)
    elif not all(url.startswith("https://") for url in seen.values()):
        fail(problems, "wheel Project-URL is not https: %r" % seen)
    else:
        ok("wheel Project-URLs: " + ", ".join(PROJECT_URL_KEYS))
    return version


def check_sdist(sdist, expected, problems):
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        pkg_info = [n for n in names if n.endswith("/PKG-INFO") or n == "PKG-INFO"]
        if not pkg_info:
            fail(problems, "sdist has no PKG-INFO")
            meta = None
        else:
            meta = metadata_of(archive.extractfile(pkg_info[0]).read().decode("utf-8"))

    missing = [item for item in REQUIRED_SDIST
               if not any(n == item or n.endswith("/" + item) for n in names)]
    if missing:
        fail(problems, "sdist is missing: %r" % missing)
    else:
        ok("sdist contains docs, tests, scripts and community files")

    version = None
    if meta is not None:
        version = meta.get("Version", "")
        if expected and version != expected:
            fail(problems, "sdist version %r != expected %r" % (version, expected))
        else:
            ok("sdist version %s" % version)
    return version


def check_checksums(dist, distributions, problems):
    sums = dist / "SHA256SUMS"
    if not sums.is_file():
        return  # optional before the workflow writes it
    wanted = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, filename = line.partition("  ")
        wanted[filename] = digest
    for path in distributions:
        if path.name not in wanted:
            fail(problems, "SHA256SUMS is missing %s" % path.name)
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != wanted[path.name]:
            fail(problems, "SHA256SUMS mismatch for %s" % path.name)
        else:
            ok("sha256 matches for %s" % path.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", help="directory containing the built wheel and sdist")
    parser.add_argument("--expected-version", default=None,
                        help="version the artifacts must carry (e.g. 0.1.0)")
    args = parser.parse_args(argv)

    dist = pathlib.Path(args.dist)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    problems = []
    if len(wheels) != 1:
        fail(problems, "expected exactly one wheel in %s, found %d" % (dist, len(wheels)))
    if len(sdists) != 1:
        fail(problems, "expected exactly one sdist in %s, found %d" % (dist, len(sdists)))
    if problems:
        print("\nrelease verification FAILED (%d problem(s))" % len(problems))
        return 1

    wheel_version = check_wheel(wheels[0], args.expected_version, problems)
    sdist_version = check_sdist(sdists[0], args.expected_version, problems)
    if wheel_version and sdist_version and wheel_version != sdist_version:
        fail(problems, "wheel and sdist versions differ: %s vs %s"
             % (wheel_version, sdist_version))
    check_checksums(dist, [wheels[0], sdists[0]], problems)

    if problems:
        print("\nrelease verification FAILED (%d problem(s))" % len(problems))
        return 1
    print("\nrelease verification PASS (%s, %s)" % (wheels[0].name, sdists[0].name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

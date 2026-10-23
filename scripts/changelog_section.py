#!/usr/bin/env python3
"""Extract a CHANGELOG release section for the release workflow.

Usage:
    python scripts/changelog_section.py v0.1.0
    python scripts/changelog_section.py 0.1.0 --file CHANGELOG.md
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def normalise(version: str) -> str:
    """Accept a tag name (`v0.1.0`) or a bare version (`0.1.0`)."""
    version = version.strip()
    return version[1:] if version.startswith("v") else version


def section_for(text: str, version: str) -> str:
    """Return the CHANGELOG section for ``version`` (heading included)."""
    version = normalise(version)
    heading = re.compile(r"^##\s+%s(?:\s+-\s+.*)?$" % re.escape(version), re.M)
    match = heading.search(text)
    if match is None:
        raise SystemExit("CHANGELOG has no section for %s" % version)
    rest = text[match.end():]
    next_heading = re.search(r"^##\s+", rest, re.M)
    body = rest[:next_heading.start()] if next_heading else rest
    return (text[match.start():match.end()] + body).strip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version or tag (leading 'v' allowed)")
    parser.add_argument("--file", default=str(ROOT / "CHANGELOG.md"))
    args = parser.parse_args(argv)
    text = pathlib.Path(args.file).read_text(encoding="utf-8")
    sys.stdout.write(section_for(text, args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

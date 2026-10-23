"""Packaging contract tests: metadata, sdist manifest and CI wiring.

These tests are pure filesystem/tomllib checks: no build, no network, no
installed distribution. The slower artifact-content and sdist-install checks run
in the CI ``build`` job (see .github/workflows/ci.yml).
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_pyproject():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pyproject = load_pyproject()
        cls.project = cls.pyproject["project"]

    def test_project_identity(self):
        self.assertEqual(self.project["name"], "peekseed")
        self.assertEqual(self.project["version"], "0.1.0rc4")
        self.assertEqual(self.project["requires-python"], ">=3.11")
        self.assertEqual(self.project["description"],
                         "Local-first AI research framework over your own documents")

    def test_version_agrees_with_package(self):
        from peekseed import __version__

        self.assertEqual(__version__, self.project["version"])
        self.assertEqual(__version__, "0.1.0rc4")

    def test_pep639_license_metadata(self):
        # PEP 639: an SPDX expression plus explicit license files, never the
        # legacy ``license = {text = ...}`` table or license classifiers.
        self.assertEqual(self.project["license"], "MIT")
        self.assertEqual(self.project["license-files"], ["LICENSE"])
        classifiers = self.project.get("classifiers", [])
        self.assertFalse([c for c in classifiers if c.startswith("License ::")],
                         "legacy license classifier must not be declared")
        self.assertTrue((ROOT / "LICENSE").is_file())

    def test_build_system_supports_pep639(self):
        requires = self.pyproject["build-system"]["requires"]
        self.assertEqual(self.pyproject["build-system"]["build-backend"],
                         "setuptools.build_meta")
        setuptools = [req for req in requires
                      if req.replace(" ", "").lower().startswith("setuptools")]
        self.assertTrue(setuptools, "setuptools must be a build requirement")
        match = re.search(r">=\s*(\d+)", setuptools[0])
        self.assertIsNotNone(match, "setuptools version floor must be explicit")
        self.assertGreaterEqual(int(match.group(1)), 77,
                                "PEP 639 needs setuptools>=77")

    def test_classifiers_cover_supported_interpreters(self):
        classifiers = self.project["classifiers"]
        self.assertIn("Programming Language :: Python :: 3", classifiers)
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn("Programming Language :: Python :: " + version,
                          classifiers)

    def test_console_script_entry_point(self):
        self.assertEqual(self.project["scripts"],
                         {"peekseed": "peekseed.__main__:main"})

    def test_optional_extras_present(self):
        extras = self.project["optional-dependencies"]
        for extra in ("office", "tables", "ocr", "layout", "pymupdf", "test"):
            self.assertIn(extra, extras)
        self.assertTrue(any(dep.startswith("pytest") for dep in extras["test"]))

    def test_package_data_ships_the_web_ui(self):
        package_data = self.pyproject["tool"]["setuptools"]["package-data"]
        self.assertIn("web/*", package_data["peekseed"])

    def test_web_ui_file_exists_and_is_nontrivial(self):
        ui = ROOT / "peekseed" / "web" / "index.html"
        self.assertTrue(ui.is_file(), ui)
        text = ui.read_text(encoding="utf-8")
        self.assertGreater(len(text), 10_000, "web UI looks truncated")
        self.assertIn("<html", text.lower())
        self.assertIn("peekseed", text.lower())

    def test_manifest_in_ships_docs_tests_and_ui(self):
        manifest = ROOT / "MANIFEST.in"
        self.assertTrue(manifest.is_file(), manifest)
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("recursive-include docs *.md", text)
        self.assertIn("recursive-include tests *.py", text)
        self.assertIn("recursive-include peekseed/web *", text)
        for filename in ("LICENSE", "README.md", "pyproject.toml",
                         "THIRD-PARTY-NOTICES.md", ".env.example"):
            self.assertIn("include " + filename, text)

    def test_public_install_docs_exist_and_are_linked(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("docs/install.md", "docs/deploy.md",
                     "docs/local-models.md"):
            path = ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertIn("](%s)" % name, readme,
                          "README must link %s" % name)

    def test_project_urls_point_at_the_public_repo(self):
        urls = self.project.get("urls")
        self.assertIsNotNone(urls, "[project.urls] is missing")
        for key in ("Homepage", "Repository", "Issues", "Changelog"):
            self.assertIn(key, urls)
            self.assertTrue(urls[key].startswith("https://github.com/"),
                            "%s must be an https GitHub URL" % key)
        self.assertEqual(urls["Homepage"], urls["Repository"])

    def test_ci_workflow_exists_and_covers_supported_python(self):
        workflow = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file(), workflow)
        text = workflow.read_text(encoding="utf-8")
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(version, text)
        self.assertIn("pytest -q", text)
        self.assertIn("ruff check .", text)
        self.assertIn("scripts/check_licenses.py", text)
        self.assertIn("peekseed/web/index.html", text)
        self.assertIn("peekseed version", text)

    def test_release_policy_is_documented(self):
        release = ROOT / "docs" / "release.md"
        self.assertTrue(release.is_file(), release)
        text = release.read_text(encoding="utf-8").lower()
        for phrase in ("0.1.0rc", "v0.1.0", "testpypi", "never move",
                       "signed tag"):
            self.assertIn(phrase, text)

    def test_license_checker_script_exists(self):
        script = ROOT / "scripts" / "check_licenses.py"
        self.assertTrue(script.is_file(), script)
        text = script.read_text(encoding="utf-8")
        self.assertIn("AGPL", text)
        self.assertIn("GPL", text)

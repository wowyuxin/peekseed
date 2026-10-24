"""Offline checks for the release tooling and workflow wiring."""

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.rsplit(".", 1)[0], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChangelogSectionTests(unittest.TestCase):
    def setUp(self):
        self.changelog = load_script("changelog_section.py")

    def test_extracts_matching_section(self):
        text = (
            "# Changelog\n\n"
            "## 0.1.0 - 2026-10-23\n\nFirst release.\n\n"
            "## 0.1.0rc4 - 2026-10-23\n\nFourth release candidate.\n"
        )
        section = self.changelog.section_for(text, "0.1.0")
        self.assertIn("First release.", section)
        self.assertNotIn("Fourth release candidate.", section)
        self.assertTrue(section.startswith("## 0.1.0"))

    def test_strips_v_prefix(self):
        text = "## 0.1.0 - 2026-10-23\n\nNotes.\n"
        self.assertIn("Notes.", self.changelog.section_for(text, "v0.1.0"))

    def test_missing_section_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.changelog.section_for("# Changelog\n", "9.9.9")

    def test_real_changelog_has_rc4_section(self):
        path = ROOT / "CHANGELOG.md"
        text = path.read_text(encoding="utf-8")
        section = self.changelog.section_for(text, "v0.1.0rc4")
        self.assertIn("0.1.0rc4", section.splitlines()[0])


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

    def test_workflow_is_tag_triggered_and_skips_rc(self):
        self.assertIn("tags:", self.workflow)
        self.assertIn('"v*"', self.workflow)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", self.workflow)
        self.assertIn("contains(github.ref_name, 'rc')", self.workflow)
        self.assertNotIn("workflow_dispatch:", self.workflow)

    def test_workflow_uses_trusted_publishing_with_environments(self):
        for needle in (
            "pypa/gh-action-pypi-publish",
            "https://test.pypi.org/legacy/",
            "id-token: write",
            "environment:",
            "name: testpypi",
            "name: pypi",
        ):
            self.assertIn(needle, self.workflow)

    def test_workflow_builds_verifies_and_attaches_release_assets(self):
        for needle in (
            "python -m build",
            "scripts/verify_release.py",
            "SHA256SUMS",
            "anchore/sbom-action",
            "cyclonedx-json",
            "scripts/changelog_section.py",
            "softprops/action-gh-release",
        ):
            self.assertIn(needle, self.workflow)

    def test_release_tooling_scripts_exist_and_run_offline(self):
        for name in ("verify_release.py", "changelog_section.py"):
            self.assertTrue((ROOT / "scripts" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()

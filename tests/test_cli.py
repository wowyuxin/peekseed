import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path


@contextlib.contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def clean_peekseed_env():
    """Remove PEEKSEED_* so a test .env is the only source, then restore."""
    saved = {key: os.environ.pop(key) for key in list(os.environ)
             if key.startswith("PEEKSEED_")}
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("PEEKSEED_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)


class CliTests(unittest.TestCase):
    def test_version(self):
        from peekseed.__main__ import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["version"]), 0)
        self.assertIn("peekseed 0.1.0", buffer.getvalue())

    def test_env_file_provides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            project = Path(tmp)
            (project / ".env").write_text(
                "PEEKSEED_DATA=./from-env\nPEEKSEED_PORT=9123\n"
                "# a comment\nexport PEEKSEED_HOST=0.0.0.0\n")
            with working_directory(project):
                from peekseed import config
                config.reset_cache()
                config.load_env_file()
                try:
                    self.assertEqual(config.data_root(),
                                     (project / "from-env").resolve())
                    self.assertEqual(config.port(), 9123)
                    self.assertEqual(config.host(), "0.0.0.0")
                    # a real environment variable wins over the file
                    os.environ["PEEKSEED_PORT"] = "9999"
                    self.assertEqual(config.port(), 9999)
                finally:
                    config.reset_cache()

    def test_init_creates_the_data_root(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            project = Path(tmp)
            (project / ".env").write_text("PEEKSEED_DATA=./data\n")
            with working_directory(project):
                from peekseed.__main__ import main
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(main(["init"]), 0)
                # PEEKSEED_DATA pins the derived root; the workspace tree still
                # lands in ./workspace (documents are never derived data).
                self.assertTrue((project / "data" / "config" / "links.json").is_file())
                self.assertTrue((project / "workspace" / "documents").is_dir())
                self.assertIn("workspace:", buffer.getvalue())

    def test_init_creates_the_workspace_and_gitignore_guard(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            project = Path(tmp)
            with working_directory(project):
                from peekseed.__main__ import main
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(main(["init"]), 0)
                self.assertTrue((project / "workspace" / "documents").is_dir())
                self.assertTrue((project / "workspace" / ".peekseed" / "manifest.json").is_file())
                self.assertTrue(
                    (project / "workspace" / ".peekseed" / "config" / "links.json").is_file())
                self.assertIn("/workspace/",
                              (project / ".gitignore").read_text(encoding="utf-8"))

    def test_workspace_path_prints_the_absolute_root(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            project = Path(tmp)
            with working_directory(project):
                from peekseed.__main__ import main
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(main(["workspace", "path"]), 0)
                self.assertEqual(buffer.getvalue().strip(),
                                 str((project / "workspace").resolve()))

    def test_migrate_is_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            project = Path(tmp)
            dataset_dir = project / "workspace" / ".peekseed" / "datasets" / "docs"
            original = dataset_dir / "original"
            original.mkdir(parents=True)
            (original / "a.pdf").write_bytes(b"%PDF-1.4\nfake")
            (dataset_dir / "dataset.json").write_text(
                '{"version": 1, "id": "docs", "name": "Docs", '
                '"created_at": "2026-01-01T00:00:00Z", '
                '"sources": [{"name": "a.pdf", "path": "original/a.pdf", '
                '"bytes": 12, "pages": 1}]}', encoding="utf-8")
            with working_directory(project):
                from peekseed.__main__ import main
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(main(["migrate"]), 0)
                text = buffer.getvalue()
                self.assertIn("dry-run (nothing written)", text)
                self.assertIn("schema 1 -> 2", text)
                self.assertIn('"version": 1', (dataset_dir / "dataset.json").read_text())


if __name__ == "__main__":
    unittest.main()

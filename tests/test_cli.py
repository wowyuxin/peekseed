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


@contextlib.contextmanager
def temp_layout():
    """A temporary home + shared folder with both required env vars set."""
    with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
        base = Path(tmp)
        home = base / "home"
        shared = base / "shared"
        home.mkdir()
        shared.mkdir()
        os.environ["PEEKSEED_HOME"] = str(home)
        os.environ["PEEKSEED_SHARED_DIR"] = str(shared)
        from peekseed import config
        config.reset_cache()
        try:
            yield base, home, shared
        finally:
            config.reset_cache()


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
            (project / "home").mkdir()
            (project / "shared").mkdir()
            (project / ".env").write_text(
                "PEEKSEED_HOME=%s\nPEEKSEED_SHARED_DIR=%s\n"
                "PEEKSEED_PORT=9123\n"
                "# a comment\nexport PEEKSEED_HOST=0.0.0.0\n"
                % (project / "home", project / "shared"))
            with working_directory(project):
                from peekseed import config
                config.reset_cache()
                config.load_env_file()
                try:
                    self.assertEqual(config.data_root(),
                                     (project / "home").resolve())
                    self.assertEqual(config.port(), 9123)
                    self.assertEqual(config.host(), "0.0.0.0")
                    # a real environment variable wins over the file
                    os.environ["PEEKSEED_PORT"] = "9999"
                    self.assertEqual(config.port(), 9999)
                finally:
                    config.reset_cache()

    def test_init_creates_the_home_and_shared_tree(self):
        with temp_layout() as (base, home, shared):
            from peekseed.__main__ import main
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["init"]), 0)
            self.assertTrue((home / "config" / "links.json").is_file())
            self.assertTrue((home / "datasets").is_dir())
            self.assertTrue((shared / "sources").is_dir())
            self.assertTrue((shared / "peels").is_dir())
            self.assertIn("home:", buffer.getvalue())
            self.assertFalse((home / "documents").exists())

    def test_missing_settings_exit_2_and_version_still_works(self):
        with clean_peekseed_env():
            from peekseed.__main__ import main
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                self.assertEqual(main(["status"]), 2)
            self.assertIn("PEEKSEED_HOME", buffer.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["version"]), 0)
            self.assertIn("peekseed 0.1.0", out.getvalue())

    def test_removed_commands_are_not_in_help(self):
        from peekseed.__main__ import build_parser
        text = build_parser().format_help()
        self.assertNotIn("scan", text)
        self.assertNotIn("workspace", text)
        for command in ("init", "status", "doctor", "reindex", "migrate",
                        "serve", "index", "version"):
            self.assertIn(command, text)

    def test_help_has_no_import_command(self):
        from peekseed.__main__ import build_parser
        parser = build_parser()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            parser.print_help()
        text = buffer.getvalue()
        self.assertNotIn("import", text)
        self.assertIn("index", text)

    def test_index_stores_without_extraction(self):
        from tests.fixtures import make_pdf
        with temp_layout() as (base, home, shared):
            make_pdf(base / "a.pdf", pages=["Widget page."])
            from peekseed.__main__ import main
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["index", "--name", "My docs",
                                       "--file", str(base / "a.pdf")]), 0)
            text = buffer.getvalue()
            self.assertIn("status=uploaded", text)
            self.assertIn("peelfeed", text)
            dataset_dir = home / "datasets" / "my-docs"
            self.assertTrue((dataset_dir / "dataset.json").is_file())
            self.assertFalse((dataset_dir / "original").exists())
            stored = list((shared / "sources").rglob("*.pdf"))
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].name, "a.pdf")
            self.assertFalse((dataset_dir / "chunks.jsonl").exists())

    def test_migrate_is_dry_run_by_default(self):
        with temp_layout() as (base, home, shared):
            dataset_dir = home / "datasets" / "docs"
            original = dataset_dir / "original"
            original.mkdir(parents=True)
            (original / "a.pdf").write_bytes(b"%PDF-1.4\nfake")
            (dataset_dir / "dataset.json").write_text(
                '{"version": 1, "id": "docs", "name": "Docs", '
                '"created_at": "2026-01-01T00:00:00Z", '
                '"sources": [{"name": "a.pdf", "path": "original/a.pdf", '
                '"bytes": 12, "pages": 1}]}', encoding="utf-8")
            from peekseed.__main__ import main
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["migrate"]), 0)
            text = buffer.getvalue()
            self.assertIn("dry-run (nothing written)", text)
            self.assertIn("schema 1 -> 4", text)
            self.assertIn('"version": 1',
                          (dataset_dir / "dataset.json").read_text())


if __name__ == "__main__":
    unittest.main()

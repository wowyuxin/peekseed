"""Tests for the home/shared layout and maintenance commands.

Every test runs with its own temporary home and shared folder, with every
``PEEKSEED_*`` variable removed and ``config``'s path cache reset, so a real
deployment can never be touched.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from peekseed import config, contracts, workspace


@contextlib.contextmanager
def clean_peekseed_env():
    """Remove every PEEKSEED_* variable, then restore the process environment."""
    saved = {key: os.environ.pop(key) for key in list(os.environ)
             if key.startswith("PEEKSEED_")}
    config.reset_cache()
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("PEEKSEED_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)
        config.reset_cache()


@contextlib.contextmanager
def temp_layout():
    """A temporary home + shared folder with both required env vars set."""
    with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
        home = Path(tmp) / "home"
        shared = Path(tmp) / "shared"
        home.mkdir()
        shared.mkdir()
        os.environ["PEEKSEED_HOME"] = str(home)
        os.environ["PEEKSEED_SHARED_DIR"] = str(shared)
        config.reset_cache()
        try:
            yield home, shared
        finally:
            config.reset_cache()


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


class ConfigPathsTests(unittest.TestCase):
    def test_missing_home_raises(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            os.environ["PEEKSEED_SHARED_DIR"] = tmp
            with self.assertRaises(config.ConfigError) as info:
                config.paths()
            self.assertIn("PEEKSEED_HOME", str(info.exception))

    def test_missing_shared_raises(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            os.environ["PEEKSEED_HOME"] = tmp
            with self.assertRaises(config.ConfigError) as info:
                config.paths()
            self.assertIn("PEEKSEED_SHARED_DIR", str(info.exception))

    def test_relative_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            os.environ["PEEKSEED_HOME"] = "relative/home"
            os.environ["PEEKSEED_SHARED_DIR"] = tmp
            with self.assertRaises(config.ConfigError) as info:
                config.paths()
            self.assertIn("absolute", str(info.exception))

    def test_missing_directory_raises(self):
        with temp_layout() as (home, shared):
            os.environ["PEEKSEED_HOME"] = str(home / "missing")
            config.reset_cache()
            with self.assertRaises(config.ConfigError) as info:
                config.paths()
            self.assertIn("does not exist", str(info.exception))

    def test_paths_shape(self):
        with temp_layout() as (home, shared):
            paths = config.ensure()
            self.assertEqual(paths.root, home.resolve())
            self.assertEqual(paths.shared_dir, shared.resolve())
            self.assertEqual(paths.sources_dir, shared.resolve() / "sources")
            self.assertEqual(paths.peels_dir, shared.resolve() / "peels")
            self.assertEqual(set(paths.as_dict()), {
                "root", "shared_dir", "sources_dir", "peels_dir",
                "config_dir", "links_file", "settings_file", "datasets_dir",
                "history_dir", "jobs_dir", "uploads_dir",
            })
            for name in ("config_dir", "datasets_dir", "history_dir",
                         "jobs_dir", "uploads_dir", "sources_dir", "peels_dir"):
                self.assertTrue(getattr(paths, name).is_dir(), name)

    def test_cache_follows_the_environment(self):
        with temp_layout() as (home, shared):
            first = config.paths()
            other = home.parent / "other-home"
            other.mkdir()
            os.environ["PEEKSEED_HOME"] = str(other)
            config.reset_cache()
            second = config.paths()
            self.assertNotEqual(first.root, second.root)
            self.assertEqual(second.root, other.resolve())


class InitTests(unittest.TestCase):
    def test_init_creates_the_tree_and_is_idempotent(self):
        with temp_layout() as (home, shared):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                report = workspace.init()
            self.assertIn("home:", buffer.getvalue())
            self.assertEqual(report["home"], str(home.resolve()))
            self.assertEqual(report["shared_dir"], str(shared.resolve()))
            for directory in (home / "config", home / "datasets",
                              home / "history", home / "jobs", home / "uploads",
                              shared / "sources", shared / "peels"):
                self.assertTrue(directory.is_dir(), directory)
                self.assertEqual(_mode(directory), 0o700, directory)
            self.assertEqual(_mode(home), 0o700)
            self.assertEqual(_mode(shared), 0o700)
            links = home / "config" / "links.json"
            self.assertTrue(links.is_file())
            self.assertEqual(_mode(links), 0o600)
            self.assertEqual(json.loads(links.read_text()),
                             {"version": 1, "links": []})
            # Idempotent: an existing link store is never overwritten.
            links.write_text('{"version": 1, "links": [{"id": "keep"}]}')
            with contextlib.redirect_stdout(io.StringIO()):
                again = workspace.init()
            self.assertEqual(again["home"], str(home.resolve()))
            self.assertEqual(json.loads(links.read_text())["links"][0]["id"], "keep")

    def test_init_does_not_touch_other_files(self):
        with temp_layout() as (home, shared):
            marker = home / "datasets" / "keep.txt"
            marker.parent.mkdir(parents=True)
            marker.write_text("user data")
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            self.assertEqual(marker.read_text(), "user data")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_a_clean_layout(self):
        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            report = workspace.doctor()
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["paths"]["root"], str(home.resolve()))
            self.assertEqual(report["paths"]["shared_dir"], str(shared.resolve()))
            self.assertTrue(report["links"]["ok"])

    def test_doctor_detects_missing_links_and_folders(self):
        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            (home / "config" / "links.json").unlink()
            shutil.rmtree(str(shared / "peels"))
            report = workspace.doctor()
            joined = "\n".join(report["issues"])
            self.assertIn("model link store is missing", joined)
            self.assertIn("peels folder is missing", joined)

    def test_doctor_detects_world_writable_folder(self):
        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            os.chmod(str(home), 0o775)
            try:
                report = workspace.doctor()
            finally:
                os.chmod(str(home), 0o700)
            self.assertTrue(any("group- or other-writable" in issue
                                for issue in report["issues"]))

    def test_doctor_detects_corrupt_json_and_legacy_schemas(self):
        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            dataset_dir = home / "datasets" / "legacy"
            dataset_dir.mkdir()
            (dataset_dir / "dataset.json").write_text(json.dumps({
                "version": 1, "id": "legacy", "name": "Legacy",
                "created_at": "2020-01-01T00:00:00Z",
                "sources": [{"name": "a.pdf"}],
            }))
            report = workspace.doctor()
            joined = "\n".join(report["issues"])
            self.assertIn("dataset 'legacy' uses schema 1", joined)
            self.assertIn("peekseed migrate", joined)
            for issue in report["issues"]:
                self.assertNotIn(str(home), issue)

    def test_doctor_and_status_never_write(self):
        def snapshot(path):
            return sorted(str(item.relative_to(path)) for item in path.rglob("*"))

        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            before = snapshot(home)
            workspace.doctor()
            workspace.status()
            self.assertEqual(snapshot(home), before)


class StatusTests(unittest.TestCase):
    def _write_dataset(self, home, dataset="cxl", status="chunked", chunks=2):
        dataset_dir = home / "datasets" / dataset
        dataset_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": contracts.DATASET_SCHEMA,
            "id": dataset,
            "name": "CXL docs",
            "created_at": "2020-01-01T00:00:00Z",
            "sources": [{"name": "a.pdf",
                         "path": "sources/" + "a" * 64 + "/a.pdf",
                         "sha256": "a" * 64, "bytes": 10, "pages": 1,
                         "extractor": "pypdf@1.0", "status": "chunked"}],
            "chunker": contracts.chunker_label(),
            "extractor": "pypdf@1.0",
            "status": status,
        }
        contracts.write_json_atomic(dataset_dir / "dataset.json",
                                    contracts.validate_dataset(data))
        (dataset_dir / "chunks.jsonl").write_text(
            "\n".join(json.dumps({"id": "x%d" % index}) for index in range(chunks))
            + "\n")
        return dataset_dir

    def test_status_shape(self):
        with temp_layout() as (home, shared):
            self._write_dataset(home)
            report = workspace.status()
            self.assertEqual(set(report["paths"]), {
                "root", "shared_dir", "sources_dir", "peels_dir",
                "config_dir", "links_file", "settings_file", "datasets_dir",
                "history_dir", "jobs_dir", "uploads_dir",
            })
            collection = report["collections"]["cxl"]
            self.assertEqual(collection["status"], "chunked")
            self.assertEqual(collection["sources"], 1)
            self.assertEqual(collection["chunks"], 2)
            self.assertEqual(report["counts"]["collections"], 1)
            self.assertEqual(report["counts"]["chunks"], 2)
            self.assertIn("links", report)
            self.assertIn("peelfeed", report)
            self.assertNotIn("last_scan", report)
            self.assertNotIn("manifest", report)

    def test_status_derives_a_collection_without_a_status_field(self):
        with temp_layout() as (home, shared):
            dataset_dir = self._write_dataset(home)
            data = json.loads((dataset_dir / "dataset.json").read_text())
            data.pop("status")
            contracts.write_json_atomic(dataset_dir / "dataset.json",
                                        contracts.validate_dataset(data))
            report = workspace.status()
            self.assertEqual(report["collections"]["cxl"]["status"], "not_indexed")


class PeelfeedReportTests(unittest.TestCase):
    def test_peelfeed_config_points_at_the_home_and_shared_peels(self):
        with temp_layout() as (home, shared):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            report = workspace.doctor()
            cfg = report["peelfeed"]["config"]
            self.assertEqual(cfg["path"],
                             str(home.resolve() / "config" / "peelfeed.json"))
            self.assertFalse(cfg["present"])
            self.assertEqual(cfg["root"], str(shared.resolve() / "peels"))


if __name__ == "__main__":
    unittest.main()

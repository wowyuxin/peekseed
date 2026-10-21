"""Tests for the workspace layout, resolution and maintenance commands.

Every test runs in its own temporary directory (or with explicit paths), with
``PEEKSEED_*`` variables removed and ``config``'s path cache reset, so a real
user workspace or data root can never be touched.
"""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from peekseed import config, contracts, workspace


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
    """Remove every PEEKSEED_* variable, then restore the process environment."""
    saved = {key: os.environ.pop(key) for key in list(os.environ)
             if key.startswith("PEEKSEED_")}
    workspace._WARNED.clear()
    config.reset_cache()
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("PEEKSEED_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)
        workspace._WARNED.clear()
        config.reset_cache()


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


class ResolutionTests(unittest.TestCase):
    def test_explicit_argument_wins(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            explicit = Path(tmp) / "chosen"
            os.environ["PEEKSEED_WORKSPACE"] = str(Path(tmp) / "from-env")
            ws = workspace.resolve(explicit)
            self.assertEqual(ws.root, explicit.resolve())
            self.assertEqual(ws.derived_dir, explicit.resolve() / ".peekseed")
            self.assertEqual(ws.documents_dir, explicit.resolve() / "documents")

    def test_environment_workspace_is_used(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            target = Path(tmp) / "from-env"
            os.environ["PEEKSEED_WORKSPACE"] = str(target)
            self.assertEqual(workspace.resolve().root, target.resolve())

    def test_default_is_dot_workspace_in_a_writable_cwd(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                self.assertEqual(workspace.resolve().root,
                                 (Path(tmp) / "workspace").resolve())

    def test_readonly_cwd_falls_back_to_home_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            readonly = Path(tmp) / "readonly"
            readonly.mkdir()
            os.chmod(str(readonly), 0o555)
            try:
                with working_directory(readonly):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        resolved = workspace.resolve()
                    self.assertEqual(resolved.root,
                                     (Path.home() / "peekseed").resolve())
                    self.assertIn("not writable", buffer.getvalue())
            finally:
                os.chmod(str(readonly), 0o755)

    def test_data_env_overrides_only_the_derived_root(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            root = Path(tmp) / "ws"
            data = Path(tmp) / "elsewhere"
            os.environ["PEEKSEED_DATA"] = str(data)
            ws = workspace.resolve(str(root))
            self.assertEqual(ws.root, root.resolve())
            self.assertEqual(ws.derived_dir, data.resolve())
            self.assertEqual(ws.manifest_file, data.resolve() / "manifest.json")

    def test_legacy_data_root_is_adopted_with_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            legacy = Path(tmp) / "data"
            (legacy / "config").mkdir(parents=True)
            (legacy / "datasets").mkdir()
            with working_directory(tmp):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    ws = workspace.resolve()
                self.assertEqual(ws.root, (Path(tmp) / "workspace").resolve())
                self.assertEqual(ws.derived_dir, legacy.resolve())
                self.assertIn("legacy data root", buffer.getvalue())

    def test_config_paths_default_to_the_workspace_derived_tree(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                paths = config.paths()
                ws = workspace.resolve()
                self.assertEqual(paths.root, ws.derived_dir)
                self.assertEqual(paths.workspace_root, ws.root)
                self.assertEqual(paths.documents_dir, ws.documents_dir)
                # Every historical attribute is still present.
                for name in ("root", "config_dir", "links_file", "settings_file",
                             "datasets_dir", "history_dir", "uploads_dir"):
                    self.assertTrue(hasattr(paths, name), name)
                for name in ("manifest_file", "extracted_dir", "jobs_dir",
                             "workspace_root", "documents_dir"):
                    self.assertTrue(hasattr(paths, name), name)

    def test_config_data_env_still_overrides_the_root(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            os.environ["PEEKSEED_DATA"] = str(Path(tmp) / "data")
            paths = config.ensure()
            self.assertEqual(paths.root, (Path(tmp) / "data").resolve())
            self.assertTrue(paths.config_dir.is_dir())
            self.assertTrue(paths.links_file.parent.is_dir())


class InitTests(unittest.TestCase):
    def test_init_creates_the_contract_tree_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    report = workspace.init()
                self.assertIn("workspace:", buffer.getvalue())
                root = (Path(tmp) / "workspace").resolve()
                self.assertEqual(report["root"], str(root))
                self.assertEqual(report["derived_dir"], str(root / ".peekseed"))
                for name in workspace._DERIVED_CHILDREN:
                    self.assertTrue((root / ".peekseed" / name).is_dir(), name)
                self.assertTrue((root / "documents").is_dir())
                self.assertTrue((root / ".peekseed" / "manifest.json").is_file())
                self.assertTrue((root / ".peekseed" / "config" / "links.json").is_file())
                stored_links = json.loads(
                    (root / ".peekseed" / "config" / "links.json").read_text())
                self.assertEqual(stored_links, {"version": 1, "links": []})
                self.assertEqual(_mode(root / ".peekseed"), 0o700)
                for name in workspace._DERIVED_CHILDREN:
                    self.assertEqual(_mode(root / ".peekseed" / name), 0o700, name)
                self.assertEqual(_mode(root / "documents"), 0o755)
                self.assertEqual(_mode(root / ".peekseed" / "config" / "links.json"),
                                 0o600)
                manifest = json.loads(
                    (root / ".peekseed" / "manifest.json").read_text())
                self.assertEqual(manifest["schema"], contracts.MANIFEST_SCHEMA)
                contracts.validate_manifest(manifest)
                # Idempotent: nothing is overwritten and no entry is duplicated.
                manifest["created_at"] = "2020-01-01T00:00:00Z"
                (root / ".peekseed" / "manifest.json").write_text(
                    json.dumps(manifest))
                with contextlib.redirect_stdout(io.StringIO()):
                    again = workspace.init()
                self.assertEqual(again["root"], str(root))
                self.assertEqual(
                    json.loads((root / ".peekseed" / "manifest.json")
                               .read_text())["created_at"],
                    "2020-01-01T00:00:00Z",
                )
                gitignore = (Path(tmp) / ".gitignore").read_text()
                self.assertEqual(gitignore.count("/workspace/"), 1)

    def test_init_is_idempotent_in_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            checkout = Path(tmp)
            (checkout / ".git").mkdir()
            (checkout / ".gitignore").write_text("# existing\n*.log\n")
            with contextlib.redirect_stdout(io.StringIO()):
                report = workspace.init(str(checkout / "workspace"))
            self.assertEqual(report["gitignore"]["git_root"], str(checkout.resolve()))
            content = (checkout / ".gitignore").read_text()
            self.assertIn("/workspace/\n", content)
            self.assertIn("*.log", content)
            # A second init does not append the entry twice.
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init(str(checkout / "workspace"))
            self.assertEqual((checkout / ".gitignore").read_text().count("/workspace/"), 1)

    def test_init_never_overwrites_documents(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                document = Path(tmp) / "workspace" / "documents" / "keep.txt"
                document.write_text("user data")
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                self.assertEqual(document.read_text(), "user data")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_a_clean_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                report = workspace.doctor()
                self.assertEqual(report["issues"], [])
                self.assertEqual(report["paths"]["root"],
                                 str((Path(tmp) / "workspace").resolve()))
                self.assertIn("manifest", report["schemas"])
                self.assertTrue(report["links"]["ok"])

    def test_doctor_detects_a_missing_manifest_and_links(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                root = Path(tmp) / "workspace"
                (root / ".peekseed" / "manifest.json").unlink()
                (root / ".peekseed" / "config" / "links.json").unlink()
                report = workspace.doctor()
                joined = "\n".join(report["issues"])
                self.assertIn("manifest.json is missing", joined)
                self.assertIn("model link store is missing", joined)

    def test_doctor_detects_corrupt_json_and_legacy_schemas(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                root = Path(tmp) / "workspace"
                (root / ".peekseed" / "manifest.json").write_text("{not json")
                dataset_dir = root / ".peekseed" / "datasets" / "legacy"
                dataset_dir.mkdir()
                (dataset_dir / "dataset.json").write_text(json.dumps({
                    "version": 1, "id": "legacy", "name": "Legacy",
                    "created_at": "2020-01-01T00:00:00Z",
                    "sources": [{"name": "a.pdf"}],
                }))
                report = workspace.doctor()
                joined = "\n".join(report["issues"])
                self.assertIn("manifest.json is unreadable", joined)
                self.assertIn("dataset 'legacy' uses schema 1", joined)
                self.assertIn("peekseed migrate", joined)
                # No full machine paths leak into user-facing issue strings.
                for issue in report["issues"]:
                    self.assertNotIn(str(root), issue)

    def test_doctor_detects_an_unignored_git_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            (Path(tmp) / ".git").mkdir()
            # A .gitignore without the anchored entry: init would add it, so no init.
            (Path(tmp) / ".gitignore").write_text("*.log\n")
            report = workspace.doctor(str(Path(tmp) / "workspace"))
            self.assertEqual(report["git"]["root"], str(Path(tmp).resolve()))
            self.assertFalse(report["git"]["ignored"])
            self.assertTrue(any("not in .gitignore" in issue
                                for issue in report["issues"]))


class StatusTests(unittest.TestCase):
    def _write_dataset(self, root, collection="cxl", status="chunked", chunks=2):
        dataset_dir = root / ".peekseed" / "datasets" / collection
        dataset_dir.mkdir(parents=True, exist_ok=True)
        dataset = {
            "schema": contracts.DATASET_SCHEMA,
            "id": collection,
            "name": "CXL docs",
            "created_at": "2020-01-01T00:00:00Z",
            "sources": [{"name": "a.pdf", "path": "cxl/a.pdf",
                         "sha256": "a" * 64, "bytes": 10, "pages": 1,
                         "extractor": "pypdf@1.0", "status": "chunked"}],
            "chunker": contracts.chunker_label(),
            "extractor": "pypdf@1.0",
            "status": status,
        }
        contracts.write_json_atomic(dataset_dir / "dataset.json",
                                    contracts.validate_dataset(dataset))
        (dataset_dir / "chunks.jsonl").write_text(
            "\n".join(json.dumps({"id": "x%d" % index}) for index in range(chunks))
            + "\n")
        return dataset_dir

    def test_status_shape(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
            root = Path(tmp) / "workspace"
            self._write_dataset(root)
            manifest = json.loads((root / ".peekseed" / "manifest.json").read_text())
            manifest["last_scan"] = "2024-01-02T03:04:05Z"
            contracts.write_json_atomic(root / ".peekseed" / "manifest.json", manifest)
            with working_directory(tmp):
                report = workspace.status()
            self.assertEqual(report["last_scan"], "2024-01-02T03:04:05Z")
            self.assertEqual(set(report["workspace"]), {
                "root", "documents_dir", "derived_dir", "manifest_file",
                "config_dir", "links_file", "settings_file", "datasets_dir",
                "extracted_dir", "history_dir", "jobs_dir", "uploads_dir",
            })
            collection = report["collections"]["cxl"]
            self.assertEqual(collection["status"], "chunked")
            self.assertEqual(collection["sources"], 1)
            self.assertEqual(collection["chunks"], 2)
            self.assertEqual(report["counts"]["collections"], 1)
            self.assertEqual(report["counts"]["chunks"], 2)

    def test_status_derives_a_collection_without_a_status_field(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
            root = Path(tmp) / "workspace"
            dataset_dir = self._write_dataset(root)
            data = json.loads((dataset_dir / "dataset.json").read_text())
            data.pop("status")
            contracts.write_json_atomic(dataset_dir / "dataset.json",
                                        contracts.validate_dataset(data))
            with working_directory(tmp):
                report = workspace.status()
            self.assertEqual(report["collections"]["cxl"]["status"], "not_indexed")


class MoveTests(unittest.TestCase):
    def test_move_moves_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                document = Path(tmp) / "workspace" / "documents" / "a.txt"
                document.write_text("hello")
                target = Path(tmp) / "elsewhere" / "new-workspace"
                moved = workspace.move(str(target))
                self.assertEqual(moved["root"], str(target.resolve()))
                self.assertFalse((Path(tmp) / "workspace").exists())
                new_root = target.resolve()
                self.assertEqual((new_root / "documents" / "a.txt").read_text(),
                                 "hello")
                self.assertTrue((new_root / ".peekseed" / "manifest.json").is_file())
                self.assertEqual(workspace.resolve(str(target)).root, new_root)

    def test_move_refuses_an_occupied_target(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with contextlib.redirect_stdout(io.StringIO()):
                    workspace.init()
                occupied = Path(tmp) / "occupied"
                occupied.mkdir()
                (occupied / "file.txt").write_text("not empty")
                with self.assertRaises(ValueError):
                    workspace.move(str(occupied))
                self.assertTrue((Path(tmp) / "workspace").is_dir())


if __name__ == "__main__":
    unittest.main()

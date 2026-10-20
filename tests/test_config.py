import json
import os
import unittest
from pathlib import Path

from tests.fixtures import temp_data_root


class ConfigTests(unittest.TestCase):
    def test_ensure_creates_the_whole_data_root(self):
        with temp_data_root() as root:
            from peekseed import config
            paths = config.ensure()
            self.assertEqual(paths.root, root.resolve())
            for directory in (paths.config_dir, paths.datasets_dir,
                              paths.history_dir, paths.uploads_dir):
                self.assertTrue(directory.is_dir(), directory)

    def test_paths_cache_follows_the_data_root(self):
        with temp_data_root() as root:
            from peekseed import config
            first = config.paths()
            self.assertEqual(first.root, root.resolve())
            os.environ["PEEKSEED_DATA"] = str(root / "other")
            config.reset_cache()
            second = config.paths()
            self.assertEqual(second.root, (root / "other").resolve())
            self.assertNotEqual(first.links_file, second.links_file)

    def test_settings_defaults_and_round_trip(self):
        with temp_data_root():
            from peekseed import config
            settings = config.load_settings()
            self.assertEqual(settings["title"], "peekseed")
            self.assertEqual(settings["examples"], [])
            saved = config.save_settings({"title": "Docs", "tagline": "Find it",
                                          "examples": ["how?"]})
            self.assertEqual(saved["title"], "Docs")
            self.assertEqual(saved["examples"], ["how?"])
            # unknown keys are not persisted as a second config surface
            config.save_settings({"title": "Docs", "unexpected": 1})
            raw = json.loads(config.paths().settings_file.read_text())
            self.assertNotIn("unexpected", raw)

    def test_settings_validation(self):
        with temp_data_root():
            from peekseed import config
            with self.assertRaises(ValueError):
                config.save_settings({"title": "  "})
            with self.assertRaises(ValueError):
                config.save_settings({"examples": "not a list"})

    def test_host_and_port(self):
        with temp_data_root():
            from peekseed import config
            old_host = os.environ.pop("PEEKSEED_HOST", None)
            old_port = os.environ.pop("PEEKSEED_PORT", None)
            try:
                self.assertEqual(config.host(), "127.0.0.1")
                self.assertEqual(config.port(), 8030)
                os.environ["PEEKSEED_HOST"] = "0.0.0.0"
                os.environ["PEEKSEED_PORT"] = "9000"
                self.assertEqual(config.host(), "0.0.0.0")
                self.assertEqual(config.port(), 9000)
                os.environ["PEEKSEED_PORT"] = "nope"
                with self.assertRaises(ValueError):
                    config.port()
            finally:
                for key, value in (("PEEKSEED_HOST", old_host),
                                   ("PEEKSEED_PORT", old_port)):
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

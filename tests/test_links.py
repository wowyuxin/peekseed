import os
import stat
import unittest

from tests.fixtures import temp_data_root


class LinkStoreTests(unittest.TestCase):
    def _links(self):
        from peekseed import links
        return links

    def _make(self, **overrides):
        data = {"role": "embed", "label": "My embedder", "protocol": "openai",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "nomic-embed-text", "enabled": True}
        data.update(overrides)
        return data

    def test_first_run_writes_an_empty_config(self):
        with temp_data_root():
            links = self._links()
            links.reset_cache()
            self.assertEqual(links.links(), [])
            self.assertTrue(links.config_path().is_file())
            self.assertTrue(links.seeded())

    def test_validation_rejects_bad_values(self):
        with temp_data_root():
            links = self._links()
            cases = [
                self._make(base_url=""),
                self._make(base_url="ftp://x"),
                self._make(base_url="http://x:bad"),
                self._make(protocol="banana"),
                self._make(model=""),
                self._make(label=""),
                self._make(api_key="a\nb"),
                self._make(unknown="x"),
            ]
            for case in cases:
                with self.assertRaises(links.LinkValidationError, msg=case):
                    links.create(case)

    def test_ssrf_guard_rejects_metadata_link_local_and_bad_schemes(self):
        """links.validate() itself enforces the SSRF guard (not just the app)."""
        with temp_data_root():
            links = self._links()
            cases = [
                ("http://169.254.169.254/latest/meta-data", "metadata"),
                ("http://169.254.1.1/v1", "link-local"),
                ("http://[fd00:ec2::254]/", "metadata"),
                ("file:///etc/passwd", "http"),
                ("http://user:pass@127.0.0.1/v1", "credentials"),
            ]
            for url, expected in cases:
                with self.assertRaises(links.LinkValidationError, msg=url) as raised:
                    links.create(self._make(base_url=url))
                self.assertIn(expected, str(raised.exception).lower())
            # Loopback and RFC1918 stay allowed: local-first model servers.
            links.create(self._make(label="Loopback", base_url="http://127.0.0.1:9/v1"))

    def test_create_mask_and_key_never_returned(self):
        with temp_data_root():
            links = self._links()
            link = links.create(self._make(api_key="secret-key"))
            self.assertEqual(link["api_key"], "secret-key")
            masked = links.mask(links.get(link["id"]))
            self.assertTrue(masked["api_key_set"])
            self.assertNotIn("api_key", masked)
            self.assertNotIn("secret-key", str(masked))
            raw = links.config_path().read_text()
            self.assertIn("secret-key", raw)
            mode = stat.S_IMODE(os.stat(links.config_path()).st_mode)
            self.assertEqual(mode, 0o600)

    def test_update_keeps_omitted_key_and_can_clear_it(self):
        with temp_data_root():
            links = self._links()
            link = links.create(self._make(api_key="secret-key"))
            updated = links.update(link["id"], {"label": "Renamed"})
            self.assertEqual(updated["api_key"], "secret-key")
            cleared = links.update(link["id"], {"api_key": ""})
            self.assertEqual(cleared["api_key"], "")
            self.assertFalse(links.mask(cleared)["api_key_set"])

    def test_one_enabled_link_per_stage_role(self):
        with temp_data_root():
            links = self._links()
            first = links.create(self._make(label="First"))
            second = links.create(self._make(label="Second"))
            self.assertFalse(links.get(first["id"])["enabled"])
            self.assertTrue(links.get(second["id"])["enabled"])
            links.set_enabled(first["id"], True)
            self.assertTrue(links.get(first["id"])["enabled"])
            self.assertFalse(links.get(second["id"])["enabled"])

    def test_answer_links_can_all_be_enabled(self):
        with temp_data_root():
            links = self._links()
            a = links.create({"role": "answer", "label": "A", "protocol": "openai",
                              "base_url": "http://127.0.0.1:1/v1", "model": "one"})
            b = links.create({"role": "answer", "label": "B", "protocol": "openai",
                              "base_url": "http://127.0.0.1:2/v1", "model": "two"})
            self.assertTrue(links.get(a["id"])["enabled"])
            self.assertTrue(links.get(b["id"])["enabled"])

    def test_embed_space_key_tracks_link_and_model(self):
        with temp_data_root():
            links = self._links()
            link = links.create(self._make())
            key = links.embed_space_key(link)
            self.assertIn(link["id"], key)
            self.assertIn("nomic-embed-text", key)
            other = self._make(label="Other", model="another-model")
            self.assertNotEqual(key, links.embed_space_key(other))

    def test_create_generates_an_id_from_the_label(self):
        with temp_data_root():
            links = self._links()
            link = links.create(self._make(label="My Embedder!"))
            self.assertEqual(link["id"], "my-embedder")

    def test_delete_reports_missing_ids(self):
        with temp_data_root():
            links = self._links()
            link = links.create(self._make())
            self.assertTrue(links.delete(link["id"]))
            self.assertFalse(links.delete(link["id"]))


if __name__ == "__main__":
    unittest.main()

"""M3 CP0.5: the decision child-env boundary in the peelfeed seam.

Mirrors the CP5.1 H7 VLM proof for the decision provider: tuning env reaches
every peelfeed child, the provider seam never does, and the secret
BASE/MODEL/TOKEN/LINK_ID variables are built only by ``decision_child_env``
(resolved from peekseed's own env first, else the first enabled decision link).
"""

import contextlib
import json
import os
import unittest

from tests.fixtures import temp_data_root

TUNING = {
    "PEELFEED_DECISION_TIMEOUT": "45",
    "PEELFEED_DECISION_BUDGET_USD": "1.25",
    "PEELFEED_DECISION_PRICE_INPUT": "0.042",
    "PEELFEED_DECISION_MAX_CALLS_PER_JOB": "64",
    "PEELFEED_DECISION_IMAGES": "1",
}


@contextlib.contextmanager
def _decision_tuning_env():
    saved = {key: os.environ.get(key) for key in
             list(TUNING) + ["PEELFEED_DECISION_PROVIDER"]}
    os.environ.update(TUNING)
    # The local/test seam must never reach a child.
    os.environ["PEELFEED_DECISION_PROVIDER"] = (
        "sh -c 'echo never-forwarded; exit 2'")
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DecisionEnvForwardingTests(unittest.TestCase):
    def test_tuning_reaches_all_children_and_the_seam_never_does(self):
        with temp_data_root():
            from peekseed import peelfeed
            with _decision_tuning_env():
                env = peelfeed.client()._environment()
                for key, value in TUNING.items():
                    self.assertEqual(env.get(key), value, key)
                self.assertNotIn("PEELFEED_DECISION_PROVIDER", env)
                # The secret four never leak into the base environment.
                for key in peelfeed.DECISION_ENV_KEYS:
                    self.assertNotIn(key, env)
                # VLM stripping is untouched by the decision rules.
                self.assertNotIn("PEELFEED_LLM_TOKEN", env)
                self.assertNotIn("PEELFEED_LLM_PROVIDER", env)

    def test_every_child_gets_tuning_but_never_secrets(self):
        with temp_data_root():
            from peekseed import links, peelfeed
            # A configured decision link must not change what ordinary
            # children receive: tuning yes, secrets no.
            links.reset_cache()
            links.create({"role": "decision", "label": "D",
                          "protocol": "typesafe",
                          "base_url": "http://127.0.0.1:9",
                          "model": "jev-1.14.0", "enabled": True})
            with _decision_tuning_env():
                env = peelfeed.client()._environment()
                self.assertEqual(env["PEELFEED_DECISION_TIMEOUT"], "45")
                for key in peelfeed.DECISION_ENV_KEYS:
                    self.assertNotIn(key, env)


class DecisionChildEnvTests(unittest.TestCase):
    def _decision(self):
        from peekseed import peelfeed
        return peelfeed

    def test_env_provider_wins_and_builds_child_env(self):
        saved = {key: os.environ.get(key)
                 for key in self._decision().DECISION_ENV_KEYS}
        os.environ.update({
            "PEELFEED_DECISION_BASE": "http://127.0.0.1:9911",
            "PEELFEED_DECISION_MODEL": "jev-1.14.0",
            "PEELFEED_DECISION_TOKEN": "decision-secret",
            "PEELFEED_DECISION_LINK_ID": "env",
        })
        try:
            with temp_data_root():
                peelfeed = self._decision()
                provider = peelfeed.decision_provider()
                self.assertTrue(provider["available"])
                self.assertEqual(provider["provider"], "env")
                self.assertEqual(provider["model"], "jev-1.14.0")
                self.assertNotIn("_token", provider)
                self.assertNotIn("decision-secret", json.dumps(provider))
                env = peelfeed.decision_child_env()
                self.assertEqual(env["PEELFEED_DECISION_TOKEN"],
                                 "decision-secret")
                self.assertEqual(env["PEELFEED_DECISION_BASE"],
                                 "http://127.0.0.1:9911")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_first_enabled_decision_link_is_the_fallback(self):
        with temp_data_root():
            from peekseed import links
            peelfeed = self._decision()
            os.environ["PEEKSEED_DECISION_TEST_API_KEY"] = "decision-secret-2"
            try:
                links.reset_cache()
                links.create({"role": "decision", "label": "First",
                              "protocol": "typesafe",
                              "base_url": "http://127.0.0.1:9922",
                              "model": "jev-1.14.0", "enabled": True,
                              "api_key_env": "PEEKSEED_DECISION_TEST_API_KEY"})
                provider = peelfeed.decision_provider()
                self.assertTrue(provider["available"])
                self.assertEqual(provider["provider"], "decision_link")
                self.assertEqual(provider["link_id"],
                                 links.default_decision_link()["id"])
                self.assertNotIn("decision-secret-2", json.dumps(provider))
                env = peelfeed.decision_child_env()
                self.assertEqual(env["PEELFEED_DECISION_TOKEN"],
                                 "decision-secret-2")
                self.assertEqual(env["PEELFEED_DECISION_LINK_ID"],
                                 provider["link_id"])
            finally:
                os.environ.pop("PEEKSEED_DECISION_TEST_API_KEY", None)

    def test_missing_provider_is_a_503_decision_unavailable(self):
        with temp_data_root():
            peelfeed = self._decision()
            provider = peelfeed.decision_provider()
            self.assertFalse(provider["available"])
            self.assertIn("decision link", provider["reason"])
            with self.assertRaises(peelfeed.PeelfeedError) as ctx:
                peelfeed.decision_child_env()
            self.assertEqual(ctx.exception.status, 503)
            self.assertIn("decision_unavailable", ctx.exception.detail)

    def test_configured_link_without_key_is_unavailable(self):
        with temp_data_root():
            from peekseed import links
            peelfeed = self._decision()
            links.reset_cache()
            links.create({"role": "decision", "label": "No key",
                          "protocol": "typesafe",
                          "base_url": "http://127.0.0.1:9",
                          "model": "jev-1.14.0", "enabled": True,
                          "api_key_env": "PEEKSEED_DECISION_MISSING_API_KEY"})
            provider = peelfeed.decision_provider()
            self.assertFalse(provider["available"])
            self.assertIn("not set", provider["reason"])


if __name__ == "__main__":
    unittest.main()

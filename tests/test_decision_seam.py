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


@contextlib.contextmanager
def _clean_provider_env():
    """Remove any ambient decision/VLM provider env for one test."""
    keys = [key for key in os.environ
            if key.startswith(("PEELFEED_DECISION_", "PEELFEED_LLM_"))]
    saved = {key: os.environ[key] for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(saved)


class JobResumeProviderEnvTests(unittest.TestCase):
    """CP5 F1: ``job resume`` injects the SAME provider env as adjudicate.

    The resumed runner continues provider work, so the resume child must
    receive the decision/VLM child env exactly like an adjudicate child; with
    no provider configured the env stays stripped (degraded resume, never a
    bypass of the allowlisted subprocess env rules).
    """

    PROVIDER_ENV = {
        "decision": {"PEELFEED_DECISION_BASE": "http://127.0.0.1:9911",
                     "PEELFEED_DECISION_MODEL": "jev-1.14.0",
                     "PEELFEED_DECISION_TOKEN": "decision-secret",
                     "PEELFEED_DECISION_LINK_ID": "env"},
        "llm": {"PEELFEED_LLM_BASE": "http://127.0.0.1:9912/v1",
                "PEELFEED_LLM_MODEL": "fake-vlm",
                "PEELFEED_LLM_TOKEN": "vlm-secret",
                "PEELFEED_LLM_LINK_ID": "env"},
    }

    def _cli(self, root):
        from tests.fixtures import FakePeelfeedCli
        cli = FakePeelfeedCli(root / "fake")
        cli.update(peels={"peel-1": {"peel_id": "peel-1", "state": "ready"}},
                   jobs={"adjudicate-1": {
                       "job_id": "adjudicate-1", "type": "adjudicate",
                       "status": "paused", "progress": {},
                       "result_ref": None}})
        return cli

    def _resume_call(self, cli):
        return next(call for call in cli.calls()
                    if call["argv"][:2] == ["job", "resume"])

    def test_resume_child_gets_the_injected_provider_env(self):
        with temp_data_root() as root:
            from peekseed import peelfeed
            from tests.test_review_ocr import _peelfeed_cli_env
            cli = self._cli(root)
            with _peelfeed_cli_env(cli.path), _clean_provider_env():
                peelfeed.client().job_resume(
                    "peel-1", "adjudicate-1",
                    provider_env=self.PROVIDER_ENV)
                call = self._resume_call(cli)
                self.assertEqual(call["decision_env"],
                                 self.PROVIDER_ENV["decision"])
                self.assertEqual(call["llm_env"], self.PROVIDER_ENV["llm"])

    def test_resume_env_is_identical_to_the_adjudicate_env(self):
        with temp_data_root() as root:
            from peekseed import peelfeed
            from tests.test_review_ocr import _peelfeed_cli_env
            cli = self._cli(root)
            request = {"dataset_id": "ds", "settings": {},
                       "authorization": {"authorization_id": "auth-1"},
                       "scope": {"all_unresolved": True}}
            with _peelfeed_cli_env(cli.path), _clean_provider_env():
                client = peelfeed.client()
                client.adjudicate("peel-1", request,
                                  provider_env=self.PROVIDER_ENV)
                client.job_resume("peel-1", "adjudicate-1",
                                  provider_env=self.PROVIDER_ENV)
                calls = cli.calls()
                adjudicate_call = next(call for call in calls
                                       if call["argv"][0] == "adjudicate")
                resume_call = self._resume_call(cli)
                self.assertEqual(resume_call["decision_env"],
                                 adjudicate_call["decision_env"])
                self.assertEqual(resume_call["llm_env"],
                                 adjudicate_call["llm_env"])

    def test_keyless_local_decision_env_is_forwarded_verbatim(self):
        with temp_data_root() as root:
            from peekseed import peelfeed
            from tests.test_review_ocr import _peelfeed_cli_env
            cli = self._cli(root)
            keyless = {"decision": {
                "PEELFEED_DECISION_BASE": "http://127.0.0.1:9911",
                "PEELFEED_DECISION_MODEL": "local-decision",
                "PEELFEED_DECISION_TOKEN": "",
                "PEELFEED_DECISION_LINK_ID": "env"}}
            with _peelfeed_cli_env(cli.path), _clean_provider_env():
                peelfeed.client().job_resume("peel-1", "adjudicate-1",
                                             provider_env=keyless)
                call = self._resume_call(cli)
                self.assertEqual(call["decision_env"], keyless["decision"])
                # No VLM provider configured: the LLM env stays stripped.
                self.assertEqual(call["llm_env"], {})

    def test_resume_without_provider_env_stays_stripped(self):
        with temp_data_root() as root:
            from peekseed import peelfeed
            from tests.test_review_ocr import _peelfeed_cli_env
            cli = self._cli(root)
            with _peelfeed_cli_env(cli.path), _clean_provider_env():
                # Even with provider credentials in the parent env, a resume
                # without an explicit provider_env never forwards them.
                added = {
                    "PEELFEED_DECISION_BASE": "http://127.0.0.1:9911",
                    "PEELFEED_DECISION_MODEL": "jev-1.14.0",
                    "PEELFEED_DECISION_TOKEN": "decision-secret",
                    "PEELFEED_LLM_BASE": "http://127.0.0.1:9912/v1",
                    "PEELFEED_LLM_MODEL": "fake-vlm",
                    "PEELFEED_LLM_TOKEN": "vlm-secret",
                }
                os.environ.update(added)
                try:
                    peelfeed.client().job_resume("peel-1", "adjudicate-1")
                finally:
                    for key in added:
                        os.environ.pop(key, None)
                call = self._resume_call(cli)
                self.assertEqual(call["decision_env"], {})
                self.assertEqual(call["llm_env"], {})


if __name__ == "__main__":
    unittest.main()

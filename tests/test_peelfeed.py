"""Tests for the peelfeed seam: config store, CLI adapter and the API routes.

No real extraction happens here: :class:`tests.fixtures.FakePeelfeedCli` is a
small executable that prints the JSON the real peelfeed CLI prints, records every
invocation (argv + ``PEELFEED_SHARED_DIR``/``PYTHONPATH``) and can simulate failures,
timeouts and slow ingests. Peel corpora used by index builds are created by
``tests.fixtures.make_peel``. The real engine is exercised by the end-to-end
verification, not by this suite.

There is no HTTP client or token in this seam: the CLI is the whole contract.
"""

import contextlib
import hashlib
import json
import os
import time
import unittest

from tests.fixtures import (EMBED_CHUNK, FakeModelServer, FakePeelfeedCli,
                            make_pdf, make_peel, temp_data_root)

_PEELFEED_ENV_KEYS = ("PEEKSEED_PEELFEED_CLI", "PEEKSEED_PEELFEED_PYTHON",
                      "PEEKSEED_PEELFEED_REPO", "PEEKSEED_PEELFEED_ENABLED",
                      "PEEKSEED_PEELFEED_TIMEOUT", "PEEKSEED_PEELFEED_POLL",
                      "PEEKSEED_PEELFEED_LLM_MODE",
                      "PEEKSEED_PEELFEED_CHUNK_PROFILE")


def _clear_peelfeed_env():
    for key in list(os.environ):
        if key.startswith("PEEKSEED_PEELFEED_"):
            os.environ.pop(key, None)


@contextlib.contextmanager
def peelfeed_env(**values):
    """Set the peelfeed env for one test and reset the config/handshake caches.

    Every key is prefixed with ``PEEKSEED_PEELFEED_``. Empty values are left
    unset. The peel root is always ``config.paths().peels_dir``.
    """
    saved = {key: os.environ.get(key) for key in _PEELFEED_ENV_KEYS}
    _clear_peelfeed_env()
    for name, value in values.items():
        if value is None or value == "":
            continue
        os.environ["PEEKSEED_PEELFEED_" + name] = str(value)
    from peekseed import peelfeed
    peelfeed.reset_cache()
    try:
        yield
    finally:
        _clear_peelfeed_env()
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
        peelfeed.reset_cache()


def _shared(root):
    """The shared folder of a ``temp_data_root`` home (resolved)."""
    return (root.parent / "shared").resolve()


def _client():
    from fastapi.testclient import TestClient
    from peekseed import app as app_module
    app_module.reset_state()
    return TestClient(app_module.create_app())


def _upload(client, root, pages=None, filename="manual.pdf"):
    pdf = make_pdf(root / (filename + ".tmp"), pages=pages)
    response = client.post(
        "/api/datasets", data={"name": "Docs"},
        files=[("files", (filename, pdf.read_bytes(), "application/pdf"))])
    assert response.status_code == 201, response.text
    return response.json()["dataset"], pdf


def _sources(client, dataset_id):
    response = client.get("/api/datasets/%s/sources" % dataset_id)
    assert response.status_code == 200, response.text
    return response.json()["sources"]


def _wait_source(client, dataset_id, sha, timeout=25.0):
    """Poll the sources list until the extraction reaches a terminal state."""
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        rows = _sources(client, dataset_id)
        row = next((item for item in rows if item["sha256"] == sha), {})
        state = (row.get("extraction") or {}).get("state")
        if state in ("extracted", "failed", "stale"):
            return row
        time.sleep(0.05)
    raise AssertionError("source %s did not finish: %r" % (sha[:8], row))


def _wait_job(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = client.get("/api/jobs/%s" % job_id).json()
        if state.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
            return state
        time.sleep(0.02)
    raise AssertionError("job %s did not finish: %r" % (job_id, state))


def _extracted_review_source(client, root):
    """Upload, extract and return the ``/api/datasets/<id>/sources/<sha>`` base."""
    dataset, _pdf = _upload(client, root)
    sha = _sources(client, dataset["id"])[0]["sha256"]
    client.post("/api/datasets/%s/sources/%s/extract" % (dataset["id"], sha),
                json={})
    _wait_source(client, dataset["id"], sha)
    return "/api/datasets/%s/sources/%s" % (dataset["id"], sha)


def _add_embed_link(client, fake):
    response = client.post("/api/links", json={
        "role": "embed", "label": "Fake embed", "protocol": "openai",
        "base_url": fake.openai_base, "model": "test-embed", "enabled": True,
        "chunk": dict(EMBED_CHUNK)})
    assert response.status_code == 201, response.text
    return response.json()["link"]


class UiWiringTests(unittest.TestCase):
    def test_the_ui_ships_peelfeed_controls(self):
        with temp_data_root():
            client = _client()
            page = client.get("/").content
            for marker in (b'id="peelfeedBanner"', b'id="reviewModal"',
                           b'id="reviewClose"', b'id="reviewMsg"',
                           b'id="reviewSummary"', b'id="reviewClusters"',
                           b'id="reviewDetail"', b'reviewReportBtn',
                           b'reviewRefreshBtn', b'extractSource',
                           b'openReview', b'/api/peelfeed/status'):
                self.assertIn(marker, page)
            # CP1 removed the old flat Accept/Reject + "Apply & rebuild" UI.
            for gone in (b'reviewRebuildBtn', b'decideReview',
                         b'rebuildReviewed'):
                self.assertNotIn(gone, page)

    def test_the_ui_ships_cp2_strategy_param_inputs(self):
        """CP2 E2: the served UI renders real params, never an unconditional {}."""
        with temp_data_root():
            page = _client().get("/").text
            for marker in ("reviewStrategyParamsHtml", "reviewSyncStrategyParams",
                           "reviewStrategyParams", "REVIEW_PARAM_STRATEGIES",
                           "inherit_parent_header:'parent'",
                           "use_header_rows:'rows'",
                           "parent_candidates", 'data-r="parent-asset"',
                           "parent_asset_id", 'data-r="header-row-0"',
                           'data-r="header-row-1"', "header_cells",
                           "原生表头", "规则表头",
                           "请至少输入一个表头行号",
                           "两个表头行号必须不同",
                           "parent_asset_id 不能为空",
                           "reviewStrategyParams(card,opt.value)",
                           "params:built.params"):
                self.assertIn(marker, page)

    def test_the_ui_maps_stale_reasons_and_reconfirm_affordances(self):
        """CP2 E3: every stable stale code has a Chinese label and confirm
        distinguishes refresh-only reasons from blocking ones."""
        with temp_data_root():
            page = _client().get("/").text
            for code, label in (
                    ("evidence_changed", "证据变化"),
                    ("parent_changed", "父表变化"),
                    ("parent_header_not_selected", "父表表头未确认"),
                    ("strategy_changed", "策略已变更"),
                    ("capability_unavailable", "能力不可用"),
                    ("action_invalid", "动作已失效"),
                    ("chain_not_allowed", "禁止链式继承"),
                    ("item_missing", "条目缺失")):
                self.assertIn(code + ":'" + label + "'", page)
            for marker in (
                    "REVIEW_STALE_REFRESHABLE=['evidence_changed','parent_changed']",
                    "reviewStaleRefreshable", "reviewStaleLabel",
                    "reviewStaleBadge", "已过期：",
                    "canConfirm=hasEntry&&(!stale||refreshable)",
                    "重新确认会刷新证据快照",
                    "需重新选择策略或先处理父表"):
                self.assertIn(marker, page)

    def test_the_ui_ships_cp2_accept_clear_and_refresh_controls(self):
        """CP2 W9: 确认选中项/清除选择 + the E4 re-confirm refresh display."""
        with temp_data_root():
            page = _client().get("/").text
            for marker in (
                    "确认选中项", "清除选择",
                    'data-r="accept-selected"', 'data-r="accept-pick"',
                    "reviewSelectedItemIds", "reviewSyncAcceptButton",
                    "reviewAcceptErrText", "reviewAcceptReasons",
                    "REVIEW_ACCEPT_REASON_LABEL",
                    "locked:'已锁定'", "stale:'已过期'",
                    "no_ready_suggestion:'无就绪建议'",
                    "validator_failed:'校验未通过'", "item_missing:'条目缺失'",
                    "body.action='accept'", "item_ids=ids",
                    "body.action='clear'", "body.entry_id=entry.entry_id",
                    "plan.cleared.reverted_resolution", "已撤销 R0 记录",
                    "reviewRefreshText", "已重新确认（revision r",
                    "证据已刷新（revision 不变）",
                    "action_params：", "source_refs："):
                self.assertIn(marker, page)


class ConfigTests(unittest.TestCase):
    def test_env_seeds_a_0600_config_file_without_secrets(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), LLM_MODE="review", CHUNK_PROFILE="large",
                              TIMEOUT="12", POLL="1.5"):
                from peekseed import peelfeed
                cfg = peelfeed.load()
                path = peelfeed.config_path()
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(cfg["cli"], str(cli.path))
                self.assertEqual(cfg["root"], str(_shared(root) / "peels"))
                self.assertEqual(cfg["llm_mode"], "review")
                self.assertEqual(cfg["chunk_profile"], "large")
                self.assertEqual(cfg["timeout"], 12.0)
                self.assertEqual(cfg["poll"], 1.5)
                self.assertTrue(cfg["enabled"])       # auto from the CLI path
                self.assertTrue(peelfeed.seeded())
                masked = peelfeed.mask(cfg)
                self.assertTrue(masked["configured"])
                payload = json.dumps(masked)
                self.assertNotIn("base_url", payload)
                self.assertNotIn("token", payload)

    def test_no_cli_means_not_configured(self):
        with temp_data_root(), peelfeed_env():
            from peekseed import peelfeed
            self.assertFalse(peelfeed.configured())
            status = peelfeed.status(probe=False)
            self.assertFalse(status["config"]["configured"])
            self.assertFalse(status["handshake"]["available"])
            self.assertIn("PEEKSEED_PEELFEED_CLI", status["handshake"]["error"])

    def test_enabled_zero_disables_extraction(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), ENABLED="0"):
                from peekseed import peelfeed
                self.assertFalse(peelfeed.configured())
                handshake = peelfeed.handshake()
                self.assertFalse(handshake["available"])
                self.assertIn("disabled", handshake["error"])

    def test_env_wins_over_the_config_file(self):
        with temp_data_root() as root:
            first = FakePeelfeedCli(root / "first")
            second = FakePeelfeedCli(root / "second")
            with peelfeed_env(CLI=str(first.path)):
                from peekseed import peelfeed
                self.assertEqual(peelfeed.load()["cli"], str(first.path))
                os.environ["PEEKSEED_PEELFEED_CLI"] = str(second.path)
                peelfeed.reset_cache()
                self.assertEqual(peelfeed.load()["cli"], str(second.path))

    def test_relative_cli_path_is_a_config_error(self):
        with temp_data_root(), peelfeed_env(CLI="peelfeed/bin/peelfeed"):
            from peekseed import peelfeed
            with self.assertRaises(peelfeed.PeelfeedConfigError):
                peelfeed.load()

    def test_bad_llm_mode_is_a_config_error(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), LLM_MODE="yolo"):
                from peekseed import peelfeed
                with self.assertRaises(peelfeed.PeelfeedConfigError):
                    peelfeed.load()

    def test_python_fallback_derives_the_repo_and_sets_pythonpath(self):
        with temp_data_root() as root:
            repo = root / "peelfeed"
            (repo / "peelfeed").mkdir(parents=True)
            (repo / ".venv" / "bin").mkdir(parents=True)
            cli = FakePeelfeedCli(root / "fake")
            python = repo / ".venv" / "bin" / "python"
            python.write_text(cli.path.read_text(encoding="utf-8"),
                              encoding="utf-8")
            python.chmod(0o755)
            with peelfeed_env(PYTHON=str(python)):
                from peekseed import peelfeed
                cfg = peelfeed.load()
                self.assertEqual(cfg["repo"], str(repo))
                self.assertEqual(peelfeed._command(cfg),
                                 (str(python), "-m", "peelfeed.cli"))
                result = peelfeed.handshake(force=True)
                self.assertTrue(result["available"], result)
                call = cli.calls()[-1]
                self.assertTrue((call["pythonpath"] or "").startswith(str(repo)))
                self.assertEqual(call["peel_shared_dir"], str(_shared(root)))


class SubprocessEnvironmentTests(unittest.TestCase):
    def test_the_peelfeed_subprocess_gets_an_allowlisted_environment(self):
        with temp_data_root() as home:
            cli = FakePeelfeedCli(home / "fake")
            saved = {key: os.environ.get(key) for key in
                     ("PEEKSEED_TOKEN", "PEEKSEED_ANSWER_API_KEY", "FOO",
                      "PEELFEED_HOME")}
            os.environ["PEEKSEED_TOKEN"] = "secret-token"
            os.environ["PEEKSEED_ANSWER_API_KEY"] = "k"
            os.environ["FOO"] = "bar"
            os.environ["PEELFEED_HOME"] = str(home / "pf-home")
            try:
                with peelfeed_env(CLI=str(cli.path)):
                    from peekseed import config, peelfeed
                    env = peelfeed.client()._environment()
                    for name in ("PEEKSEED_TOKEN", "PEEKSEED_ANSWER_API_KEY",
                                 "FOO"):
                        self.assertNotIn(name, env)
                    self.assertIn("PEELFEED_HOME", env)
                    self.assertEqual(env["PEELFEED_SHARED_DIR"],
                                     str(config.paths().shared_dir))
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class HandshakeTests(unittest.TestCase):
    def test_handshake_reports_capabilities_and_is_cached_60s(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                first = peelfeed.handshake()
                self.assertTrue(first["available"], first)
                self.assertEqual(first["engine"], "peelfeed")
                self.assertEqual(first["version"], "0.1.0")
                self.assertEqual(first["cli_protocol"], 1)
                self.assertEqual(first["corpus_schema"], 2)
                self.assertEqual(first["chunk_contract"], 1)
                self.assertEqual(first["review_schema"], 2)
                self.assertIn("set_header_rows", first["operations"])
                self.assertIn("confirm_known", first["resolutions"])
                self.assertEqual(first["strategies"][0]["id"],
                                 "use_first_row_header")
                self.assertEqual(first["formats"], ["pdf"])
                self.assertIn("generic", first["profiles"])
                self.assertEqual(first["peel_root"], str(_shared(root) / "peels"))
                self.assertEqual(cli.commands(), ["capabilities"])
                second = peelfeed.handshake()
                self.assertTrue(second["available"])
                self.assertEqual(cli.commands(), ["capabilities"])
                forced = peelfeed.handshake(force=True)
                self.assertTrue(forced["available"])
                self.assertEqual(cli.commands(),
                                 ["capabilities", "capabilities"])

    def test_missing_cli_is_unavailable_not_a_crash(self):
        with temp_data_root() as root:
            with peelfeed_env(CLI=str(root / "does-not-exist")):
                from peekseed import peelfeed
                result = peelfeed.handshake()
                self.assertFalse(result["available"])
                self.assertTrue(result["configured"])
                self.assertIn("cannot start", result["error"])
                self.assertNotIn("Traceback", result["error"])

    def test_capabilities_timeout_is_unavailable(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"capabilities_delay": 5})
            with peelfeed_env(CLI=str(cli.path), TIMEOUT="0.3"):
                from peekseed import peelfeed
                result = peelfeed.handshake()
                self.assertFalse(result["available"])
                self.assertIn("timed out", result["error"])

    def test_capabilities_failure_is_unavailable(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"capabilities_fail": True})
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                result = peelfeed.handshake()
                self.assertFalse(result["available"])
                self.assertIn("capabilities failed", result["error"])

    def test_status_has_no_http_fields(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                payload = peelfeed.status(probe=True)
                self.assertTrue(payload["handshake"]["available"])
                text = json.dumps(payload)
                self.assertNotIn("base_url", text)
                self.assertNotIn("token", text)
                self.assertIn("cli", payload["config"])


class CliAdapterTests(unittest.TestCase):
    def test_client_calls_the_cli_contract_with_root_and_json(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"ingest_pages": 2,
                                                  "ingest_review_items": 2})
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                caps = engine.capabilities()
                self.assertEqual(caps["engine"], "peelfeed")
                # M2 review seam: schema gate + frozen catalogs.
                self.assertEqual(caps["review_schema"], 2)
                self.assertIn("set_header_rows", caps["operations"])
                self.assertIn("confirm_known", caps["resolutions"])
                self.assertEqual(caps["strategies"][0]["id"],
                                 "use_first_row_header")
                result = engine.ingest(str(source), llm_mode="off",
                                       chunk_profile="default",
                                       chunk_versions=[EMBED_CHUNK])
                self.assertTrue(result["ok"])
                peel_id = result["peel"]["peel_id"]
                summary = engine.status(peel_id)
                self.assertEqual(summary["peel_id"], peel_id)
                rows = engine.status_all()
                self.assertEqual([row["peel_id"] for row in rows], [peel_id])
                review = engine.review(peel_id)
                self.assertEqual(review["review_schema"], 2)
                self.assertEqual(review["counts"]["clusters"], 3)
                self.assertEqual(len(review["items"]), 3)
                item_id = review["items"][0]["id"]
                self.assertTrue(item_id.startswith("it-"))
                item = engine.review(peel_id, item=item_id)
                self.assertEqual(item["item"]["id"], item_id)
                filtered = engine.review(peel_id, group="code",
                                         status="needs_decision")
                self.assertEqual([row["id"] for row in filtered["items"]],
                                 [item_id])
                rebuilt = engine.rebuild(peel_id, ["review decisions"])
                self.assertTrue(rebuilt["ok"])
                event = engine.event(peel_id, "embed", {"generation": "g1"})
                self.assertTrue(event["ok"])
                archived = engine.archive(peel_id, reason="test")
                self.assertEqual(archived["state"], "archived")

                calls = cli.calls()
                commands = [call["argv"][0] for call in calls]
                for command in ("capabilities", "ingest", "status", "review",
                                "rebuild", "event", "archive"):
                    self.assertIn(command, commands)
                # The old decide command/method is removed, not deprecated.
                self.assertNotIn("decide", commands)
                self.assertFalse(hasattr(engine, "decide"))
                for call in calls:
                    self.assertEqual(call["peel_shared_dir"], str(_shared(root)))
                    if call["argv"][0] != "capabilities":
                        self.assertIn("--json", call["argv"])
                        self.assertIn("--root", call["argv"])
                review_calls = [call["argv"] for call in calls
                                if call["argv"][0] == "review"]
                self.assertEqual(review_calls[0][1], peel_id)
                self.assertNotIn("--group", review_calls[0])
                self.assertIn("--item", review_calls[1])
                self.assertIn(item_id, review_calls[1])
                self.assertIn("--group", review_calls[2])
                self.assertIn("code", review_calls[2])
                self.assertIn("--status", review_calls[2])
                self.assertIn("needs_decision", review_calls[2])
                ingest_call = next(call["argv"] for call in calls
                                   if call["argv"][0] == "ingest")
                self.assertIn("--llm", ingest_call)
                sizes = ingest_call[ingest_call.index("--chunk-versions") + 1]
                self.assertEqual(json.loads(sizes), [EMBED_CHUNK])
                rebuild_call = next(call["argv"] for call in calls
                                    if call["argv"][0] == "rebuild")
                self.assertIn("review decisions", rebuild_call)
                self.assertEqual(cli.state()["events"][0]["event"], "embed")

    def test_plan_actions_use_the_frozen_argv_and_stdin(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")["peel"]["peel_id"]
                item_id = "it-3f9c2a4b6d8e0f1a"
                selected = engine.plan(peel_id, {
                    "action": "select", "base_plan_version": 0,
                    "item_id": item_id,
                    "strategy": {"id": "use_first_row_header", "version": 1},
                    "params": {"rows": [0]}, "notes": "from test"})
                self.assertEqual(selected["plan_version"], 1)
                self.assertEqual(selected["entry"]["item_id"], item_id)
                self.assertEqual(selected["entry"]["strategy"]["id"],
                                 "use_first_row_header")
                self.assertEqual(selected["entry"]["action"]["params"],
                                 {"rows": [0]})
                self.assertEqual(selected["entry"]["notes"], "from test")
                entry_id = selected["entry"]["entry_id"]
                confirmed = engine.plan(peel_id, {
                    "action": "confirm",
                    "base_plan_version": selected["plan_version"],
                    "entry_id": entry_id})
                self.assertEqual(confirmed["entry"]["status"], "confirmed")
                locked = engine.plan(peel_id, {
                    "action": "lock",
                    "base_plan_version": confirmed["plan_version"],
                    "entry_id": entry_id})
                self.assertTrue(locked["entry"]["locked"])
                unlocked = engine.plan(peel_id, {
                    "action": "unlock",
                    "base_plan_version": locked["plan_version"],
                    "entry_id": entry_id})
                self.assertFalse(unlocked["entry"]["locked"])
                deferred = engine.plan(peel_id, {
                    "action": "defer",
                    "base_plan_version": unlocked["plan_version"],
                    "item_id": "it-8b7a6c5d4e3f2a10",
                    "reason_code": "not_relevant", "reason": "figure is fine"})
                self.assertEqual(deferred["resolution"]["name"], "defer")
                self.assertEqual(deferred["resolution"]["reason_code"],
                                 "not_relevant")
                self.assertTrue(cli.state()["resolutions"])
                # A stale base_plan_version is a conflict (exit 2).
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.plan(peel_id, {"action": "confirm",
                                          "base_plan_version": 0,
                                          "entry_id": entry_id})
                self.assertEqual(caught.exception.status, 409)
                self.assertIn("conflict", caught.exception.detail)

                plan_calls = [call["argv"] for call in cli.calls()
                              if call["argv"][0] == "plan"]
                self.assertEqual(plan_calls[0][1], peel_id)
                self.assertEqual(plan_calls[0][2], "select")
                self.assertIn("--input", plan_calls[0])
                self.assertIn("-", plan_calls[0])
                self.assertEqual(plan_calls[1][2], "confirm")
                self.assertIn("--entry", plan_calls[1])
                self.assertIn(entry_id, plan_calls[1])
                self.assertIn("--base-plan-version", plan_calls[1])
                defer_call = next(call for call in plan_calls
                                  if call[2] == "defer")
                self.assertIn("--input", defer_call)

    def test_plan_accept_and_clear_use_the_cp2_argv_and_stdin(self):
        """CP2 W9: accept goes via ``--input -``; clear via ``--entry``."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                accepted = engine.plan(peel_id, {
                    "action": "accept", "base_plan_version": 0,
                    "item_ids": ["it-3f9c2a4b6d8e0f1a",
                                 "it-8b7a6c5d4e3f2a10"]})
                self.assertEqual(accepted["plan_version"], 1)
                self.assertEqual(
                    [row["item_id"] for row in accepted["accepted"]],
                    ["it-3f9c2a4b6d8e0f1a", "it-8b7a6c5d4e3f2a10"])
                self.assertIsNone(accepted["refresh"])
                entry_id = accepted["accepted"][0]["entry_id"]
                cleared = engine.plan(peel_id, {
                    "action": "clear",
                    "base_plan_version": accepted["plan_version"],
                    "entry_id": entry_id})
                self.assertEqual(cleared["plan_version"], 2)
                self.assertEqual(cleared["cleared"]["entry_id"], entry_id)
                self.assertEqual(cleared["cleared"]["status_before"],
                                 "confirmed")
                self.assertIsNone(cleared["cleared"]["reverted_resolution"])
                # The fake CLI records the exact stdin request it received.
                self.assertEqual(cli.state()["accept_requests"][-1], {
                    "base_plan_version": 0,
                    "item_ids": ["it-3f9c2a4b6d8e0f1a",
                                 "it-8b7a6c5d4e3f2a10"]})
                self.assertEqual(cli.state()["clear_requests"][-1], {
                    "entry_id": entry_id, "base_plan_version": 1})
                plan_calls = [call["argv"] for call in cli.calls()
                              if call["argv"][0] == "plan"]
                accept_call = next(call for call in plan_calls
                                   if call[2] == "accept")
                self.assertIn("--input", accept_call)
                self.assertIn("-", accept_call)
                clear_call = next(call for call in plan_calls
                                  if call[2] == "clear")
                self.assertIn("--entry", clear_call)
                self.assertIn(entry_id, clear_call)
                self.assertIn("--base-plan-version", clear_call)
                self.assertEqual(
                    clear_call[clear_call.index("--base-plan-version") + 1],
                    "1")

    def test_review_report_passes_filters_via_stdin(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")["peel"]["peel_id"]
                report = engine.review_report(peel_id)
                self.assertEqual(report["status"], "ready")
                self.assertEqual(report["report_path"],
                                 "extract/review/report.json")
                self.assertEqual(report["report"]["status"], "ready")
                self.assertEqual(report["counts"]["suggestions"], 3)
                filtered = engine.review_report(
                    peel_id, item_ids=["it-3f9c2a4b6d8e0f1a"],
                    cluster_ids=["cl-0f1e2d3c4b5a"])
                self.assertTrue(filtered["report"]["clusters"])
                calls = [call["argv"] for call in cli.calls()
                         if call["argv"][0] == "review-report"]
                self.assertNotIn("--input", calls[0])
                self.assertIn("--input", calls[1])
                self.assertEqual(cli.state()["review_report_requests"][-1], {
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})

    def test_peelfeed_error_codes_map_to_http_statuses(self):
        cases = [
            ("busy", 423),
            ("conflict", 409),
            ("stale", 409),
            ("review_schema", 409),
            ("corrections_conflict", 409),
            ("validation", 400),
            ("peel_not_found", 404),
        ]
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                for code, status in cases:
                    cli.update(errors={"review": {
                        "code": code, "message": code + " failure"}})
                    with self.assertRaises(peelfeed.PeelfeedError) as caught:
                        engine.review(peel_id)
                    self.assertEqual(caught.exception.status, status, code)
                    self.assertIn(code, caught.exception.detail)
                # An unknown error code is a peelfeed failure (502).
                cli.update(errors={"review": {"code": "weird",
                                              "message": "boom"}})
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.review(peel_id)
                self.assertEqual(caught.exception.status, 502)
                # Exit code 3 (not found) is a 404 even without an error code.
                cli.update(errors={})
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.review("missing-peel")
                self.assertEqual(caught.exception.status, 404)
                self.assertIn("no such peel", caught.exception.detail)

    def test_nonzero_exit_is_a_typed_error_with_a_readable_reason(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"ingest_fail": True})
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    peelfeed.client().ingest(str(source))
                self.assertEqual(caught.exception.status, 502)
                self.assertIn("simulated ingest failure",
                              caught.exception.detail)

    def test_unknown_peel_maps_to_404(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    peelfeed.client().status("nope")
                self.assertEqual(caught.exception.status, 404)
                self.assertIn("no such peel", caught.exception.detail)

    def test_missing_executable_is_unavailable(self):
        with temp_data_root() as root:
            with peelfeed_env(CLI=str(root / "missing")):
                from peekseed import peelfeed
                with self.assertRaises(peelfeed.PeelfeedUnavailable):
                    peelfeed.client().capabilities()


class PeelfeedApiTests(unittest.TestCase):
    def test_upload_and_file_management_work_without_peelfeed(self):
        with temp_data_root() as root, peelfeed_env():
            client = _client()
            dataset, _pdf = _upload(client, root)
            sources = _sources(client, dataset["id"])
            self.assertEqual(sources[0]["status"], "uploaded")
            self.assertEqual(sources[0]["extraction"]["state"], "not_extracted")
            self.assertFalse(sources[0]["indexed"])
            extract = client.post("/api/datasets/%s/sources/%s/extract"
                                  % (dataset["id"], sources[0]["sha256"]))
            self.assertEqual(extract.status_code, 503, extract.text)
            detail = extract.json()["detail"]
            self.assertIn("extraction is unavailable", detail)
            self.assertIn("PEEKSEED_PEELFEED_CLI", detail)
            status = client.get("/api/peelfeed/status").json()
            self.assertFalse(status["config"]["configured"])
            self.assertFalse(status["handshake"]["available"])
            self.assertEqual(status["queue"]["states"]["failed"], 1)

            opened = client.get("/pdf/%s/%s" % (dataset["id"], "manual.pdf"))
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertIn("pdf", opened.headers.get("content-type", ""))
            self.assertEqual(opened.content[:4], b"%PDF")

            deleted = client.delete("/api/datasets/%s/sources/%s"
                                    % (dataset["id"], sources[0]["sha256"]))
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(_sources(client, dataset["id"]), [])

    def test_extract_polls_queued_running_extracted(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"ingest_seconds": 1.0,
                                                  "ingest_pages": 2,
                                                  "ingest_review_items": 4})
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                extract = client.post(
                    "/api/datasets/%s/sources/%s/extract" % (dataset["id"], sha),
                    json={"llm_mode": "off"})
                self.assertEqual(extract.status_code, 200, extract.text)
                body = extract.json()
                self.assertEqual(body["peel"]["state"], "queued")
                self.assertEqual(body["source"]["extraction"]["state"], "queued")
                self.assertEqual(body["capabilities"]["version"], "0.1.0")

                # While `peelfeed ingest` is still running, the status poller
                # moves the chip from queued to running.
                deadline = time.time() + 5.0
                observed = None
                while time.time() < deadline:
                    rows = _sources(client, dataset["id"])
                    observed = rows[0]["extraction"]["state"]
                    if observed in ("running", "extracted", "failed"):
                        break
                    time.sleep(0.02)
                self.assertEqual(observed, "running", rows[0]["extraction"])

                row = _wait_source(client, dataset["id"], sha)
                extraction = row["extraction"]
                self.assertEqual(extraction["state"], "extracted", row)
                self.assertEqual(extraction["error"], None)
                self.assertEqual(len(extraction["conversion_id"]), 64)
                self.assertEqual(extraction["validation"]["errors"], 0)
                self.assertEqual(extraction["validation"]["review_items"], 4)
                self.assertEqual(row["pages"], 2)
                self.assertEqual(row["status"], "extracted")
                self.assertTrue(extraction["peel_id"])
                self.assertTrue(extraction["started_at"])
                self.assertTrue(extraction["finished_at"])
                commands = cli.commands()
                self.assertIn("ingest", commands)
                self.assertGreater(commands.count("status"), 1)

    def test_extract_reuses_an_existing_peel_by_the_sha8_suffix(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, pdf = _upload(client, root)
                sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
                peel_id = "cxl-" + sha[:8]
                cli.update(peels={peel_id: {
                    "peel_id": peel_id, "state": "ready",
                    "source": {"name": "manual.pdf", "size": 1, "sha256": sha,
                               "pages": 2},
                    "profile": "generic",
                    "conversion_id": hashlib.sha256(b"conv").hexdigest(),
                    "counts": {"pages": 2, "chunks": 2},
                    "validation": {"errors": [], "review_items": 0},
                    "review_items": 0, "error": None,
                }}, ingest_peel_id=peel_id, ingest_reused=True)
                extract = client.post(
                    "/api/datasets/%s/sources/%s/extract"
                    % (dataset["id"], sha), json={})
                self.assertEqual(extract.status_code, 200, extract.text)
                body = extract.json()
                self.assertTrue(body["peel"]["reused"], body["peel"])
                self.assertEqual(body["peel"]["peel_id"], peel_id)
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "extracted")
                self.assertEqual(row["extraction"]["peel_id"], peel_id)

    def test_ingest_failure_marks_the_source_failed_with_a_readable_error(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"ingest_fail": True})
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                extract = client.post(
                    "/api/datasets/%s/sources/%s/extract" % (dataset["id"], sha),
                    json={})
                self.assertEqual(extract.status_code, 200, extract.text)
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "failed")
                self.assertIn("simulated ingest failure",
                              row["extraction"]["error"])
                self.assertEqual(row["status"], "failed")
                self.assertTrue(row["present"])

    def test_extract_unavailable_marks_the_source_failed_and_keeps_uploads(self):
        with temp_data_root() as root:
            with peelfeed_env(CLI=str(root / "missing"),
                              TIMEOUT="1"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                response = client.post(
                    "/api/datasets/%s/sources/%s/extract" % (dataset["id"], sha),
                    json={})
                self.assertEqual(response.status_code, 503, response.text)
                sources = _sources(client, dataset["id"])
                self.assertEqual(sources[0]["extraction"]["state"], "failed")
                self.assertIn("unavailable", sources[0]["extraction"]["error"])
                self.assertTrue(sources[0]["present"])

    def test_review_routes_proxy_the_schema_2_payload(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                row = _wait_source(client, dataset["id"], sha)
                peel_id = row["extraction"]["peel_id"]

                review = client.get("/api/datasets/%s/sources/%s/review"
                                    % (dataset["id"], sha))
                self.assertEqual(review.status_code, 200, review.text)
                payload = review.json()
                self.assertEqual(payload["review_schema"], 2)
                self.assertEqual(payload["peel_id"], peel_id)
                self.assertEqual(payload["counts"]["clusters"], 3)
                self.assertEqual(payload["counts"]["human_pending"], 2)
                self.assertEqual(len(payload["items"]), 3)
                self.assertEqual(len(payload["clusters"]), 3)
                self.assertEqual(payload["clusters"][0]["strategies"][0]["id"],
                                 "use_first_row_header")
                self.assertEqual(payload["items"][0]["id"],
                                 "it-3f9c2a4b6d8e0f1a")

                filtered = client.get(
                    "/api/datasets/%s/sources/%s/review?group=code"
                    "&status=needs_decision" % (dataset["id"], sha))
                self.assertEqual(filtered.status_code, 200, filtered.text)
                self.assertEqual([item["id"] for item in filtered.json()["items"]],
                                 ["it-3f9c2a4b6d8e0f1a"])
                review_calls = [call["argv"] for call in cli.calls()
                                if call["argv"][0] == "review"]
                self.assertIn("--group", review_calls[-1])
                self.assertIn("code", review_calls[-1])
                self.assertIn("--status", review_calls[-1])
                self.assertIn("needs_decision", review_calls[-1])

                bad_group = client.get(
                    "/api/datasets/%s/sources/%s/review?group=nope"
                    % (dataset["id"], sha))
                self.assertEqual(bad_group.status_code, 400, bad_group.text)

                detail = client.get(
                    "/api/datasets/%s/sources/%s/review/items/%s"
                    % (dataset["id"], sha, "it-3f9c2a4b6d8e0f1a"))
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["item"]["id"],
                                 "it-3f9c2a4b6d8e0f1a")
                self.assertEqual(detail.json()["peel_id"], peel_id)
                missing = client.get(
                    "/api/datasets/%s/sources/%s/review/items/%s"
                    % (dataset["id"], sha, "it-0000000000000000"))
                self.assertEqual(missing.status_code, 404, missing.text)

    def test_review_plan_actions_round_trip(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                route = ("/api/datasets/%s/sources/%s/review/plan"
                         % (dataset["id"], sha))
                selected = client.post(route, json={
                    "base_plan_version": 0,
                    "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}, "notes": "pick"}})
                self.assertEqual(selected.status_code, 200, selected.text)
                plan = selected.json()
                self.assertEqual(plan["plan_version"], 1)
                self.assertEqual(plan["entry"]["item_id"],
                                 "it-3f9c2a4b6d8e0f1a")
                self.assertEqual(plan["entry"]["status"], "draft")
                self.assertEqual(plan["entry"]["notes"], "pick")
                entry_id = plan["entry"]["entry_id"]
                self.assertTrue(entry_id.startswith("e-"))

                confirmed = client.post(route, json={
                    "base_plan_version": plan["plan_version"],
                    "action": "confirm", "entry_id": entry_id})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["entry"]["status"],
                                 "confirmed")
                locked = client.post(route, json={
                    "base_plan_version": confirmed.json()["plan_version"],
                    "action": "lock", "entry_id": entry_id})
                self.assertEqual(locked.status_code, 200, locked.text)
                self.assertTrue(locked.json()["entry"]["locked"])
                unlocked = client.post(route, json={
                    "base_plan_version": locked.json()["plan_version"],
                    "action": "unlock", "entry_id": entry_id})
                self.assertEqual(unlocked.status_code, 200, unlocked.text)
                self.assertFalse(unlocked.json()["entry"]["locked"])
                deferred = client.post(route, json={
                    "base_plan_version": unlocked.json()["plan_version"],
                    "action": "defer", "item_id": "it-8b7a6c5d4e3f2a10",
                    "reason_code": "not_relevant", "reason": "figure is fine"})
                self.assertEqual(deferred.status_code, 200, deferred.text)
                body = deferred.json()
                self.assertIn("entries", body)
                self.assertEqual(body["resolution"]["name"], "defer")
                self.assertEqual(body["resolution"]["reason_code"],
                                 "not_relevant")
                self.assertEqual(cli.state()["resolutions"][-1]["name"], "defer")

    def test_review_plan_r0_confirm_applies_a_resolution(self):
        """CP1.1: an R0 resolution strategy is applied on confirm (not confirmed)."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "confirm_no_header",
                                           "version": 1}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                plan = selected.json()
                entry = plan["entry"]
                self.assertEqual(entry["action"], {
                    "type": "resolution", "name": "confirm_headerless"})
                self.assertEqual(entry["status"], "draft")
                self.assertIsNone(plan["resolution"])

                confirmed = client.post(route, json={
                    "base_plan_version": plan["plan_version"],
                    "action": "confirm", "entry_id": entry["entry_id"]})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                body = confirmed.json()
                self.assertEqual(body["entry"]["status"], "applied")
                resolution = body["resolution"]
                self.assertEqual(resolution["name"], "confirm_headerless")
                self.assertEqual(resolution["item_id"],
                                 "it-3f9c2a4b6d8e0f1a")
                self.assertEqual(resolution["change"], "add")
                state = cli.state()
                self.assertEqual(state["plan"]["entries"][0]["status"],
                                 "applied")
                self.assertEqual(state["resolutions"][-1]["name"],
                                 "confirm_headerless")

    def test_review_plan_operation_confirm_waits_for_a_batch(self):
        """A corpus operation stays ``confirmed``; it is not applied immediately."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry = selected.json()["entry"]
                self.assertEqual(entry["action"]["type"], "operation")
                self.assertEqual(entry["action"]["name"], "set_header_rows")
                confirmed = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "confirm", "entry_id": entry["entry_id"]})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["entry"]["status"],
                                 "confirmed")
                self.assertIsNone(confirmed.json()["resolution"])
                self.assertFalse(cli.state().get("resolutions"))

    def test_review_plan_locked_entry_defer_is_a_conflict(self):
        """CP1.1 A3: deferring a locked entry is a 409 and keeps the entry."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry_id = selected.json()["entry"]["entry_id"]
                locked = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "lock", "entry_id": entry_id})
                self.assertEqual(locked.status_code, 200, locked.text)
                locked_version = locked.json()["plan_version"]
                deferred = client.post(route, json={
                    "base_plan_version": locked_version, "action": "defer",
                    "item_id": "it-3f9c2a4b6d8e0f1a",
                    "reason_code": "not_relevant"})
                self.assertEqual(deferred.status_code, 409, deferred.text)
                self.assertIn("conflict", deferred.json()["detail"])
                self.assertFalse(cli.state().get("resolutions"))
                # The entry is still there; unlock returns the full plan.
                unlocked = client.post(route, json={
                    "base_plan_version": locked_version, "action": "unlock",
                    "entry_id": entry_id})
                self.assertEqual(unlocked.status_code, 200, unlocked.text)
                self.assertIn(entry_id,
                              [row["entry_id"]
                               for row in unlocked.json()["entries"]])
                self.assertFalse(unlocked.json()["entry"]["locked"])

    def test_review_plan_accept_round_trip(self):
        """CP2 W9: accept adopts each ready suggestion, bumps version once."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                accepted = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a",
                                 "it-8b7a6c5d4e3f2a10"]})
                self.assertEqual(accepted.status_code, 200, accepted.text)
                plan = accepted.json()
                self.assertEqual(plan["plan_version"], 1)
                self.assertIsNone(plan["entry"])
                self.assertIsNone(plan["resolution"])
                self.assertIsNone(plan["refresh"])
                rows = {row["item_id"]: row for row in plan["accepted"]}
                self.assertEqual(set(rows), {"it-3f9c2a4b6d8e0f1a",
                                             "it-8b7a6c5d4e3f2a10"})
                operation = rows["it-3f9c2a4b6d8e0f1a"]
                self.assertEqual(set(operation), {
                    "item_id", "entry_id", "strategy", "action", "status",
                    "revision", "replaced_existing"})
                self.assertEqual(operation["status"], "confirmed")
                self.assertEqual(operation["revision"], 1)
                self.assertFalse(operation["replaced_existing"])
                self.assertEqual(operation["action"], {
                    "type": "operation", "name": "set_header_rows",
                    "params": {"rows": [0]}})
                self.assertEqual(operation["strategy"],
                                 {"id": "use_first_row_header", "version": 1})
                resolution = rows["it-8b7a6c5d4e3f2a10"]
                self.assertEqual(resolution["status"], "applied")
                self.assertEqual(resolution["action"], {
                    "type": "resolution", "name": "keep_visual_fallback"})
                self.assertEqual(cli.state()["resolutions"][-1]["name"],
                                 "keep_visual_fallback")
                # The exact stdin request is the frozen wire §2.6 accept shape.
                self.assertEqual(cli.state()["accept_requests"][-1], {
                    "base_plan_version": 0,
                    "item_ids": ["it-3f9c2a4b6d8e0f1a",
                                 "it-8b7a6c5d4e3f2a10"]})
                # Accepting the same item again replaces its entry: revision +1,
                # still exactly one plan_version bump.
                again = client.post(route, json={
                    "base_plan_version": plan["plan_version"],
                    "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"]})
                self.assertEqual(again.status_code, 200, again.text)
                self.assertEqual(again.json()["plan_version"], 2)
                replaced = again.json()["accepted"][0]
                self.assertTrue(replaced["replaced_existing"])
                self.assertEqual(replaced["revision"], 2)

    def test_review_plan_accept_is_all_or_nothing_with_per_item_reasons(self):
        """CP2 W9: any failing item fails the whole accept with reasons."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                default_report = client.post(base + "/review/reports",
                                             json={}).json()
                failed = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a",
                                 "it-5a7c8b9d0e1f2a3b",
                                 "it-0000000000000000"]})
                self.assertEqual(failed.status_code, 400, failed.text)
                detail = failed.json()["detail"]
                self.assertIn("validation", detail)
                self.assertIn("it-5a7c8b9d0e1f2a3b=stale", detail)
                self.assertIn("it-0000000000000000=item_missing", detail)
                # All-or-nothing: the good item was not accepted either.
                state = cli.state()
                plan_state = state.get("plan") or {}
                self.assertEqual(plan_state.get("plan_version", 0), 0)
                self.assertFalse(plan_state.get("entries"))
                self.assertFalse(state.get("resolutions"))
                # no_ready_suggestion when the report has no ready candidate.
                report = json.loads(json.dumps(default_report))
                report["report"]["clusters"] = report["report"]["clusters"][:1]
                cli.update(review_report=report)
                missing = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": ["it-8b7a6c5d4e3f2a10"]})
                self.assertEqual(missing.status_code, 400, missing.text)
                self.assertIn("it-8b7a6c5d4e3f2a10=no_ready_suggestion",
                              missing.json()["detail"])
                # validator_failed when the item's only candidate failed check.
                review = client.get(base + "/review").json()
                stale_item = review["items"][2]
                stale_item["stale"] = False
                stale_item["status"] = "needs_decision"
                stale_item["entry"] = None
                cli.update(review=review, review_report=default_report)
                invalid = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": ["it-5a7c8b9d0e1f2a3b"]})
                self.assertEqual(invalid.status_code, 400, invalid.text)
                self.assertIn("it-5a7c8b9d0e1f2a3b=validator_failed",
                              invalid.json()["detail"])

    def test_review_plan_accept_locked_and_409_busy(self):
        """CP2 W9: a locked item is an accept reason; stale base/busy map 409/423."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry_id = selected.json()["entry"]["entry_id"]
                locked = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "lock", "entry_id": entry_id})
                self.assertEqual(locked.status_code, 200, locked.text)
                version = locked.json()["plan_version"]
                rejected = client.post(route, json={
                    "base_plan_version": version, "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"]})
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertIn("it-3f9c2a4b6d8e0f1a=locked",
                              rejected.json()["detail"])
                # The plan is unchanged: the locked entry is still there.
                self.assertEqual(cli.state()["plan"]["plan_version"], version)
                self.assertTrue(cli.state()["plan"]["entries"][0]["locked"])
                # A stale base_plan_version is a 409 conflict.
                stale = client.post(route, json={
                    "base_plan_version": 99, "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"]})
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertIn("conflict", stale.json()["detail"])
                # A busy review lock is 423, not a 400/502.
                cli.update(errors={"plan": {"code": "busy",
                                            "message": "review lock held"}})
                busy = client.post(route, json={
                    "base_plan_version": version, "action": "accept",
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"]})
                self.assertEqual(busy.status_code, 423, busy.text)
                self.assertIn("busy", busy.json()["detail"])

    def test_review_plan_clear_round_trip_and_r0_revert(self):
        """CP2 W9: clear deletes an entry; an applied R0 entry is reverted."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                entry_id = selected.json()["entry"]["entry_id"]
                confirmed = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "confirm", "entry_id": entry_id})
                self.assertEqual(confirmed.json()["entry"]["status"],
                                 "confirmed")
                cleared = client.post(route, json={
                    "base_plan_version": confirmed.json()["plan_version"],
                    "action": "clear", "entry_id": entry_id})
                self.assertEqual(cleared.status_code, 200, cleared.text)
                plan = cleared.json()
                self.assertEqual(plan["plan_version"], 3)
                self.assertIsNone(plan["entry"])
                self.assertIsNone(plan["resolution"])
                self.assertIsNone(plan["refresh"])
                self.assertEqual(set(plan["cleared"]), {
                    "entry_id", "item_id", "status_before",
                    "reverted_resolution"})
                self.assertEqual(plan["cleared"]["entry_id"], entry_id)
                self.assertEqual(plan["cleared"]["item_id"],
                                 "it-3f9c2a4b6d8e0f1a")
                self.assertEqual(plan["cleared"]["status_before"],
                                 "confirmed")
                self.assertIsNone(plan["cleared"]["reverted_resolution"])
                self.assertFalse(plan["entries"])
                # R0: clear an applied resolution entry writes a revert row.
                selected = client.post(route, json={
                    "base_plan_version": plan["plan_version"],
                    "action": "select",
                    "entry": {"item_id": "it-8b7a6c5d4e3f2a10",
                              "strategy": {"id": "keep_visual_fallback",
                                           "version": 1}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry = selected.json()["entry"]
                self.assertEqual(entry["status"], "draft")
                confirmed = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "confirm", "entry_id": entry["entry_id"]})
                self.assertEqual(confirmed.json()["entry"]["status"],
                                 "applied")
                add_row = confirmed.json()["resolution"]
                cleared = client.post(route, json={
                    "base_plan_version": confirmed.json()["plan_version"],
                    "action": "clear", "entry_id": entry["entry_id"]})
                self.assertEqual(cleared.status_code, 200, cleared.text)
                reverted = cleared.json()["cleared"]["reverted_resolution"]
                self.assertIsNotNone(reverted)
                self.assertEqual(reverted["change"], "revert")
                self.assertEqual(reverted["supersedes"],
                                 add_row["resolution_id"])
                self.assertEqual(reverted["item_id"],
                                 "it-8b7a6c5d4e3f2a10")
                resolutions = cli.state()["resolutions"]
                self.assertEqual([row["change"] for row in resolutions],
                                 ["add", "revert"])

    def test_review_plan_clear_conflicts_and_not_found(self):
        """CP2 W9: locked/batch clear is 409; unknown entry is 404."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                entry_id = selected.json()["entry"]["entry_id"]
                locked = client.post(route, json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "lock", "entry_id": entry_id})
                rejected = client.post(route, json={
                    "base_plan_version": locked.json()["plan_version"],
                    "action": "clear", "entry_id": entry_id})
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertIn("conflict", rejected.json()["detail"])
                self.assertTrue(cli.state()["plan"]["entries"][0]["locked"])
                # A batch-bound entry is read-only -> conflict.
                review = client.get(base + "/review").json()
                batch_entry = json.loads(json.dumps(
                    review["items"][2]["entry"]))
                batch_entry["batch_id"] = "batch-1"
                cli.update(plan={
                    "peel_id": "peel", "review_schema": 2, "plan_version": 0,
                    "updated_at": "2026-11-05T00:00:00Z",
                    "entries": [batch_entry], "entry": None,
                    "resolution": None})
                batch = client.post(route, json={
                    "base_plan_version": 0, "action": "clear",
                    "entry_id": batch_entry["entry_id"]})
                self.assertEqual(batch.status_code, 409, batch.text)
                self.assertIn("conflict", batch.json()["detail"])
                # An unknown entry is a 404 (peelfeed exit code 3).
                unknown = client.post(route, json={
                    "base_plan_version": 0, "action": "clear",
                    "entry_id": "e-0000000000000000"})
                self.assertEqual(unknown.status_code, 404, unknown.text)

    def test_review_plan_accept_clear_request_validation(self):
        """CP2 W9: peekseed rejects malformed accept/clear bodies with 400."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                cli.clear_calls()
                cases = [
                    ({"base_plan_version": 0, "action": "accept"}, "item_ids"),
                    ({"base_plan_version": 0, "action": "accept",
                      "item_ids": []}, "item_ids"),
                    ({"base_plan_version": 0, "action": "accept",
                      "item_ids": "it-x"}, "item_ids"),
                    ({"base_plan_version": 0, "action": "accept",
                      "item_ids": ["it-x", "it-x"]}, "unique"),
                    ({"base_plan_version": 0, "action": "accept",
                      "item_ids": [7]}, "strings"),
                    ({"base_plan_version": 0, "action": "clear"}, "entry_id"),
                    ({"base_plan_version": 0, "action": "explode"},
                     "action must be one of"),
                ]
                for body, marker in cases:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn(marker, response.json()["detail"])
                self.assertNotIn("plan", cli.commands())

    def test_review_plan_confirm_refresh_matches_wire(self):
        """CP2 W9/E4: refresh before/after + revision rules match wire §2.7."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                review = client.get(base + "/review").json()
                stale_entry = json.loads(json.dumps(
                    review["items"][2]["entry"]))
                self.assertTrue(stale_entry["stale"])
                stale_plan = {"peel_id": "peel", "review_schema": 2,
                              "plan_version": 0,
                              "updated_at": "2026-11-05T00:00:00Z",
                              "entries": [stale_entry], "entry": None,
                              "resolution": None}
                cli.update(plan=stale_plan)
                confirmed = client.post(route, json={
                    "base_plan_version": 0, "action": "confirm",
                    "entry_id": stale_entry["entry_id"]})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                plan = confirmed.json()
                self.assertEqual(plan["plan_version"], 1)
                refresh = plan["refresh"]
                self.assertEqual(set(refresh), {
                    "changed", "revision_before", "revision_after",
                    "action_params_before", "action_params_after",
                    "source_refs_before", "source_refs_after"})
                self.assertTrue(refresh["changed"])
                self.assertEqual(refresh["revision_before"], 2)
                self.assertEqual(refresh["revision_after"], 3)
                self.assertEqual(
                    refresh["action_params_before"]["parent_header_cells"],
                    [["bits", "variable"]])
                self.assertEqual(
                    refresh["action_params_after"]["parent_header_cells"],
                    [["Bits", "Variable"]])
                self.assertEqual(plan["entry"]["revision"], 3)
                self.assertEqual(plan["entry"]["status"], "confirmed")
                self.assertFalse(plan["entry"]["stale"])
                # Hash-only evidence change: same cells, no revision bump.
                hash_review = json.loads(json.dumps(review))
                stale_item = hash_review["items"][2]
                candidate = stale_item["parent_candidates"][0]
                candidate["header_cells"] = [["bits", "variable"]]
                candidate["source_refs"][0]["cells"] = ["bits", "variable"]
                candidate["source_refs"][0]["evidence_hash"] = "ev-p1041-new"
                cli.update(plan=json.loads(json.dumps(stale_plan)),
                           review=hash_review)
                hash_only = client.post(route, json={
                    "base_plan_version": 0, "action": "confirm",
                    "entry_id": stale_entry["entry_id"]})
                self.assertEqual(hash_only.status_code, 200, hash_only.text)
                refresh = hash_only.json()["refresh"]
                self.assertFalse(refresh["changed"])
                self.assertEqual(refresh["revision_before"], 2)
                self.assertEqual(refresh["revision_after"], 2)
                self.assertEqual(hash_only.json()["entry"]["revision"], 2)
                # A non-refreshable stale reason is a conflict, no plan write.
                blocked_plan = json.loads(json.dumps(stale_plan))
                blocked_plan["entries"][0]["stale_reasons"] = [
                    "strategy_changed"]
                cli.update(plan=blocked_plan)
                conflict = client.post(route, json={
                    "base_plan_version": 0, "action": "confirm",
                    "entry_id": stale_entry["entry_id"]})
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn("conflict", conflict.json()["detail"])

    def test_review_payload_exposes_converter_code_hash_only(self):
        """CP1.1: derived_from names the diagnostic converter hash, not the engine."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                review = client.get(base + "/review")
                self.assertEqual(review.status_code, 200, review.text)
                derived = review.json()["derived_from"]
                self.assertEqual(derived["converter_code_hash"],
                                 "d6ef5d97b7865966")
                self.assertNotIn("engine_fingerprint", derived)
                self.assertEqual(derived["derivation_version"], 1)

    def test_review_plan_inherit_select_keeps_strategy_and_parent_params(self):
        """CP1.1: inherit select passes strategy.id and fixed parent params through."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                params = {
                    "parent_asset_id": "table-p1041-01",
                    "parent_header_rows": [0],
                    "parent_header_cells": [["Bits", "Variable"]],
                    "parent_source_refs": [{
                        "segment": "table-p1041-01",
                        "source_row": 0,
                        "cells": ["Bits", "Variable"],
                        "evidence_hash": "ev-p1041"},
                    ],
                    "parent_header_source": "native",
                }
                selected = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "inherit_parent_header",
                                           "version": 1},
                              "params": params}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry = selected.json()["entry"]
                self.assertEqual(entry["strategy"],
                                 {"id": "inherit_parent_header", "version": 1})
                self.assertEqual(entry["action"], {
                    "type": "operation", "name": "inherit_header_from",
                    "params": params})

    def test_review_plan_error_mapping(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                from peekseed import peelfeed
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                route = ("/api/datasets/%s/sources/%s/review/plan"
                         % (dataset["id"], sha))
                # peekseed-side validation -> 400
                missing_base = client.post(route, json={
                    "action": "select",
                    "entry": {"item_id": "it-x",
                              "strategy": {"id": "s", "version": 1}}})
                self.assertEqual(missing_base.status_code, 400, missing_base.text)
                bad_action = client.post(route, json={
                    "base_plan_version": 0, "action": "explode"})
                self.assertEqual(bad_action.status_code, 400, bad_action.text)
                no_entry = client.post(route, json={
                    "base_plan_version": 0, "action": "select"})
                self.assertEqual(no_entry.status_code, 400, no_entry.text)
                bad_reason = client.post(route, json={
                    "base_plan_version": 0, "action": "defer",
                    "item_id": "it-x", "reason_code": "because"})
                self.assertEqual(bad_reason.status_code, 400, bad_reason.text)
                # A stale base_plan_version is a conflict -> 409
                stale = client.post(route, json={
                    "base_plan_version": 9, "action": "confirm",
                    "entry_id": "e-abc"})
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertIn("conflict", stale.json()["detail"])
                # peelfeed validation -> 400; busy -> 423
                cli.update(errors={"plan": {"code": "validation",
                                            "message": "bad params"}})
                invalid = client.post(route, json={
                    "base_plan_version": 0, "action": "confirm",
                    "entry_id": "e-abc"})
                self.assertEqual(invalid.status_code, 400, invalid.text)
                cli.update(errors={"plan": {"code": "busy",
                                            "message": "review lock held"}})
                busy = client.post(route, json={
                    "base_plan_version": 0, "action": "confirm",
                    "entry_id": "e-abc"})
                self.assertEqual(busy.status_code, 423, busy.text)
                self.assertIn("busy", busy.json()["detail"])
                # A review-schema mismatch refuses every review call -> 409
                cli.update(errors={}, capabilities={"review_schema": 1})
                peelfeed.invalidate_handshake()
                mismatch = client.get("/api/datasets/%s/sources/%s/review"
                                      % (dataset["id"], sha))
                self.assertEqual(mismatch.status_code, 409, mismatch.text)
                self.assertIn("review schema", mismatch.json()["detail"])
                report = client.post(
                    "/api/datasets/%s/sources/%s/review/reports"
                    % (dataset["id"], sha), json={})
                self.assertEqual(report.status_code, 409, report.text)

    def test_review_plan_validation_maps_to_400_not_502(self):
        """CP2 E1: peelfeed ``error [validation]`` (exit 2) on plan is a 400.

        The review plan route must not turn a strategy-param validation failure
        into a 502: the adapter maps the typed ``validation`` code to 400. A
        malformed ``params`` shape is rejected by peekseed itself with 400 and
        never reaches the CLI.
        """
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                cli.clear_calls()
                bad_shape = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_header_rows",
                                           "version": 1},
                              "params": ["rows", 0]}})
                self.assertEqual(bad_shape.status_code, 400, bad_shape.text)
                self.assertIn("params", bad_shape.json()["detail"])
                self.assertNotIn("plan", cli.commands())
                cli.update(errors={"plan": {
                    "code": "validation", "exit": 2,
                    "message": "params invalid: rows must be 0-based",
                    "detail": {"reasons": ["rows[0] must be >= 0"]}}})
                invalid = client.post(route, json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_header_rows",
                                           "version": 1},
                              "params": {"rows": [-1]}}})
                self.assertEqual(invalid.status_code, 400, invalid.text)
                self.assertNotEqual(invalid.status_code, 502)
                detail = invalid.json()["detail"]
                self.assertIn("validation", detail)
                self.assertIn("rows must be 0-based", detail)
                self.assertIn("rows[0] must be >= 0", detail)

    def test_review_payload_carries_parent_candidates_and_stale_reasons(self):
        """CP2 E2/E3: the proxied payload exposes the fields the UI reads."""
        with temp_data_root() as root:
            item = {
                "id": "it-3f9c2a4b6d8e0f1a",
                "code": "table_header_absent",
                "kind": "table",
                "status": "confirmed",
                "stale": True,
                "parent_candidates": [{
                    "asset_id": "table-p1041-01",
                    "pages": [1041],
                    "col_count": 2,
                    "header_rows": [0],
                    "header_cells": [["Bits", "Variable"]],
                    "header_signature": "bits|variable",
                    "source_refs": [{"segment": "table-p1041-01",
                                     "source_row": 0,
                                     "cells": ["Bits", "Variable"],
                                     "evidence_hash": "ev-p1041"}],
                    "parent_evidence_hash": "ev-p1041",
                    "header_source": "native",
                }],
                "entry": {
                    "entry_id": "e-3f9c2a4b6d8e0f1a",
                    "item_id": "it-3f9c2a4b6d8e0f1a",
                    "status": "confirmed",
                    "stale": True,
                    "stale_reasons": ["evidence_changed", "parent_changed"],
                    "locked": True,
                    "strategy": {"id": "inherit_parent_header", "version": 1},
                    "action": {"type": "operation",
                               "name": "inherit_header_from",
                               "params": {
                                   "parent_asset_id": "table-p1041-01",
                                   "parent_header_rows": [0],
                                   "parent_header_cells": [["Bits", "Variable"]],
                                   "parent_source_refs": [],
                                   "parent_header_source": "native"}},
                    "evidence": {"hash": "9c1f2e3d4b5a6978", "refs": []},
                    "dependencies": [],
                    "capability_snapshot": {"native_lines": True, "ocr": None,
                                            "vlm": None},
                    "source": "user", "revision": 2, "batch_id": None,
                    "notes": None,
                },
            }
            review = {"review_schema": 2, "counts": {}, "clusters": [],
                      "items": [item]}
            cli = FakePeelfeedCli(root / "fake", {"review": review})
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                payload = client.get(base + "/review").json()
                item = payload["items"][0]
                candidate = item["parent_candidates"][0]
                self.assertEqual(candidate["asset_id"], "table-p1041-01")
                self.assertEqual(candidate["pages"], [1041])
                self.assertEqual(candidate["header_cells"],
                                 [["Bits", "Variable"]])
                self.assertEqual(candidate["header_source"], "native")
                entry = item["entry"]
                self.assertTrue(entry["locked"])
                self.assertTrue(entry["stale"])
                self.assertEqual(entry["stale_reasons"],
                                 ["evidence_changed", "parent_changed"])
                # The served UI carries the handlers that read these fields.
                page = client.get("/").text
                self.assertIn("parent_candidates", page)
                self.assertIn("stale_reasons", page)

    def test_review_reports_are_synchronous_and_accept_filters(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                route = ("/api/datasets/%s/sources/%s/review/reports"
                         % (dataset["id"], sha))
                report = client.post(route, json={})
                self.assertEqual(report.status_code, 200, report.text)
                payload = report.json()
                self.assertEqual(payload["status"], "ready")
                self.assertEqual(payload["report_path"],
                                 "extract/review/report.json")
                self.assertEqual(payload["report"]["status"], "ready")
                self.assertEqual(payload["counts"]["suggestions"], 3)
                self.assertTrue(payload["report"]["clusters"][0]["suggestions"])
                filtered = client.post(route, json={
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})
                self.assertEqual(filtered.status_code, 200, filtered.text)
                self.assertEqual(cli.state()["review_report_requests"][-1], {
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})
                bad = client.post(route, json={"item_ids": "it-x"})
                self.assertEqual(bad.status_code, 400, bad.text)
                # peelfeed failure maps to 400/502, never a silent success
                cli.update(errors={"review-report": {
                    "code": "validation", "message": "bad report request"}})
                failed = client.post(route, json={})
                self.assertEqual(failed.status_code, 400, failed.text)

    def test_old_decide_and_rebuild_routes_are_gone(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                decide = client.post(
                    "/api/datasets/%s/sources/%s/review/it-3f9c2a4b6d8e0f1a"
                    % (dataset["id"], sha), json={"decision": "accept"})
                self.assertEqual(decide.status_code, 404, decide.text)
                rebuild = client.post(
                    "/api/datasets/%s/sources/%s/rebuild"
                    % (dataset["id"], sha), json={"reasons": ["decisions"]})
                self.assertEqual(rebuild.status_code, 404, rebuild.text)
                self.assertNotIn("decide", cli.commands())

    def test_events_assets_and_delete(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake", {"ingest_pages": 2})
            peel_root = _shared(root) / "peels"
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                row = _wait_source(client, dataset["id"], sha)
                peel_id = row["extraction"]["peel_id"]

                event = client.post(
                    "/api/datasets/%s/sources/%s/events"
                    % (dataset["id"], sha),
                    json={"event": "embed", "generation": "g1"})
                self.assertEqual(event.status_code, 200, event.text)
                self.assertTrue(event.json()["ok"])
                self.assertEqual(cli.state()["events"][-1]["event"], "embed")
                self.assertEqual(cli.state()["events"][-1]["data"]["generation"],
                                 "g1")

                # Assets are served from disk: corpus-relative and peel-relative.
                png = b"\x89PNG\r\n\x1a\nfake-image"
                corpus_image = peel_root / peel_id / "extract" / "corpus" / "images"
                corpus_image.mkdir(parents=True, exist_ok=True)
                (corpus_image / "fig_1_1.png").write_bytes(png)
                peel_table = peel_root / peel_id / "tables"
                peel_table.mkdir(parents=True, exist_ok=True)
                (peel_table / "table-1-1.json").write_text("{}", encoding="utf-8")
                asset = client.get("/api/datasets/%s/sources/%s/assets/%s"
                                   % (dataset["id"], sha,
                                      "images/fig_1_1.png"))
                self.assertEqual(asset.status_code, 200, asset.text)
                self.assertEqual(asset.content, png)
                self.assertEqual(asset.headers.get("content-type"), "image/png")
                table = client.get("/api/datasets/%s/sources/%s/assets/%s"
                                   % (dataset["id"], sha,
                                      "tables/table-1-1.json"))
                self.assertEqual(table.status_code, 200, table.text)
                missing = client.get("/api/datasets/%s/sources/%s/assets/%s"
                                     % (dataset["id"], sha, "images/nope.png"))
                self.assertEqual(missing.status_code, 404)
                outside = root / "outside.png"
                outside.write_bytes(png)
                traversal = client.get(
                    "/api/datasets/%s/sources/%s/assets/..%%2Foutside.png"
                    % (dataset["id"], sha))
                self.assertIn(traversal.status_code, (403, 404))

                deleted = client.delete(
                    "/api/datasets/%s/sources/%s?peel=delete"
                    % (dataset["id"], sha))
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()["peel"]["action"], "archived")
                self.assertIn("archive", cli.commands())
                self.assertEqual(_sources(client, dataset["id"]), [])

    def test_delete_keeps_the_peel_by_default(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                deleted = client.delete(
                    "/api/datasets/%s/sources/%s" % (dataset["id"], sha)).json()
                self.assertEqual(deleted["peel"]["action"], "kept")
                self.assertNotIn("archive", cli.commands())

    def test_status_reports_config_capabilities_and_queue(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                _wait_source(client, dataset["id"], sha)
                status = client.get("/api/peelfeed/status").json()
                self.assertTrue(status["config"]["configured"])
                self.assertEqual(status["config"]["cli"], str(cli.path))
                self.assertTrue(status["handshake"]["available"])
                self.assertEqual(status["handshake"]["version"], "0.1.0")
                self.assertEqual(status["queue"]["count"], 1)
                self.assertEqual(status["queue"]["states"]["extracted"], 1)
                text = json.dumps(status)
                self.assertNotIn("base_url", text)
                self.assertNotIn("token", text)

    def test_index_without_extracted_sources_is_409(self):
        with temp_data_root() as root, peelfeed_env():
            client = _client()
            dataset, _pdf = _upload(client, root)
            # The 409 gate runs before link resolution, so no embed link is needed.
            response = client.post("/api/datasets/%s/index" % dataset["id"],
                                   json={})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("no_extracted_sources", response.json()["detail"])

    def test_index_reads_a_peel_and_second_index_is_free(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake, \
                peelfeed_env():
            client = _client()
            dataset, _pdf = _upload(client, root, pages=[
                "Widget installation. The widget is mounted with two screws.",
                "Widget operation. Press the green button to start it."])
            _add_embed_link(client, fake)
            make_peel(dataset["id"], pages=[
                "Widget installation. The widget is mounted with two screws.",
                "Widget operation. Press the green button to start it."])
            first = client.post("/api/datasets/%s/index" % dataset["id"],
                                json={})
            self.assertEqual(first.status_code, 202, first.text)
            job = _wait_job(client, first.json()["job"])
            self.assertEqual(job["state"], "succeeded", job)
            self.assertEqual(job["progress"]["done"], 2)
            requests_after_first = len(fake.requests)
            self.assertGreater(requests_after_first, 0)
            listed = client.get("/api/datasets").json()["datasets"][0]
            self.assertEqual(listed["chunks"], 2)
            self.assertEqual(len(listed["indexes"]), 1)
            sources = _sources(client, dataset["id"])
            self.assertTrue(sources[0]["indexed"])

            second = client.post("/api/datasets/%s/index" % dataset["id"],
                                 json={})
            self.assertEqual(second.status_code, 202, second.text)
            job = _wait_job(client, second.json()["job"])
            self.assertEqual(job["state"], "succeeded", job)
            # Zero additional embedding requests: the index is already current.
            self.assertEqual(len(fake.requests), requests_after_first)

            search = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "green button"})
            self.assertEqual(search.status_code, 200, search.text)
            self.assertTrue(search.json()["results"])


class NoHttpContractTests(unittest.TestCase):
    def test_the_adapter_has_no_http_or_token_code_path(self):
        import inspect
        from peekseed import peelfeed
        source = inspect.getsource(peelfeed)
        for marker in ("httpx", "base_url", "Authorization", "Bearer",
                       "/peels/", "PEEKSEED_PEELFEED_TOKEN"):
            self.assertNotIn(marker, source)

    def test_the_config_has_no_base_url_or_token_keys(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                cfg = peelfeed.load()
                self.assertEqual(sorted(cfg), [
                    "chunk_profile", "cli", "enabled", "llm_mode", "poll",
                    "python", "repo", "root", "timeout", "version"])
                masked = peelfeed.mask(cfg)
                self.assertNotIn("base_url", masked)
                self.assertNotIn("token", masked)
                self.assertNotIn("token_set", masked)


if __name__ == "__main__":
    unittest.main()

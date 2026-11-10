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
import copy
import hashlib
import json
import os
import time
import unittest

from tests.fixtures import (DEFAULT_REPORT, DEFAULT_REVIEW,
                            DEFAULT_VLM_DISCUSS, EMBED_CHUNK,
                            FakeModelServer, FakePeelfeedCli,
                            REVIEW_VLM, VLM_CONFIRMATION, VLM_DISCUSS_PATCH,
                            VLM_LINK_BASE, VLM_LINK_ID, VLM_LINK_KEY,
                            VLM_LINK_KEY_ENV, VLM_MODEL, make_pdf, make_peel,
                            temp_data_root, write_peel_build)

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


def _wait_review_job(client, base, job_id, timeout=10.0):
    """Poll ``GET …/review/jobs/{id}`` until a terminal state.

    The fake CLI makes every job terminal immediately (CP3 flake hardening), so
    this normally returns on the first call; queued/running records are only
    installed by tests that set ``state["jobs"]`` directly.
    """
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        response = client.get(base + "/review/jobs/" + job_id)
        assert response.status_code == 200, response.text
        state = response.json()
        if state.get("status") in ("done", "failed", "interrupted"):
            return state
        time.sleep(0.02)
    raise AssertionError("review job %s did not finish: %r" % (job_id, state))


def _answer_link(**overrides):
    """Create the enabled default answer link used by VLM report/discuss."""
    from peekseed import links
    data = {"id": VLM_LINK_ID, "role": "answer", "label": "Review VLM",
            "protocol": "openai", "base_url": VLM_LINK_BASE,
            "api_key_env": VLM_LINK_KEY_ENV, "model": VLM_MODEL,
            "enabled": True}
    data.update(overrides)
    return links.create(data)


@contextlib.contextmanager
def vlm_answer_key():
    """Set the answer link's API key env for one test (never stored in links)."""
    saved = os.environ.get(VLM_LINK_KEY_ENV)
    os.environ[VLM_LINK_KEY_ENV] = VLM_LINK_KEY
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(VLM_LINK_KEY_ENV, None)
        else:
            os.environ[VLM_LINK_KEY_ENV] = saved


def _confirmation(**overrides):
    """A copy of the frozen privacy confirmation with field overrides."""
    value = copy.deepcopy(VLM_CONFIRMATION)
    for key, item in overrides.items():
        value[key] = item
    return value


def _confirmation_scope(**overrides):
    value = copy.deepcopy(VLM_CONFIRMATION)
    value["scope"].update(overrides)
    return value


def _discuss_request(**overrides):
    value = {
        "conversation_id": None,
        "scope": {"cluster_id": "cl-0f1e2d3c4b5a"},
        "message": "为什么把第 0 行作为表头？",
        "base_plan_version": 0,
        "confirmation": copy.deepcopy(VLM_CONFIRMATION),
    }
    value.update(overrides)
    return value


def _confirm_operation_entry(client, base, item_id="it-3f9c2a4b6d8e0f1a",
                             base_version=0):
    """Select + confirm one operation entry; returns ``(plan_version, entry_id)``."""
    route = base + "/review/plan"
    selected = client.post(route, json={
        "base_plan_version": base_version, "action": "select",
        "entry": {"item_id": item_id,
                  "strategy": {"id": "use_first_row_header", "version": 1},
                  "params": {"rows": [0]}}})
    assert selected.status_code == 200, selected.text
    entry_id = selected.json()["entry"]["entry_id"]
    confirmed = client.post(route, json={
        "base_plan_version": selected.json()["plan_version"],
        "action": "confirm", "entry_id": entry_id})
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["plan_version"], entry_id


def _create_batch(client, base, item_id="it-3f9c2a4b6d8e0f1a",
                  base_version=0):
    """Confirm one operation entry and create its batch; returns the 202 body."""
    version, entry_id = _confirm_operation_entry(client, base, item_id,
                                                 base_version)
    response = client.post(base + "/review/batches",
                           json={"entry_ids": [entry_id]})
    assert response.status_code == 202, response.text
    return response.json(), entry_id, version


def _accept_all_or_nothing_flow(client, root, cli):
    """The CP2 accept all-or-nothing assertions (run twice by the flake test)."""
    base = _extracted_review_source(client, root)
    route = base + "/review/plan"
    default_report = copy.deepcopy(cli.state().get("review_report")
                                   or DEFAULT_REPORT)
    failed = client.post(route, json={
        "base_plan_version": 0, "action": "accept",
        "item_ids": ["it-3f9c2a4b6d8e0f1a",
                     "it-5a7c8b9d0e1f2a3b",
                     "it-0000000000000000"]})
    assert failed.status_code == 400, failed.text
    detail = failed.json()["detail"]
    assert "validation" in detail, detail
    assert "it-5a7c8b9d0e1f2a3b=stale" in detail, detail
    assert "it-0000000000000000=item_missing" in detail, detail
    state = cli.state()
    plan_state = state.get("plan") or {}
    assert plan_state.get("plan_version", 0) == 0, plan_state
    assert not plan_state.get("entries"), plan_state
    assert not state.get("resolutions"), state.get("resolutions")
    # no_ready_suggestion when the report has no ready candidate.
    report = copy.deepcopy(default_report)
    report["report"]["clusters"] = report["report"]["clusters"][:1]
    cli.update(review_report=report)
    missing = client.post(route, json={
        "base_plan_version": 0, "action": "accept",
        "item_ids": ["it-8b7a6c5d4e3f2a10"]})
    assert missing.status_code == 400, missing.text
    assert "it-8b7a6c5d4e3f2a10=no_ready_suggestion" in missing.json()["detail"]
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
    assert invalid.status_code == 400, invalid.text
    assert "it-5a7c8b9d0e1f2a3b=validator_failed" in invalid.json()["detail"]
    return base


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

    def test_cp3_batch_publish_discard_rollback_and_job_argv(self):
        """CP3 wire §2.6: adapter methods call the frozen CLI argv/stdin."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]

                def confirmed_entry(item_id, base_version):
                    selected = engine.plan(peel_id, {
                        "action": "select", "base_plan_version": base_version,
                        "item_id": item_id,
                        "strategy": {"id": "use_first_row_header",
                                     "version": 1},
                        "params": {"rows": [0]}})
                    confirmed = engine.plan(peel_id, {
                        "action": "confirm",
                        "base_plan_version": selected["plan_version"],
                        "entry_id": selected["entry"]["entry_id"]})
                    return confirmed["plan_version"], selected["entry"]["entry_id"]

                version, first_entry = confirmed_entry("it-3f9c2a4b6d8e0f1a", 0)
                applied = engine.apply(peel_id, [first_entry])
                self.assertEqual(set(applied),
                                 {"job_id", "batch_id", "status"})
                self.assertEqual(applied["status"], "queued")
                self.assertEqual(cli.state()["apply_requests"][-1],
                                 {"entry_ids": [first_entry]})
                batch = engine.batch_show(peel_id, applied["batch_id"])
                self.assertEqual(batch["schema_version"], 1)
                self.assertEqual(batch["status"], "ready_to_publish")
                self.assertEqual(batch["changes"][0]["entry_id"], first_entry)
                self.assertEqual(set(batch["changes"][0]["operation"]),
                                 {"schema_version", "name", "semantic",
                                  "source_refs"})
                job = engine.job_show(peel_id, applied["job_id"])
                self.assertEqual(job["type"], "build")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"],
                                 "batches/%s.json" % applied["batch_id"])

                published = engine.publish(peel_id, applied["batch_id"])
                self.assertEqual(set(published), {"batch", "current"})
                self.assertEqual(published["batch"]["status"], "published")
                self.assertTrue(published["current"]["published"])
                self.assertEqual(
                    published["batch"]["publish"]["current_build_id"],
                    published["current"]["build_id"])
                self.assertEqual(cli.state()["published_batches"],
                                 [applied["batch_id"]])

                # A second confirmed entry gets its own batch, then discarded.
                version, second_entry = confirmed_entry("it-8b7a6c5d4e3f2a10",
                                                       version)
                second = engine.apply(peel_id, [second_entry])
                discarded = engine.discard(peel_id, second["batch_id"])
                self.assertEqual(discarded["batch"]["status"], "discarded")
                self.assertFalse(discarded["current"]["published"])
                self.assertEqual(
                    engine.batch_show(peel_id, second["batch_id"])["status"],
                    "discarded")

                rolled = engine.rollback(peel_id, reason="revert the batch")
                self.assertEqual(set(rolled), {"job_id", "batch_id", "status"})
                self.assertEqual(rolled["status"], "queued")
                rollback_job = engine.job_show(peel_id, rolled["job_id"])
                self.assertEqual(rollback_job["type"], "rollback")
                self.assertEqual(rollback_job["status"], "done")
                self.assertEqual(cli.state()["rollback_requests"][-1],
                                 {"peel_id": peel_id,
                                  "reason": "revert the batch"})
                self.assertIn(rolled["job_id"],
                              [row["job_id"] for row in engine.job_list(peel_id)])

                calls = [call["argv"] for call in cli.calls()]
                apply_call = next(call for call in calls if call[0] == "apply")
                self.assertIn("--input", apply_call)
                self.assertIn("-", apply_call)
                batch_call = next(call for call in calls if call[0] == "batch")
                # Real CLI order is action BEFORE the peel id (wire §2.6).
                self.assertEqual(batch_call[1:3], ["show", peel_id])
                self.assertEqual(
                    batch_call[batch_call.index("--batch") + 1],
                    applied["batch_id"])
                publish_call = next(call for call in calls
                                    if call[0] == "publish")
                self.assertEqual(
                    publish_call[publish_call.index("--batch") + 1],
                    applied["batch_id"])
                discard_call = next(call for call in calls
                                    if call[0] == "discard")
                self.assertEqual(
                    discard_call[discard_call.index("--batch") + 1],
                    second["batch_id"])
                rollback_call = next(call for call in calls
                                     if call[0] == "rollback")
                self.assertIn("--reason", rollback_call)
                job_calls = [call for call in calls if call[0] == "job"]
                self.assertEqual(job_calls[0][1:3], ["show", peel_id])
                self.assertIn("--job", job_calls[0])
                self.assertEqual(job_calls[-1][1:3], ["list", peel_id])

    def test_cp4_ocr_client_argv_stdin_and_env_forwarding(self):
        """CP4 K1/K2 (wire §2.8): the frozen ``ocr-check``/``ocr-refresh`` shapes.

        ``provider:{}`` means "use the env config"; peekseed forwards every
        ``PEELFEED_OCR_*`` env var through the existing allowlist seam. CP4.1
        G1: the client never accepts or sends a caller-supplied provider object
        (only peelfeed's local CLI may override the provider).
        """
        ocr_keys = ("PEELFEED_OCR_PROVIDER", "PEELFEED_OCR_ENGINE",
                    "PEELFEED_OCR_PARAMS", "PEELFEED_OCR_TIMEOUT",
                    "PEELFEED_OCR_MIN_CONFIDENCE")
        saved = {key: os.environ.get(key) for key in ocr_keys}
        try:
            os.environ["PEELFEED_OCR_PROVIDER"] = (
                "/tmp/ocr-venv/bin/python /tmp/peelfeed/scripts/"
                "ocr_provider_rapidocr.py")
            os.environ["PEELFEED_OCR_ENGINE"] = "rapidocr@1.4.4"
            os.environ["PEELFEED_OCR_PARAMS"] = '{"lang": "en"}'
            os.environ["PEELFEED_OCR_TIMEOUT"] = "12.5"
            os.environ["PEELFEED_OCR_MIN_CONFIDENCE"] = "0.7"
            with temp_data_root() as root:
                cli = FakePeelfeedCli(root / "fake")
                source = make_pdf(root / "manual.pdf")
                with peelfeed_env(CLI=str(cli.path)):
                    from peekseed import peelfeed
                    engine = peelfeed.client()
                    peel_id = engine.ingest(
                        str(source), llm_mode="off")["peel"]["peel_id"]
                    checked = engine.ocr_check(peel_id)
                    self.assertTrue(checked["available"])
                    self.assertEqual(checked["engine"], "rapidocr@1.4.4")
                    self.assertIsNone(checked["error"])
                    # Frozen stdin: provider is ALWAYS the env-config object.
                    self.assertEqual(cli.state()["ocr_check_requests"][-1],
                                     {"provider": {}})
                    refreshed = engine.ocr_refresh(peel_id, ["it-a", "it-b"])
                    self.assertEqual(refreshed["status"], "queued")
                    self.assertTrue(refreshed["job_id"].startswith("ocr-"))
                    self.assertIsNone(refreshed["result_ref"])
                    # Frozen stdin: item ids only, no provider key at all.
                    self.assertEqual(
                        cli.state()["ocr_refresh_requests"][-1],
                        {"item_ids": ["it-a", "it-b"]})
                    # CP4.1 G1: the client API no longer accepts a provider.
                    with self.assertRaises(TypeError):
                        engine.ocr_check(peel_id, provider={})
                    with self.assertRaises(TypeError):
                        engine.ocr_refresh(peel_id, ["it-a"], provider={})
                    # The job is persisted in the fake peel and re-attachable.
                    job = engine.job_show(peel_id, refreshed["job_id"])
                    self.assertEqual(job["type"], "ocr")
                    self.assertEqual(job["status"], "done")
                    calls = cli.calls()
                    check_call = next(call for call in calls
                                      if call["argv"][0] == "ocr-check")
                    refresh_call = next(call for call in calls
                                        if call["argv"][0] == "ocr-refresh")
                    # ``ocr-check <peel> --input - --root <peels> --json``
                    self.assertEqual(check_call["argv"][1:3],
                                     [peel_id, "--input"])
                    self.assertEqual(check_call["argv"][3], "-")
                    self.assertIn("--root", check_call["argv"])
                    self.assertIn("--json", check_call["argv"])
                    # ``ocr-refresh <peel> --input - --root <peels> --json``
                    self.assertEqual(refresh_call["argv"][1:3],
                                     [peel_id, "--input"])
                    self.assertEqual(refresh_call["argv"][3], "-")
                    self.assertIn("--root", refresh_call["argv"])
                    self.assertIn("--json", refresh_call["argv"])
                    # Env-only OCR config reaches the peelfeed subprocess.
                    self.assertEqual(check_call["ocr_env"], {
                        "PEELFEED_OCR_PROVIDER":
                            os.environ["PEELFEED_OCR_PROVIDER"],
                        "PEELFEED_OCR_ENGINE": "rapidocr@1.4.4",
                        "PEELFEED_OCR_PARAMS": '{"lang": "en"}',
                        "PEELFEED_OCR_TIMEOUT": "12.5",
                        "PEELFEED_OCR_MIN_CONFIDENCE": "0.7",
                    })
                    self.assertEqual(check_call["peel_shared_dir"],
                                     str(_shared(root)))
                    # Local validation never invokes the CLI with a bad request.
                    with self.assertRaises(ValueError):
                        engine.ocr_refresh(peel_id, [])
                    with self.assertRaises(ValueError):
                        engine.ocr_refresh(peel_id, ["it-a", "it-a"])
                    with self.assertRaises(ValueError):
                        engine.ocr_refresh(peel_id, ["it-a", 7])
                    with self.assertRaises(ValueError):
                        engine.ocr_refresh(peel_id, ["  "])
                    self.assertEqual(
                        len([call for call in cli.calls()
                             if call["argv"][0] == "ocr-refresh"]), 1)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_cp4_ocr_error_mapping(self):
        """CP4 K1 (contract §4 additions): OCR codes map to 503/409/400/502."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                # ocr_unavailable -> 503 (provider unconfigured/unrunnable)
                cli.update(ocr_check_error={"code": "ocr_unavailable",
                                            "message": "no OCR provider"})
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.ocr_check(peel_id)
                self.assertEqual(caught.exception.status, 503)
                self.assertIn("ocr_unavailable", caught.exception.detail)
                # ocr_config_invalid -> 400 (bad env config)
                cli.update(ocr_check_error={"code": "ocr_config_invalid",
                                            "message": "bad engine"})
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.ocr_check(peel_id)
                self.assertEqual(caught.exception.status, 400)
                # not_ready -> 409 (ocr-refresh without a current build)
                cli.update(ocr_check_error=None, ocr_not_ready=True)
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.ocr_refresh(peel_id, ["it-a"])
                self.assertEqual(caught.exception.status, 409)
                self.assertIn("not_ready", caught.exception.detail)
                # Other OCR codes are peelfeed failures -> 502.
                cli.update(ocr_not_ready=False,
                           ocr_refresh_error={"code": "ocr_timeout",
                                              "message": "provider timed out"})
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.ocr_refresh(peel_id, ["it-a"])
                self.assertEqual(caught.exception.status, 502)
                self.assertIn("ocr_timeout", caught.exception.detail)

    def test_cp3_error_mapping_for_batch_publish_and_jobs(self):
        """busy→423, stale/conflict/corrections_conflict→409, not found→404."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                selected = engine.plan(peel_id, {
                    "action": "select", "base_plan_version": 0,
                    "item_id": "it-3f9c2a4b6d8e0f1a",
                    "strategy": {"id": "use_first_row_header", "version": 1},
                    "params": {"rows": [0]}})
                engine.plan(peel_id, {
                    "action": "confirm",
                    "base_plan_version": selected["plan_version"],
                    "entry_id": selected["entry"]["entry_id"]})
                applied = engine.apply(peel_id,
                                       [selected["entry"]["entry_id"]])
                cases = [
                    ("apply", lambda: engine.apply(peel_id, ["e-x"]), "busy",
                     423),
                    ("publish", lambda: engine.publish(
                        peel_id, applied["batch_id"]), "stale", 409),
                    ("publish", lambda: engine.publish(
                        peel_id, applied["batch_id"]), "conflict", 409),
                    ("publish", lambda: engine.publish(
                        peel_id, applied["batch_id"]),
                     "corrections_conflict", 409),
                    ("discard", lambda: engine.discard(
                        peel_id, applied["batch_id"]), "validation", 400),
                    ("rollback", lambda: engine.rollback(peel_id), "conflict",
                     409),
                    ("job", lambda: engine.job_show(peel_id, "nope"),
                     "not_found", 404),
                    ("batch", lambda: engine.batch_show(peel_id, "nope"),
                     "not_found", 404),
                ]
                for command, call, code, status in cases:
                    cli.update(errors={command: {"code": code,
                                                 "message": code + " failure"}})
                    with self.assertRaises(peelfeed.PeelfeedError) as caught:
                        call()
                    self.assertEqual(caught.exception.status, status, command)
                    self.assertIn(code, caught.exception.detail)
                cli.update(errors={})
                # Unknown batch/job are 404 via peelfeed's not-found exit code.
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.batch_show(peel_id, "batch-missing")
                self.assertEqual(caught.exception.status, 404)
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.job_show(peel_id, "build-missing")
                self.assertEqual(caught.exception.status, 404)

        """CP3 wire §2.6: ``review-report`` returns a queued job, not a report."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")["peel"]["peel_id"]
                report = engine.review_report(peel_id)
                self.assertEqual(set(report), {"job_id", "status", "result_ref"})
                self.assertEqual(report["status"], "queued")
                self.assertIsNone(report["result_ref"])
                job = engine.job_show(peel_id, report["job_id"])
                self.assertEqual(job["type"], "report")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"], "report.json")
                filtered = engine.review_report(
                    peel_id, item_ids=["it-3f9c2a4b6d8e0f1a"],
                    cluster_ids=["cl-0f1e2d3c4b5a"])
                self.assertNotEqual(filtered["job_id"], report["job_id"])
                calls = [call["argv"] for call in cli.calls()
                         if call["argv"][0] == "review-report"]
                self.assertNotIn("--input", calls[0])
                self.assertIn("--input", calls[1])
                self.assertEqual(cli.state()["review_report_requests"][-1], {
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})
                # The job list is the real CLI shape: a list of job records.
                jobs = engine.job_list(peel_id)
                self.assertEqual(len(jobs), 2)
                self.assertEqual({row["type"] for row in jobs}, {"report"})
                self.assertEqual(
                    [row["job_id"] for row in engine.job_list(
                        peel_id, status="done")],
                    [row["job_id"] for row in jobs])

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
                    "source", "origin", "revision", "replaced_existing"})
                self.assertEqual(operation["status"], "confirmed")
                # CP5.1 H5 (wire §10.5): accepted entries record the actual
                # source and the report origin (job id/model/prompt version).
                self.assertEqual(operation["source"], "rule")
                self.assertEqual(set(operation["origin"]), {
                    "kind", "job_id", "model", "prompt_version"})
                self.assertEqual(operation["origin"]["kind"], "report")
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
                _accept_all_or_nothing_flow(client, root, cli)

    def test_accept_all_or_nothing_is_stable_under_fast_polling(self):
        """CP3 W0/W4 flake regression: the accept flow is deterministic twice.

        The CP2 review observed one first-run flake with ``POLL=0.05``: a late
        status poller could persist a stale ``running`` block after the worker's
        terminal write, leaving ``_wait_source`` waiting forever. The fix makes a
        non-terminal poll update unable to downgrade a terminal block and lets a
        request-path refresh persist an already-terminal CLI result; this test
        runs the full accept flow twice with the fast poll to prove it.
        """
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                for _run in range(2):
                    _accept_all_or_nothing_flow(client, root, cli)
                    # Reset only the fake plan/report state between runs: the
                    # same client/root keeps the fast poll and the extraction
                    # race in play (a fresh root would hide the timing).
                    cli.update(plan=None, review=None, review_report=None,
                               resolutions=[], accept_requests=[])

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

    def test_review_reports_create_a_job_and_persist_the_descriptor(self):
        """CP3 wire §2.7: 202 job; descriptor in ``extraction.review_jobs``."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/reports"
                report = client.post(route, json={})
                self.assertEqual(report.status_code, 202, report.text)
                payload = report.json()
                self.assertEqual(set(payload), {"job_id", "status"})
                self.assertEqual(payload["status"], "queued")
                self.assertTrue(payload["job_id"].startswith("report-"))
                job = _wait_review_job(client, base, payload["job_id"])
                self.assertEqual(job["type"], "report")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"], "report.json")
                # The source list exposes the persisted re-attach descriptor.
                row = _sources(client, base.split("/")[3])[0]
                descriptor = row["extraction"]["review_jobs"][payload["job_id"]]
                self.assertEqual(descriptor["type"], "report")
                self.assertIsNone(descriptor["batch_id"])
                self.assertTrue(descriptor["created_at"])
                filtered = client.post(route, json={
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})
                self.assertEqual(filtered.status_code, 202, filtered.text)
                self.assertNotEqual(filtered.json()["job_id"],
                                    payload["job_id"])
                self.assertEqual(cli.state()["review_report_requests"][-1], {
                    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
                    "cluster_ids": ["cl-0f1e2d3c4b5a"]})
                bad = client.post(route, json={"item_ids": "it-x"})
                self.assertEqual(bad.status_code, 400, bad.text)
                # CP5: VLM mode is accepted but requires the frozen privacy
                # confirmation; an empty one is a 400 before any CLI call.
                vlm = client.post(route, json={"mode": "vlm",
                                               "confirmation": {}})
                self.assertEqual(vlm.status_code, 400, vlm.text)
                self.assertIn("confirmation", vlm.json()["detail"])
                unknown = client.post(route, json={"tier": "R2"})
                self.assertEqual(unknown.status_code, 400, unknown.text)
                # peelfeed failure maps to 400/502, never a silent success
                cli.update(errors={"review-report": {
                    "code": "validation", "message": "bad report request"}})
                failed = client.post(route, json={})
                self.assertEqual(failed.status_code, 400, failed.text)

    def test_review_jobs_reattach_after_a_restart(self):
        """CP3 §2.7: a restarted service re-attaches by id, never reruns."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                report = client.post(base + "/review/reports", json={})
                job_id = report.json()["job_id"]
                dataset_id = base.split("/")[3]
                sha = base.split("/")[5]
                commands_before = cli.commands()
                # Simulate a restart: new app instance, same data root and fake CLI.
                from peekseed import app as app_module
                app_module.reset_state()
                restarted = _client()
                rows = _sources(restarted, dataset_id)
                row = next(item for item in rows if item["sha256"] == sha)
                self.assertEqual(
                    row["extraction"]["review_jobs"][job_id]["type"], "report")
                reattached = restarted.get(base + "/review/jobs/" + job_id)
                self.assertEqual(reattached.status_code, 200, reattached.text)
                self.assertEqual(reattached.json()["job_id"], job_id)
                self.assertEqual(reattached.json()["status"], "done")
                # Re-attach is read-only: no second review-report was created.
                self.assertEqual(
                    cli.commands().count("review-report"),
                    commands_before.count("review-report"))

    def test_review_batches_publish_discard_and_rollback_routes(self):
        """CP3 wire §2.7: batch/job/publish/discard/rollback route shapes."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                dataset_id = base.split("/")[3]
                sha = base.split("/")[5]
                created, entry_id, plan_version = _create_batch(client, base)
                self.assertEqual(set(created), {"job_id", "batch_id", "status"})
                self.assertEqual(created["status"], "queued")
                # The descriptor is persisted with type=build and the batch id.
                row = next(item for item in _sources(client, dataset_id)
                           if item["sha256"] == sha)
                descriptor = row["extraction"]["review_jobs"][created["job_id"]]
                self.assertEqual(descriptor["type"], "build")
                self.assertEqual(descriptor["batch_id"], created["batch_id"])
                # GET batch -> the persisted §2.3 record.
                batch = client.get(base + "/review/batches/" + created["batch_id"])
                self.assertEqual(batch.status_code, 200, batch.text)
                record = batch.json()
                self.assertEqual(record["schema_version"], 1)
                self.assertEqual(record["batch_id"], created["batch_id"])
                self.assertEqual(record["status"], "ready_to_publish")
                self.assertEqual(record["changes"][0]["entry_id"], entry_id)
                self.assertEqual(record["build"]["gates"]["all_pass"], True)
                # CP3.1 F3: the batch record carries the additive diff.reindex
                # shape; content counts are content-only (one changed entry).
                diff = record["build"]["diff"]
                self.assertEqual(set(diff["reindex"]),
                                 {"required", "reason", "chunks"})
                self.assertTrue(diff["reindex"]["required"])
                self.assertEqual(diff["reindex"]["reason"],
                                 "conversion_changed")
                self.assertEqual(diff["reindex"]["chunks"], 2)
                self.assertEqual(diff["chunks_changed"], 1)
                self.assertEqual(diff["blocks_changed"], 0)
                # GET job -> the §2.5 record (terminal immediately in the fake).
                job = _wait_review_job(client, base, created["job_id"])
                self.assertEqual(job["type"], "build")
                self.assertEqual(job["result_ref"],
                                 "batches/%s.json" % created["batch_id"])
                # Publish: 200 {batch, current}.
                published = client.post(
                    base + "/review/batches/" + created["batch_id"] + "/publish",
                    json={})
                self.assertEqual(published.status_code, 200, published.text)
                body = published.json()
                self.assertEqual(set(body), {"batch", "current"})
                self.assertEqual(body["batch"]["status"], "published")
                self.assertTrue(body["current"]["published"])
                self.assertEqual(body["batch"]["publish"]["current_build_id"],
                                 body["current"]["build_id"])
                # Publishing again is a conflict (fake: already published).
                again = client.post(
                    base + "/review/batches/" + created["batch_id"] + "/publish",
                    json={})
                self.assertEqual(again.status_code, 409, again.text)
                self.assertIn("conflict", again.json()["detail"])
                # Discard: current/ledger unchanged; the record stays discarded.
                current = {"build_id": "b-current", "conversion_id": "c" * 64,
                           "effective_corrections_hash": "d" * 64,
                           "published": True}
                cli.update(publish_current=current)
                _version, second_entry = _confirm_operation_entry(
                    client, base, "it-8b7a6c5d4e3f2a10", plan_version)
                second = client.post(base + "/review/batches",
                                     json={"entry_ids": [second_entry]})
                self.assertEqual(second.status_code, 202, second.text)
                discarded = client.post(
                    base + "/review/batches/" + second.json()["batch_id"]
                    + "/discard", json={})
                self.assertEqual(discarded.status_code, 200, discarded.text)
                self.assertEqual(discarded.json()["batch"]["status"],
                                 "discarded")
                self.assertEqual(discarded.json()["current"], current)
                # Rollback: 202 {job_id, batch_id, status} + descriptor.
                rolled = client.post(base + "/review/rollback", json={})
                self.assertEqual(rolled.status_code, 202, rolled.text)
                rolled_body = rolled.json()
                self.assertEqual(set(rolled_body),
                                 {"job_id", "batch_id", "status"})
                self.assertEqual(rolled_body["status"], "queued")
                rollback_job = _wait_review_job(client, base,
                                                rolled_body["job_id"])
                self.assertEqual(rollback_job["type"], "rollback")
                self.assertEqual(rollback_job["result_ref"],
                                 "batches/%s.json" % rolled_body["batch_id"])
                row = next(item for item in _sources(client, dataset_id)
                           if item["sha256"] == sha)
                self.assertEqual(
                    row["extraction"]["review_jobs"][rolled_body["job_id"]]
                    ["type"], "rollback")
                # Unknown batch/job are 404.
                missing = client.get(base + "/review/batches/batch-missing")
                self.assertEqual(missing.status_code, 404, missing.text)
                missing_job = client.get(base + "/review/jobs/build-missing")
                self.assertEqual(missing_job.status_code, 404, missing_job.text)

    def test_review_batch_request_validation_and_error_mapping(self):
        """CP3 wire §2.7: reject tier/scope/operations and malformed bodies."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/batches"
                cases = [
                    {},
                    {"entry_ids": []},
                    {"entry_ids": "e-1"},
                    {"entry_ids": [1]},
                    {"entry_ids": ["e-1", "e-1"]},
                    {"entry_ids": ["e-1"], "tier": "R2"},
                    {"entry_ids": ["e-1"], "scope": {}},
                    {"entry_ids": ["e-1"], "operations": []},
                    {"entry_ids": ["e-1"], "unknown": 1},
                ]
                for body in cases:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     (body, response.text))
                # A not-confirmed entry is a peelfeed validation error (400).
                not_confirmed = client.post(route,
                                            json={"entry_ids": ["e-nope"]})
                self.assertEqual(not_confirmed.status_code, 404,
                                 not_confirmed.text)
                # publish/discard/rollback take an empty body only.
                for suffix, method in (
                        ("/review/batches/batch-x/publish", "post"),
                        ("/review/batches/batch-x/discard", "post"),
                        ("/review/rollback", "post")):
                    response = getattr(client, method)(base + suffix,
                                                       json={"batch_id": "x"})
                    self.assertEqual(response.status_code, 400, response.text)
                # peelfeed error mapping through the API: busy 423, stale 409.
                _version, entry_id = _confirm_operation_entry(client, base)
                created = client.post(route, json={"entry_ids": [entry_id]})
                batch_id = created.json()["batch_id"]
                cli.update(errors={"publish": {"code": "busy",
                                               "message": "lock held"}})
                busy = client.post(
                    base + "/review/batches/" + batch_id + "/publish", json={})
                self.assertEqual(busy.status_code, 423, busy.text)
                cli.update(errors={"publish": {"code": "stale",
                                               "message": "candidate stale"}})
                stale = client.post(
                    base + "/review/batches/" + batch_id + "/publish", json={})
                self.assertEqual(stale.status_code, 409, stale.text)
                cli.update(errors={"apply": {"code": "busy",
                                             "message": "lock held"}})
                busy_apply = client.post(route, json={"entry_ids": [entry_id]})
                self.assertEqual(busy_apply.status_code, 423, busy_apply.text)
                cli.update(errors={})
                # review_schema mismatch stays a clear 409 on the new routes.
                cli.update(capabilities={"review_schema": 1})
                from peekseed import peelfeed
                peelfeed.invalidate_handshake()
                mismatch = client.post(route, json={"entry_ids": [entry_id]})
                self.assertEqual(mismatch.status_code, 409, mismatch.text)
                self.assertIn("review schema", mismatch.json()["detail"])

    def test_cp31_malformed_batch_and_job_ids_are_validation_400(self):
        """CP3.1 F5: generated-id validation in peelfeed maps to HTTP 400.

        peelfeed rejects ids that cannot have been generated (``../plan``,
        slash-containing, wrong prefix) with ``error [validation]`` before any
        path join; peekseed's seam maps that to 400. The malformed id must reach
        the CLI -- the validation lives in peelfeed, not in peekseed.
        """
        import subprocess

        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                cases = [
                    ("batch show", lambda: engine.batch_show(peel_id, "../plan")),
                    ("batch show", lambda: engine.batch_show(peel_id, "batch-a/b")),
                    ("batch show", lambda: engine.batch_show(peel_id, "not-a-batch")),
                    # system batches are ``system-b<build id>``.
                    ("batch show", lambda: engine.batch_show(peel_id, "system-x")),
                    ("publish", lambda: engine.publish(peel_id, "../plan")),
                    ("publish", lambda: engine.publish(peel_id, "batch-a/b")),
                    ("discard", lambda: engine.discard(peel_id, "batch-a/b")),
                    ("job show", lambda: engine.job_show(peel_id, "../plan")),
                    ("job show", lambda: engine.job_show(peel_id, "build-a/b")),
                    # ``job`` is not a generated job type (types are
                    # report/build/rollback/ocr/discuss).
                    ("job show", lambda: engine.job_show(peel_id, "job-x")),
                ]
                for what, call in cases:
                    with self.assertRaises(peelfeed.PeelfeedError) as caught:
                        call()
                    self.assertEqual(caught.exception.status, 400, what)
                    self.assertIn("validation", caught.exception.detail, what)
                # The fake CLI itself emits the real error protocol: exit 2 and
                # ``error [validation] …`` on stderr.
                completed = subprocess.run(
                    [str(cli.path), "batch", "show", peel_id,
                     "--batch", "../plan", "--root", str(_shared(root) / "peels"),
                     "--json"],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 2, completed.stdout)
                self.assertIn("error [validation]", completed.stderr)
                self.assertIn("invalid batch id", completed.stderr)
                # Valid-shaped but unknown ids stay 404 (no regression).
                for call in (lambda: engine.batch_show(peel_id, "batch-missing"),
                             lambda: engine.job_show(peel_id, "build-missing")):
                    with self.assertRaises(peelfeed.PeelfeedError) as caught:
                        call()
                    self.assertEqual(caught.exception.status, 404)
                    self.assertIn("not_found", caught.exception.detail)

    def test_cp31_malformed_ids_map_to_400_on_review_routes(self):
        """CP3.1 F5: batch/job routes return 400 for malformed ids."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                # Empty/whitespace ids are rejected by peekseed itself (400)
                # before any peelfeed subprocess is started.
                for path in ("/review/batches/%20", "/review/jobs/%20"):
                    response = client.get(base + path)
                    self.assertEqual(response.status_code, 400,
                                     (path, response.text))
                    self.assertIn("invalid", response.json()["detail"])
                # ``..`` decodes to a path segment that still reaches the route:
                # peelfeed's validation error maps to 400.
                calls_before = len(cli.calls())
                for method, path in (
                        ("get", "/review/batches/%2E%2E"),
                        ("get", "/review/jobs/%2E%2E"),
                        ("post", "/review/batches/%2E%2E/publish"),
                        ("post", "/review/batches/%2E%2E/discard")):
                    kwargs = {"json": {}} if method == "post" else {}
                    response = getattr(client, method)(base + path, **kwargs)
                    self.assertEqual(response.status_code, 400,
                                     (path, response.text))
                    self.assertIn("validation", response.json()["detail"])
                # The malformed ids did reach the CLI, which is the validation
                # layer (defence in depth, not a peekseed-side regex).
                cli_calls = [call["argv"] for call in cli.calls()[calls_before:]]
                self.assertTrue(any(argv[0] == "batch" for argv in cli_calls),
                                cli_calls)
                self.assertTrue(any(argv[0] == "job" for argv in cli_calls),
                                cli_calls)
                # Ids containing "/" can never match one HTTP path segment, so
                # those requests do not reach the route (404) and no CLI call is
                # made; peelfeed's validation remains the backstop for any
                # non-HTTP caller.
                calls_before = len(cli.calls())
                for path in ("/review/batches/batch-a%2Fb",
                             "/review/batches/%2E%2E%2Fplan",
                             "/review/jobs/build-a%2Fb"):
                    response = client.get(base + path)
                    self.assertEqual(response.status_code, 404,
                                     (path, response.text))
                    self.assertEqual(response.json()["detail"], "Not Found")
                self.assertEqual(len(cli.calls()), calls_before)

    def test_cp31_batch_diff_reindex_shape_and_content_only_counts(self):
        """CP3.1 F3: additive ``diff.reindex`` + content-only change counts."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            source = make_pdf(root / "manual.pdf")
            with peelfeed_env(CLI=str(cli.path)):
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source),
                                        llm_mode="off")["peel"]["peel_id"]
                state = cli.state()
                peel = state["peels"][peel_id]

                def confirmed(item_id, base_version):
                    selected = engine.plan(peel_id, {
                        "action": "select", "base_plan_version": base_version,
                        "item_id": item_id,
                        "strategy": {"id": "use_first_row_header",
                                     "version": 1},
                        "params": {"rows": [0]}})
                    confirmed = engine.plan(peel_id, {
                        "action": "confirm",
                        "base_plan_version": selected["plan_version"],
                        "entry_id": selected["entry"]["entry_id"]})
                    return confirmed["plan_version"], selected["entry"]["entry_id"]

                def diff_of(batch_id):
                    return engine.batch_show(peel_id, batch_id)["build"]["diff"]

                # 1. no current build yet -> full rebuild, no_current_build.
                cli.update(peels={peel_id: dict(peel, conversion_id="")})
                version, first = confirmed("it-cp31-a", 0)
                applied = engine.apply(peel_id, [first])
                diff = diff_of(applied["batch_id"])
                self.assertEqual(
                    set(diff),
                    {"assets_changed", "blocks_changed", "chunks_changed",
                     "new_warnings", "changed_assets", "samples", "reindex"})
                self.assertEqual(set(diff["reindex"]),
                                 {"required", "reason", "chunks"})
                self.assertTrue(diff["reindex"]["required"])
                self.assertEqual(diff["reindex"]["reason"], "no_current_build")
                self.assertEqual(diff["reindex"]["chunks"], 2)
                # content-only counts: one changed entry/chunk, not every chunk
                self.assertEqual(diff["assets_changed"], 1)
                self.assertEqual(diff["blocks_changed"], 0)
                self.assertEqual(diff["chunks_changed"], 1)

                # 2. conversion changed -> full rebuild, conversion_changed.
                cli.update(peels={peel_id: dict(peel,
                                                conversion_id="a" * 64)})
                version, second = confirmed("it-cp31-b", version)
                applied = engine.apply(peel_id, [second])
                diff = diff_of(applied["batch_id"])
                self.assertTrue(diff["reindex"]["required"])
                self.assertEqual(diff["reindex"]["reason"],
                                 "conversion_changed")
                self.assertEqual(diff["reindex"]["chunks"], 2)
                self.assertEqual(diff["chunks_changed"], 1)

                # 3. same conversion (e.g. chunk-set-only build) -> no reindex.
                cli.update(batch_conversion_id="a" * 64)
                version, third = confirmed("it-cp31-c", version)
                applied = engine.apply(peel_id, [third])
                diff = diff_of(applied["batch_id"])
                self.assertEqual(diff["reindex"],
                                 {"required": False, "reason": None,
                                  "chunks": 0})

    def test_corrections_conflict_is_surfaced_in_sources_and_datasets(self):
        """CP3 wire §2.7/§2.9: state + conflicting corrections in reads."""
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
                conflict = {
                    "corrections": [{"correction_id": "c-bad",
                                     "asset_id": "table-3-19",
                                     "source_batch_id": "batch-1",
                                     "reasons": ["history_correction_revalidation"]}],
                    "source_batches": ["batch-1"],
                }
                cli.update(peels={peel_id: {
                    "peel_id": peel_id, "state": "corrections_conflict",
                    "source": {"name": "manual.pdf", "sha256": sha, "pages": 2},
                    "profile": "generic",
                    "conversion_id": row["extraction"]["conversion_id"],
                    "counts": {"pages": 2, "chunks": 2},
                    "validation": {"errors": [], "review_items": 0},
                    "review_items": 0,
                    "error": "history correction revalidation failed",
                    "corrections_conflict": conflict,
                }})
                # A system rebuild in flight (source running) discovers the
                # conflict: the request-path refresh persists the terminal
                # state and its conflicting-corrections detail.
                from peekseed import config, ingest
                directory = config.paths().datasets_dir / dataset["id"]
                running = dict(row["extraction"])
                running.update({"state": "running", "finished_at": None})
                ingest.update_source(directory, sha, status="running",
                                     extraction=running)
                rows = _sources(client, dataset["id"])
                source = next(item for item in rows
                              if item["sha256"] == sha)
                extraction = source["extraction"]
                self.assertEqual(extraction["state"], "corrections_conflict")
                self.assertEqual(extraction["corrections_conflict"], conflict)
                self.assertIn("revalidation", extraction["error"])
                # The dataset list carries the aggregated banner data.
                listing = client.get("/api/datasets").json()["datasets"]
                public = next(item for item in listing
                              if item["id"] == dataset["id"])
                self.assertEqual(public["corrections_conflict"][0]["sha256"],
                                 sha)
                self.assertEqual(
                    public["corrections_conflict"][0]["corrections_conflict"],
                    conflict)

    def test_extraction_terminal_state_is_self_healing_after_a_late_poller(self):
        """CP3 W0/W4 flake hardening: a late poller cannot downgrade a terminal
        extraction, and a lost final write is repaired on the next read."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "extracted")
                from peekseed import app as app_module, config, ingest
                directory = config.paths().datasets_dir / dataset["id"]
                stale = dict(row["extraction"])
                stale.update({"state": "running", "finished_at": None})
                # The exact CP2 race: a late poller tries to persist "running"
                # after the worker already wrote the terminal block.
                written = app_module._persist_extraction(directory, sha, stale)
                self.assertFalse(written)
                rows = _sources(client, dataset["id"])
                self.assertEqual(rows[0]["extraction"]["state"], "extracted")
                # A lost final write (worker crash) leaves the source "running";
                # the request-path refresh persists peelfeed's terminal result.
                ingest.update_source(directory, sha, status="running",
                                     extraction=stale)
                recovered = _sources(client, dataset["id"])[0]
                self.assertEqual(recovered["extraction"]["state"], "extracted")
                self.assertEqual(recovered["status"], "extracted")

    def test_publish_reconciles_every_dataset_referencing_the_peel(self):
        """CP3 wire §2.7: publish refreshes ALL datasets that reference the peel."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, pdf = _upload(client, root)
                sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
                second = client.post(
                    "/api/datasets", data={"name": "Second"},
                    files=[("files", ("manual.pdf", pdf.read_bytes(),
                                      "application/pdf"))])
                self.assertEqual(second.status_code, 201, second.text)
                second_id = second.json()["dataset"]["id"]
                peel = make_peel(dataset["id"])
                make_peel(second_id)
                # The plan routes go through the fake CLI, so it must know the
                # peel the fixtures wrote on disk.
                cli.update(peels={peel["peel_id"]: {
                    "peel_id": peel["peel_id"], "state": "ready",
                    "source": {"name": "manual.pdf", "sha256": sha,
                               "pages": 2},
                    "profile": "generic",
                    "conversion_id": peel["conversion_id"],
                    "counts": {"pages": 2, "chunks": 2},
                    "validation": {"errors": [], "review_items": 0},
                    "review_items": 0, "error": None,
                }})
                base = "/api/datasets/%s/sources/%s" % (dataset["id"], sha)
                created, _entry_id, _version = _create_batch(client, base)
                # Simulate the synchronous peelfeed publish: a new build with a
                # new conversion id; the dataset blocks are left untouched.
                new_conversion = "e" * 64
                new_build = write_peel_build(peel, conversion_id=new_conversion)
                cli.update(publish_current={
                    "build_id": new_build["build_id"],
                    "conversion_id": new_conversion,
                    "effective_corrections_hash": "",
                    "published": True,
                })
                published = client.post(
                    base + "/review/batches/" + created["batch_id"] + "/publish",
                    json={})
                self.assertEqual(published.status_code, 200, published.text)
                # Both datasets now record the new conversion (including the
                # terminal ``extracted`` source of the second dataset).
                for dataset_id in (dataset["id"], second_id):
                    rows = _sources(client, dataset_id)
                    extraction = next(item["extraction"] for item in rows
                                      if item["sha256"] == sha)
                    self.assertEqual(extraction["conversion_id"],
                                     new_conversion, dataset_id)
                    self.assertEqual(extraction["state"], "extracted")

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


@contextlib.contextmanager
def _cp5_env(root, *, link=True, key=True, link_overrides=None):
    """Fake CLI + optional default answer link/key for one CP5 test."""
    cli = FakePeelfeedCli(root / "fake")
    saved_key = os.environ.get(VLM_LINK_KEY_ENV)
    with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
        from peekseed import links
        links.reset_cache()
        if link:
            _answer_link(**(link_overrides or {}))
        if key:
            os.environ[VLM_LINK_KEY_ENV] = VLM_LINK_KEY
        try:
            yield cli
        finally:
            if saved_key is None:
                os.environ.pop(VLM_LINK_KEY_ENV, None)
            else:
                os.environ[VLM_LINK_KEY_ENV] = saved_key
            links.reset_cache()


class Cp5VlmClientTests(unittest.TestCase):
    def test_vlm_report_client_argv_stdin_and_child_only_env(self):
        """CP5 K1 (wire §1.3/§2.1): ``--with-vlm --input -``, token env-only."""
        with temp_data_root() as root:
            source = make_pdf(root / "manual.pdf")
            with _cp5_env(root) as cli:
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")[
                    "peel"]["peel_id"]
                cli.clear_calls()
                confirmation = copy.deepcopy(VLM_CONFIRMATION)
                result = engine.review_report(
                    peel_id, mode="vlm", confirmation=confirmation)
                self.assertEqual(result["status"], "queued")
                self.assertTrue(result["job_id"].startswith("report-"))
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "review-report")
                # ``review-report <peel> --with-vlm --input - --root … --json``
                self.assertEqual(call["argv"][1:3], [peel_id, "--with-vlm"])
                self.assertEqual(call["argv"][3:5], ["--input", "-"])
                self.assertIn("--root", call["argv"])
                self.assertIn("--json", call["argv"])
                self.assertNotIn(VLM_LINK_KEY, " ".join(call["argv"]))
                # The token reaches exactly this child's env (wire §1.3).
                self.assertEqual(call["llm_env"], {
                    "PEELFEED_LLM_BASE": VLM_LINK_BASE,
                    "PEELFEED_LLM_MODEL": VLM_MODEL,
                    "PEELFEED_LLM_TOKEN": VLM_LINK_KEY,
                    "PEELFEED_LLM_LINK_ID": VLM_LINK_ID,
                })
                # Frozen stdin: mode + confirmation only, no token.
                self.assertEqual(cli.state()["review_vlm_requests"][-1], {
                    "mode": "vlm", "confirmation": confirmation})
                self.assertNotIn(VLM_LINK_KEY, json.dumps(cli.state()))
                # Filters ride the same VLM request.
                engine.review_report(
                    peel_id, item_ids=["it-3f9c2a4b6d8e0f1a"],
                    cluster_ids=["cl-0f1e2d3c4b5a"], mode="vlm",
                    confirmation=confirmation)
                request = cli.state()["review_vlm_requests"][-1]
                self.assertEqual(request["item_ids"], ["it-3f9c2a4b6d8e0f1a"])
                self.assertEqual(request["cluster_ids"],
                                 ["cl-0f1e2d3c4b5a"])
                # Rules reports keep their CP3 stdin and get NO LLM env.
                engine.review_report(peel_id, item_ids=["it-3f9c2a4b6d8e0f1a"])
                rules_call = [item for item in cli.calls()
                              if item["argv"][0] == "review-report"][-1]
                self.assertNotIn("--with-vlm", rules_call["argv"])
                self.assertEqual(rules_call["llm_env"], {})
                self.assertEqual(cli.state()["review_report_requests"][-1],
                                 {"item_ids": ["it-3f9c2a4b6d8e0f1a"]})

    def test_vlm_unavailable_without_link_or_key(self):
        """CP5 K1: no link / missing key -> 503 before any CLI call."""
        with temp_data_root() as root:
            source = make_pdf(root / "manual.pdf")
            with _cp5_env(root, link=False, key=False) as cli:
                from peekseed import links, peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")[
                    "peel"]["peel_id"]
                cli.clear_calls()
                for call in (
                        lambda: engine.review_report(
                            peel_id, mode="vlm",
                            confirmation=copy.deepcopy(VLM_CONFIRMATION)),
                        lambda: engine.discuss(peel_id, _discuss_request())):
                    with self.assertRaises(peelfeed.PeelfeedError) as caught:
                        call()
                    self.assertEqual(caught.exception.status, 503)
                    self.assertIn("vlm_unavailable",
                                  caught.exception.detail)
                self.assertFalse([item for item in cli.calls()
                                  if item["argv"][0] in ("review-report",
                                                         "discuss")])
                # A link without its key env is also 503, still no CLI call.
                links.reset_cache()
                _answer_link()
                os.environ.pop(VLM_LINK_KEY_ENV, None)
                with self.assertRaises(peelfeed.PeelfeedError) as caught:
                    engine.review_report(
                        peel_id, mode="vlm",
                        confirmation=copy.deepcopy(VLM_CONFIRMATION))
                self.assertEqual(caught.exception.status, 503)
                self.assertIn("vlm_unavailable", caught.exception.detail)
                self.assertFalse([item for item in cli.calls()
                                  if item["argv"][0] == "review-report"])

    def test_discuss_and_plan_patch_client_shapes(self):
        """CP5 K2/K3 (wire §2.1/§4/§5): discuss job, sync plan patch, no LLM env."""
        with temp_data_root() as root:
            source = make_pdf(root / "manual.pdf")
            with _cp5_env(root) as cli:
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")[
                    "peel"]["peel_id"]
                cli.clear_calls()
                request = _discuss_request()
                result = engine.discuss(peel_id, request)
                self.assertTrue(result["job_id"].startswith("discuss-"))
                self.assertTrue(result["conversation_id"].startswith("conv-"))
                self.assertEqual(result["status"], "queued")
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "discuss")
                self.assertEqual(call["argv"][1:4],
                                 [peel_id, "--input", "-"])
                self.assertIn("--root", call["argv"])
                self.assertIn("--json", call["argv"])
                self.assertNotIn(VLM_LINK_KEY, " ".join(call["argv"]))
                self.assertEqual(call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 VLM_LINK_KEY)
                self.assertEqual(cli.state()["discuss_requests"][-1],
                                 request)
                # plan patch is synchronous, no model call, no LLM env.
                # CP5.1 H5: source vlm needs the archived conversation+turn,
                # so the client-shape test uses a user-sourced patch.
                patch_request = {"base_plan_version": 0,
                                 "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                                 "source": "user"}
                response = engine.plan_patch(peel_id, patch_request)
                self.assertEqual(response["plan_version"], 1)
                self.assertEqual(response["diff"]["added"][0]["item_id"],
                                 "it-3f9c2a4b6d8e0f1a")
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "plan")
                self.assertEqual(call["argv"][1:5],
                                 [peel_id, "patch", "--input", "-"])
                self.assertEqual(call["llm_env"], {})
                self.assertEqual(cli.state()["plan_patch_requests"][-1],
                                 patch_request)
                self.assertNotIn(VLM_LINK_KEY,
                                 json.dumps(cli.state()["plan"]))


@contextlib.contextmanager
def _peelfeed_llm_env(**values):
    """Set peekseed's own ``PEELFEED_LLM_*`` env (the env-first provider)."""
    keys = ("PEELFEED_LLM_BASE", "PEELFEED_LLM_MODEL",
            "PEELFEED_LLM_TOKEN", "PEELFEED_LLM_LINK_ID")
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    for key, value in values.items():
        os.environ[key] = value
    try:
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


class Cp5VlmCapabilityOverlayTests(unittest.TestCase):
    """CP5 wire §2.3: the review payload must carry the truth about VLM.

    peelfeed's ``capabilities`` child never receives the provider env (only
    report/discuss children do), so peekseed overlays the resolved
    ``capabilities.vlm*`` block itself (Lead review note 1).
    """

    def test_review_payload_overlays_the_answer_link_provider(self):
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                payload = client.get(base + "/review?group=cluster").json()
                caps = payload["capabilities"]
                self.assertTrue(caps["vlm"])
                self.assertIsNone(caps["vlm_detail"])
                self.assertEqual(caps["vlm_provider"], "answer_link")
                self.assertEqual(caps["vlm_model"], VLM_MODEL)
                self.assertEqual(caps["vlm_link_id"], VLM_LINK_ID)
                self.assertIn("vlm_vision", caps)
                # The single-item route also carries the overlay (additive).
                item = payload["items"][0]["id"]
                one = client.get(base + "/review/items/" + item)
                self.assertEqual(one.status_code, 200, one.text)
                self.assertTrue(one.json()["capabilities"]["vlm"])
                self.assertEqual(one.json()["capabilities"]["vlm_provider"],
                                 "answer_link")
                # Removing the link/key turns the overlay off with a reason;
                # the rules report path is unaffected.
                from peekseed import links
                links.reset_cache()
                for existing in links.links("answer"):
                    links.delete(existing["id"])
                os.environ.pop(VLM_LINK_KEY_ENV, None)
                payload = client.get(base + "/review").json()
                caps = payload["capabilities"]
                self.assertFalse(caps["vlm"])
                self.assertIn("VLM 不可用", caps["vlm_detail"])
                self.assertIsNone(caps["vlm_model"])
                self.assertIsNone(caps["vlm_link_id"])
                report = client.post(base + "/review/reports", json={})
                self.assertEqual(report.status_code, 202, report.text)

    def test_peelfeed_llm_env_wins_and_is_injected_child_only(self):
        """Phase 4: ``PEELFEED_LLM_*`` env first, else the answer link."""
        env_model = "env-vlm-model"
        env_link = "vlm-env"
        env_token = "sk-env-vlm-secret"
        with temp_data_root() as root:
            with _cp5_env(root, link=False, key=False) as cli, \
                    _peelfeed_llm_env(
                        PEELFEED_LLM_BASE="http://127.0.0.1:9000/v1",
                        PEELFEED_LLM_MODEL=env_model,
                        PEELFEED_LLM_TOKEN=env_token,
                        PEELFEED_LLM_LINK_ID=env_link):
                client = _client()
                base = _extracted_review_source(client, root)
                payload = client.get(base + "/review").json()
                caps = payload["capabilities"]
                self.assertTrue(caps["vlm"])
                self.assertEqual(caps["vlm_provider"], "env")
                self.assertEqual(caps["vlm_model"], env_model)
                self.assertEqual(caps["vlm_link_id"], env_link)
                confirmation = copy.deepcopy(VLM_CONFIRMATION)
                confirmation.update({"provider": "env", "link_id": env_link,
                                     "model": env_model})
                cli.clear_calls()
                response = client.post(base + "/review/reports", json={
                    "mode": "vlm", "confirmation": confirmation})
                self.assertEqual(response.status_code, 202, response.text)
                report_call = next(item for item in cli.calls()
                                   if item["argv"][0] == "review-report")
                self.assertEqual(report_call["llm_env"], {
                    "PEELFEED_LLM_BASE": "http://127.0.0.1:9000/v1",
                    "PEELFEED_LLM_MODEL": env_model,
                    "PEELFEED_LLM_TOKEN": env_token,
                    "PEELFEED_LLM_LINK_ID": env_link,
                })
                self.assertNotIn(env_token, " ".join(report_call["argv"]))
                # Rules reports never get the env provider.
                rules = client.post(base + "/review/reports", json={})
                self.assertEqual(rules.status_code, 202, rules.text)
                rules_call = [item for item in cli.calls()
                              if item["argv"][0] == "review-report"][-1]
                self.assertEqual(rules_call["llm_env"], {})
                # A discussion with the env provider also gets the child env.
                discuss = client.post(base + "/review/discussions", json={
                    "scope": {"peel": True}, "message": "解释一下这个簇",
                    "base_plan_version": 0, "confirmation": confirmation})
                self.assertEqual(discuss.status_code, 202, discuss.text)
                discuss_call = next(item for item in cli.calls()
                                    if item["argv"][0] == "discuss")
                self.assertEqual(discuss_call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 env_token)
                # The confirmation must name the resolved provider.
                wrong = copy.deepcopy(confirmation)
                wrong["provider"] = "answer_link"
                mismatch = client.post(base + "/review/reports", json={
                    "mode": "vlm", "confirmation": wrong})
                self.assertEqual(mismatch.status_code, 400, mismatch.text)
                self.assertIn("privacy_confirmation_required",
                              mismatch.json()["detail"])
                self.assertNotIn(env_token, json.dumps(cli.state()))

    def test_incomplete_env_provider_falls_back_to_the_answer_link(self):
        """Only the complete trio (BASE/MODEL/TOKEN) activates the env provider."""
        with temp_data_root() as root:
            with _cp5_env(root, link=False, key=False) as cli, \
                    _peelfeed_llm_env(PEELFEED_LLM_BASE="http://x/v1",
                                      PEELFEED_LLM_MODEL="m"):
                client = _client()
                base = _extracted_review_source(client, root)
                caps = client.get(base + "/review").json()["capabilities"]
                self.assertFalse(caps["vlm"])
                self.assertIn("VLM 不可用", caps["vlm_detail"])
                cli.clear_calls()
                response = client.post(base + "/review/reports", json={
                    "mode": "vlm",
                    "confirmation": copy.deepcopy(VLM_CONFIRMATION)})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertIn("vlm_unavailable", response.json()["detail"])
                self.assertFalse([item for item in cli.calls()
                                  if item["argv"][0] == "review-report"])


class Cp5VlmReportRouteTests(unittest.TestCase):
    def test_vlm_report_route_creates_a_job_and_keeps_the_token_child_only(self):
        """CP5 K1/K6 (wire §6.1/§6.4): 202 job, descriptor, no token persisted."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                dataset_id = base.split("/")[3]
                sha = base.split("/")[5]
                cli.clear_calls()
                response = client.post(base + "/review/reports", json={
                    "mode": "vlm",
                    "confirmation": copy.deepcopy(VLM_CONFIRMATION)})
                self.assertEqual(response.status_code, 202, response.text)
                payload = response.json()
                self.assertEqual(set(payload), {"job_id", "status"})
                self.assertEqual(payload["status"], "queued")
                self.assertTrue(payload["job_id"].startswith("report-"))
                job = _wait_review_job(client, base, payload["job_id"])
                self.assertEqual(job["type"], "report")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"], "report.json")
                # Frozen stdin shape (no token) reached the fake CLI.
                self.assertEqual(cli.state()["review_vlm_requests"][-1], {
                    "mode": "vlm",
                    "confirmation": VLM_CONFIRMATION})
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "review-report")
                self.assertIn("--with-vlm", call["argv"])
                self.assertEqual(call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 VLM_LINK_KEY)
                # Token is not in argv, the dataset file or the descriptor.
                self.assertNotIn(VLM_LINK_KEY, " ".join(call["argv"]))
                dataset_text = (
                    root.parent / "home" / "datasets" / dataset_id
                    / "dataset.json").read_text(encoding="utf-8")
                self.assertNotIn(VLM_LINK_KEY, dataset_text)
                rows = _sources(client, dataset_id)
                row = next(item for item in rows if item["sha256"] == sha)
                descriptor = row["extraction"]["review_jobs"][payload["job_id"]]
                self.assertEqual(descriptor["type"], "report")
                self.assertIsNone(descriptor["batch_id"])
                self.assertIsNone(descriptor["conversation_id"])
                self.assertNotIn(VLM_LINK_KEY, json.dumps(descriptor))
                # A rules report afterwards is unchanged and gets no LLM env.
                rules = client.post(base + "/review/reports", json={})
                self.assertEqual(rules.status_code, 202, rules.text)
                rules_call = [item for item in cli.calls()
                              if item["argv"][0] == "review-report"][-1]
                self.assertEqual(rules_call["llm_env"], {})
                self.assertNotIn("--with-vlm", rules_call["argv"])

    def test_vlm_report_rejections_create_no_job(self):
        """CP5 K1/K5 (wire §6.1/§6.5): validation/privacy/unavailable mapping."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                dataset_id = base.split("/")[3]
                sha = base.split("/")[5]
                route = base + "/review/reports"

                def jobs():
                    rows = _sources(client, dataset_id)
                    row = next(item for item in rows
                               if item["sha256"] == sha)
                    return row["extraction"]["review_jobs"]

                cli.clear_calls()
                before = jobs()
                bad_bodies = [
                    {"tier": "R2"},
                    {"mode": "rules"},
                    {"confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"mode": "vlm"},
                    {"mode": "vlm", "confirmation": []},
                    {"mode": "vlm", "confirmation": _confirmation(
                        confirmed=False)},
                    {"mode": "vlm", "confirmation": _confirmation(
                        confirmed="true")},
                    {"mode": "vlm", "confirmation": _confirmation(
                        unknown="x")},
                    {"mode": "vlm", "confirmation": _confirmation_scope(
                        bogus=True)},
                    {"mode": "vlm", "confirmation": _confirmation(
                        provider="env")},
                    {"mode": "vlm", "confirmation": _confirmation(
                        link_id="other-link")},
                    {"mode": "vlm", "confirmation": _confirmation(
                        model="other-model")},
                    {"mode": "vlm", "confirmation": _confirmation_scope(
                        item_ids=["b", "a"])},
                    {"mode": "vlm", "confirmation": _confirmation_scope(
                        include_crop="true")},
                    {"mode": "vlm", "confirmation": _confirmation_scope(
                        item_ids=["it-a", "it-a"])},
                ]
                for body in bad_bodies:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     (body, response.text))
                # Privacy mismatches carry the frozen code.
                for body in bad_bodies[9:12]:
                    response = client.post(route, json=body)
                    self.assertIn("privacy_confirmation_required",
                                  response.json()["detail"])
                # No rejected request created a job or invoked the CLI.
                self.assertEqual(jobs(), before)
                self.assertEqual(
                    [item for item in cli.calls()
                     if item["argv"][0] == "review-report"], [])
                # No answer link -> 503 vlm_unavailable, still no job.
                from peekseed import links
                links.reset_cache()
                for existing in links.links("answer"):
                    links.delete(existing["id"])
                unconfigured = client.post(route, json={
                    "mode": "vlm",
                    "confirmation": copy.deepcopy(VLM_CONFIRMATION)})
                self.assertEqual(unconfigured.status_code, 503,
                                 unconfigured.text)
                self.assertIn("vlm_unavailable",
                              unconfigured.json()["detail"])
                self.assertEqual(jobs(), before)
                # A link whose key env is missing is also 503 with no job.
                _answer_link()
                os.environ.pop(VLM_LINK_KEY_ENV, None)
                missing_key = client.post(route, json={
                    "mode": "vlm",
                    "confirmation": copy.deepcopy(VLM_CONFIRMATION)})
                self.assertEqual(missing_key.status_code, 503,
                                 missing_key.text)
                self.assertIn("vlm_unavailable",
                              missing_key.json()["detail"])
                self.assertEqual(jobs(), before)
                self.assertEqual(
                    [item for item in cli.calls()
                     if item["argv"][0] == "review-report"], [])


class Cp5DiscussionRouteTests(unittest.TestCase):
    def test_discussion_route_creates_a_job_with_conversation_descriptor(self):
        """CP5 K2/K6 (wire §6.2/§6.4): 202 job, descriptor + conversation_id."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                dataset_id = base.split("/")[3]
                sha = base.split("/")[5]
                cli.clear_calls()
                response = client.post(base + "/review/discussions",
                                       json=_discuss_request())
                self.assertEqual(response.status_code, 202, response.text)
                payload = response.json()
                self.assertEqual(set(payload),
                                 {"job_id", "conversation_id", "status"})
                self.assertTrue(payload["job_id"].startswith("discuss-"))
                self.assertTrue(payload["conversation_id"].startswith("conv-"))
                self.assertEqual(payload["status"], "queued")
                job = _wait_review_job(client, base, payload["job_id"])
                self.assertEqual(job["type"], "discuss")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"],
                                 "conversations/%s.jsonl"
                                 % payload["conversation_id"])
                row = next(item for item in _sources(client, dataset_id)
                           if item["sha256"] == sha)
                descriptor = row["extraction"]["review_jobs"][
                    payload["job_id"]]
                self.assertEqual(descriptor["type"], "discuss")
                self.assertEqual(descriptor["conversation_id"],
                                 payload["conversation_id"])
                self.assertIsNone(descriptor["batch_id"])
                # Frozen stdin + child-only env; token never in argv/state.
                self.assertEqual(cli.state()["discuss_requests"][-1],
                                 _discuss_request())
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "discuss")
                self.assertEqual(call["argv"][1:4],
                                 ["peel-" + sha[:8], "--input", "-"])
                self.assertEqual(call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 VLM_LINK_KEY)
                self.assertNotIn(VLM_LINK_KEY, " ".join(call["argv"]))
                self.assertNotIn(VLM_LINK_KEY, json.dumps(descriptor))
                # Follow-up turn reuses the conversation id (peelfeed appends).
                follow = _discuss_request(
                    conversation_id=payload["conversation_id"],
                    message="那前两行呢？")
                second = client.post(base + "/review/discussions",
                                     json=follow)
                self.assertEqual(second.status_code, 202, second.text)
                self.assertEqual(second.json()["conversation_id"],
                                 payload["conversation_id"])

    def test_discussion_request_validation_and_error_mapping(self):
        """CP5 K2 (wire §6.2): strict body, 503/404/400/502 mapping, no job."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/discussions"
                cli.clear_calls()
                bad_bodies = [
                    {"message": "hi"},                       # missing fields
                    {"scope": {"peel": True}, "message": "hi",
                     "base_plan_version": 0},                # missing confirm
                    {"scope": {"item_id": "it-a", "cluster_id": "cl-a"},
                     "message": "hi", "base_plan_version": 0,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {}, "message": "hi", "base_plan_version": 0,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {"peel": False}, "message": "hi",
                     "base_plan_version": 0,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {"peel": True}, "message": "",
                     "base_plan_version": 0,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {"peel": True}, "message": "x" * 4001,
                     "base_plan_version": 0,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {"peel": True}, "message": "hi",
                     "base_plan_version": -1,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                    {"scope": {"peel": True}, "message": "hi",
                     "base_plan_version": 0,
                     "confirmation": _confirmation(provider="env")},
                    {"scope": {"peel": True}, "message": "hi",
                     "base_plan_version": 0, "unknown": 1,
                     "confirmation": copy.deepcopy(VLM_CONFIRMATION)},
                ]
                for body in bad_bodies:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     (body, response.text))
                self.assertEqual(
                    [item for item in cli.calls()
                     if item["argv"][0] == "discuss"], [])
                # Unknown conversation -> 404 (peelfeed not_found).
                unknown = client.post(route, json=_discuss_request(
                    conversation_id="conv-20260101T000000-ffff"))
                self.assertEqual(unknown.status_code, 404, unknown.text)
                # peelfeed job-time errors map through contract §4.
                for code, exit_code, status in (
                        ("validation", 2, 400),
                        ("privacy_confirmation_required", 2, 400),
                        ("vlm_bad_response", 2, 502),
                        ("conflict", 2, 409)):
                    cli.update(discuss_error={"code": code, "exit": exit_code,
                                              "message": code})
                    response = client.post(route, json=_discuss_request())
                    self.assertEqual(response.status_code, status,
                                     (code, response.text))
                cli.update(discuss_error=None)
                # No answer link -> 503 before any CLI call.
                from peekseed import links
                links.reset_cache()
                for existing in links.links("answer"):
                    links.delete(existing["id"])
                discuss_calls_before = len(
                    [item for item in cli.calls()
                     if item["argv"][0] == "discuss"])
                unconfigured = client.post(route, json=_discuss_request())
                self.assertEqual(unconfigured.status_code, 503,
                                 unconfigured.text)
                self.assertIn("vlm_unavailable",
                              unconfigured.json()["detail"])
                self.assertEqual(
                    len([item for item in cli.calls()
                         if item["argv"][0] == "discuss"]),
                    discuss_calls_before)


class Cp5PlanPatchRouteTests(unittest.TestCase):
    def test_plan_patch_route_is_synchronous_and_never_applies(self):
        """CP5 K3/K4 (wire §5/§6.3): 200 plan+diff; draft only; no LLM env."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                # CP5.1 H5: a VLM-sourced patch must name the archived assistant
                # turn; create a discussion whose assistant turn carries the
                # default draft patch, then adopt that exact patch.
                cli.update(discuss_result=copy.deepcopy(DEFAULT_VLM_DISCUSS))
                discussion = client.post(base + "/review/discussions",
                                         json=_discuss_request())
                self.assertEqual(discussion.status_code, 202,
                                 discussion.text)
                conversation_id = discussion.json()["conversation_id"]
                cli.clear_calls()
                body = {"base_plan_version": 0,
                        "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                        "source": "vlm",
                        "conversation_id": conversation_id,
                        "turn": 2}
                response = client.post(base + "/review/plan/patches",
                                       json=body)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["review_schema"], 2)
                self.assertEqual(payload["plan_version"], 1)
                self.assertEqual(payload["diff"]["added"][0]["item_id"],
                                 "it-3f9c2a4b6d8e0f1a")
                self.assertEqual(payload["diff"]["changed"], [])
                self.assertEqual(payload["diff"]["removed"], [])
                self.assertEqual(payload["diff"]["unchanged"], 0)
                self.assertEqual(payload["entries"][0]["status"], "draft")
                # The archived patch was submitted unchanged -> source vlm with
                # the conversation origin (wire §10.5).
                self.assertEqual(payload["entries"][0]["source"], "vlm")
                self.assertEqual(payload["entries"][0]["origin"], {
                    "kind": "conversation",
                    "conversation_id": conversation_id, "turn": 2,
                    "model": VLM_MODEL, "prompt_version": "peel-reviews@1"})
                call = next(item for item in cli.calls()
                            if item["argv"][0] == "plan")
                self.assertEqual(call["argv"][1:5],
                                 ["peel-" + base.split("/")[5][:8], "patch",
                                  "--input", "-"])
                self.assertEqual(call["llm_env"], {})
                self.assertEqual(cli.state()["plan_patch_requests"][-1], body)
                # The route never confirms/applies: no batch/resolution exists.
                self.assertFalse(cli.state().get("batches"))
                self.assertFalse(cli.state().get("resolutions"))
                # A no-op repeat does not bump the plan version.
                noop = client.post(base + "/review/plan/patches", json={
                    "base_plan_version": 1,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "user"})
                self.assertEqual(noop.status_code, 200, noop.text)
                self.assertEqual(noop.json()["plan_version"], 1)
                self.assertEqual(noop.json()["diff"]["unchanged"], 1)

    def test_plan_patch_conflicts_and_validation(self):
        """CP5 K4 (wire §5/§6.3): base/locked 409, validation 400, 404, no apply."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan/patches"
                # Base mismatch -> 409 conflict (source user needs no turn).
                stale = client.post(route, json={
                    "base_plan_version": 7,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "user"})
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertIn("conflict", stale.json()["detail"])
                # Locked entry in the patch target -> 409 with conflict ids.
                plan_version, entry_id = _confirm_operation_entry(
                    client, base)
                locked = client.post(base + "/review/plan", json={
                    "base_plan_version": plan_version, "action": "lock",
                    "entry_id": entry_id})
                self.assertEqual(locked.status_code, 200, locked.text)
                locked_version = locked.json()["plan_version"]
                conflict = client.post(route, json={
                    "base_plan_version": locked_version,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "user"})
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn("conflicts:", conflict.json()["detail"])
                self.assertIn("it-3f9c2a4b6d8e0f1a",
                              conflict.json()["detail"])
                # The rejected patch changed nothing (entry stays locked).
                self.assertEqual(cli.state()["plan"]["plan_version"],
                                 locked_version)
                self.assertTrue(cli.state()["plan"]["entries"][0]["locked"])
                # Envelope validation is a 400 and never invokes the CLI.
                cli.clear_calls()
                bad_bodies = [
                    {"patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm"},
                    {"base_plan_version": 0, "source": "vlm"},
                    {"base_plan_version": 0, "patch": [],
                     "source": "vlm"},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "model"},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm", "tier": "R2"},
                    # CP5.1 H5 (wire §10.5): source vlm requires the archived
                    # conversation and assistant turn number.
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm"},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm", "conversation_id": "conv-1"},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm", "conversation_id": "conv-1", "turn": 0},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm", "conversation_id": "conv-1",
                     "turn": True},
                    {"base_plan_version": 0,
                     "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                     "source": "vlm", "conversation_id": "conv-1",
                     "turn": "2"},
                ]
                for body in bad_bodies:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     (body, response.text))
                self.assertEqual(
                    [item for item in cli.calls()
                     if item["argv"][0] == "plan"], [])
                # Unknown conversation -> 404 (with the required turn present).
                unknown = client.post(route, json={
                    "base_plan_version": 0,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "vlm",
                    "conversation_id": "conv-20260101T000000-ffff",
                    "turn": 2})
                self.assertEqual(unknown.status_code, 404, unknown.text)
                # Invalid strategy/params -> peelfeed validation 400 (use an
                # item that is not locked so the conflict check does not fire).
                bad_patch = copy.deepcopy(VLM_DISCUSS_PATCH)
                bad_patch["scope"] = {"kind": "item",
                                      "item_id": "it-8b7a6c5d4e3f2a10",
                                      "cluster_id": None}
                bad_patch["members"] = ["it-8b7a6c5d4e3f2a10"]
                bad_patch["changes"] = [{
                    "op": "select", "item_id": "it-8b7a6c5d4e3f2a10",
                    "strategy": {"id": "no_such_strategy", "version": 1},
                    "params": {},
                }]
                invalid = client.post(route, json={
                    "base_plan_version": locked_version, "patch": bad_patch,
                    "source": "user"})
                self.assertEqual(invalid.status_code, 400, invalid.text)


class Cp51H7EnvForwardingTests(unittest.TestCase):
    """CP5.1 H7 (wire §10.7): tuning env reaches every child, provider does not.

    peekseed forwards the non-secret ``PEELFEED_LLM_*`` tuning variables from
    its own environment to **all** peelfeed children (read-only commands
    included) so a deployed budget/price/timeout/vision/cap config reaches the
    engine; ``PEELFEED_LLM_PROVIDER`` (the local/test command seam) is never
    forwarded, and BASE/MODEL/TOKEN/LINK_ID stay child-only for report/discuss.
    """

    TUNING = {
        "PEELFEED_LLM_PROMPT_SET": "peel-reviews@1",
        "PEELFEED_LLM_TIMEOUT": "90",
        "PEELFEED_LLM_BUDGET_USD": "1.50",
        "PEELFEED_LLM_PRICE_INPUT": "0.001",
        "PEELFEED_LLM_PRICE_OUTPUT": "0.002",
        "PEELFEED_LLM_VISION": "1",
        "PEELFEED_LLM_MAX_CALLS_PER_JOB": "4",
        "PEELFEED_LLM_MAX_CALLS_PER_PEEL": "100",
        "PEELFEED_LLM_MAX_IMAGES_PER_TURN": "6",
        "PEELFEED_LLM_JSON_MODE": "1",
    }

    @contextlib.contextmanager
    def _tuning_env(self):
        saved = {key: os.environ.get(key) for key in
                 list(self.TUNING) + ["PEELFEED_LLM_PROVIDER"]}
        os.environ.update(self.TUNING)
        # The seam must never reach a child: if it did, the fake CLI would
        # execute it instead of answering from its state file.
        os.environ["PEELFEED_LLM_PROVIDER"] = (
            "sh -c 'echo never-forwarded; exit 2'")
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_read_only_children_get_tuning_env_but_never_provider(self):
        with temp_data_root() as root:
            with _cp5_env(root) as cli, self._tuning_env():
                from peekseed import peelfeed
                engine = peelfeed.client()
                engine.capabilities()
                engine.status_all()
                call = cli.calls()[-1]
                for key, value in self.TUNING.items():
                    self.assertEqual(call["llm_env"].get(key), value, key)
                self.assertNotIn("PEELFEED_LLM_PROVIDER", call["llm_env"])
                self.assertNotIn("PEELFEED_LLM_TOKEN", call["llm_env"])
                self.assertNotIn("PEELFEED_LLM_BASE", call["llm_env"])
                self.assertNotIn("PEELFEED_LLM_MODEL", call["llm_env"])
                # The seam is forwarded to no child and persisted nowhere.
                self.assertNotIn("PEELFEED_LLM_PROVIDER",
                                 json.dumps(cli.state()))
                self.assertFalse([item for item in cli.calls()
                                  if "never-forwarded" in item.get("argv", [])])

    def test_report_and_discuss_children_get_provider_plus_tuning(self):
        with temp_data_root() as root:
            source = make_pdf(root / "manual.pdf")
            with _cp5_env(root) as cli, self._tuning_env():
                from peekseed import peelfeed
                engine = peelfeed.client()
                peel_id = engine.ingest(str(source), llm_mode="off")[
                    "peel"]["peel_id"]
                cli.clear_calls()
                engine.review_report(
                    peel_id, mode="vlm",
                    confirmation=copy.deepcopy(VLM_CONFIRMATION))
                report_call = next(item for item in cli.calls()
                                   if item["argv"][0] == "review-report")
                self.assertEqual(report_call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 VLM_LINK_KEY)
                self.assertEqual(report_call["llm_env"]["PEELFEED_LLM_MODEL"],
                                 VLM_MODEL)
                for key, value in self.TUNING.items():
                    self.assertEqual(report_call["llm_env"].get(key), value,
                                     key)
                self.assertNotIn("PEELFEED_LLM_PROVIDER",
                                 report_call["llm_env"])
                engine.discuss(peel_id, _discuss_request())
                discuss_call = next(item for item in cli.calls()
                                    if item["argv"][0] == "discuss")
                self.assertEqual(discuss_call["llm_env"]["PEELFEED_LLM_TOKEN"],
                                 VLM_LINK_KEY)
                for key, value in self.TUNING.items():
                    self.assertEqual(discuss_call["llm_env"].get(key), value,
                                     key)
                self.assertNotIn("PEELFEED_LLM_PROVIDER",
                                 discuss_call["llm_env"])
                # No token in argv or any persisted fake state.
                self.assertNotIn(VLM_LINK_KEY,
                                 " ".join(report_call["argv"]))
                self.assertNotIn(VLM_LINK_KEY, json.dumps(cli.state()))

    def test_review_capabilities_preserve_priced_and_per_peel_cap(self):
        """The overlay must keep peelfeed's additive pricing/cap fields."""
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                review = copy.deepcopy(REVIEW_VLM)
                review["capabilities"].update({"vlm_priced": False,
                                               "vlm_max_calls_per_peel": 100})
                cli.update(review=review)
                caps = client.get(base + "/review").json()["capabilities"]
                self.assertIs(caps["vlm_priced"], False)
                self.assertEqual(caps["vlm_max_calls_per_peel"], 100)
                self.assertTrue(caps["vlm"])
                # Absent fields stay absent (no invented default).
                self.assertNotIn("vlm_priced",
                                 DEFAULT_REVIEW["capabilities"])


class Cp51H5AcceptSourceTests(unittest.TestCase):
    """CP5.1 H5 (wire §10.5): per-item adoption source + origin/ambiguity."""

    ITEM_ID = "it-3f9c2a4b6d8e0f1a"

    @staticmethod
    def _suggestion(item_id, strategy_id, source, *, version=1, params=None):
        return {
            "item_id": item_id,
            "strategy": {"id": strategy_id, "version": version},
            "params": params if params is not None else {},
            "evidence_refs": [],
            "validator": {"status": "ready", "reasons": []},
            "in_plan": False,
            "state": "ready",
            "source": source,
        }

    def _report(self, suggestions, *, job_id=None, provider=None):
        report = copy.deepcopy(DEFAULT_REPORT)
        report["report"]["clusters"] = [{
            "cluster_id": "cl-0f1e2d3c4b5a",
            "item_ids": [self.ITEM_ID],
            "conclusion": "规则与 VLM 建议不一致。",
            "suggestions": suggestions,
            "clarifications": [],
        }]
        if job_id is not None:
            report["report"]["job_id"] = job_id
        if provider is not None:
            report["report"]["provider"] = provider
        return report

    def test_ambiguous_suggestion_needs_sources_and_records_origin(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                report = self._report(
                    [self._suggestion(self.ITEM_ID, "use_first_row_header",
                                      "rule"),
                     self._suggestion(self.ITEM_ID, "confirm_no_header",
                                      "vlm")],
                    job_id="report-20261109T031000-abc1",
                    provider={"kind": "vlm", "model": VLM_MODEL,
                              "prompt_version": "peel-reviews@1"})
                cli.update(review_report=report)
                cli.clear_calls()
                # Without ``sources`` the differing ready suggestions are
                # ambiguous -> 400 all-or-nothing, no plan write.
                ambiguous = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": [self.ITEM_ID]})
                self.assertEqual(ambiguous.status_code, 400, ambiguous.text)
                self.assertIn(self.ITEM_ID + "=ambiguous_suggestion",
                              ambiguous.json()["detail"])
                self.assertEqual((cli.state().get("plan") or {}).get("plan_version", 0), 0)
                self.assertFalse((cli.state().get("plan") or {}).get("entries"))
                # With ``sources`` the requested suggestion is adopted and the
                # plan entry/accepted row record the actual source and origin.
                accepted = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": [self.ITEM_ID],
                    "sources": {self.ITEM_ID: "vlm"}})
                self.assertEqual(accepted.status_code, 200, accepted.text)
                plan = accepted.json()
                self.assertEqual(plan["accepted"][0]["source"], "vlm")
                self.assertEqual(plan["accepted"][0]["origin"], {
                    "kind": "report",
                    "job_id": "report-20261109T031000-abc1",
                    "model": VLM_MODEL,
                    "prompt_version": "peel-reviews@1"})
                entry = plan["entries"][0]
                self.assertEqual(entry["source"], "vlm")
                self.assertEqual(entry["origin"], plan["accepted"][0]["origin"])
                self.assertEqual(entry["strategy"]["id"], "confirm_no_header")
                # The exact stdin request carried the sources map.
                self.assertEqual(cli.state()["accept_requests"][-1], {
                    "base_plan_version": 0, "item_ids": [self.ITEM_ID],
                    "sources": {self.ITEM_ID: "vlm"}})
                # Same-identity suggestions are not ambiguous: rule preferred.
                cli.update(plan=None)
                cli.update(review_report=self._report([
                    self._suggestion(self.ITEM_ID, "use_first_row_header",
                                     "rule"),
                    self._suggestion(self.ITEM_ID, "use_first_row_header",
                                     "vlm")]))
                same = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": [self.ITEM_ID]})
                self.assertEqual(same.status_code, 200, same.text)
                self.assertEqual(same.json()["accepted"][0]["source"], "rule")

    def test_source_missing_and_strict_body_validation(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan"
                cli.clear_calls()
                # The default report only has a rule suggestion; asking for vlm
                # is a per-item failure ``source_missing``.
                missing = client.post(route, json={
                    "base_plan_version": 0, "action": "accept",
                    "item_ids": [self.ITEM_ID],
                    "sources": {self.ITEM_ID: "vlm"}})
                self.assertEqual(missing.status_code, 400, missing.text)
                self.assertIn(self.ITEM_ID + "=source_missing",
                              missing.json()["detail"])
                self.assertEqual((cli.state().get("plan") or {}).get("plan_version", 0), 0)
                self.assertFalse((cli.state().get("plan") or {}).get("entries"))
                # Strict envelope: unknown field, non-object sources, unknown
                # key, bad value are all 400 and never reach the CLI.
                cli.clear_calls()
                bad_bodies = [
                    {"base_plan_version": 0, "action": "accept",
                     "item_ids": [self.ITEM_ID], "tier": "R2"},
                    {"base_plan_version": 0, "action": "accept",
                     "item_ids": [self.ITEM_ID], "sources": ["rule"]},
                    {"base_plan_version": 0, "action": "accept",
                     "item_ids": [self.ITEM_ID],
                     "sources": {"it-ffffffffffffffff": "rule"}},
                    {"base_plan_version": 0, "action": "accept",
                     "item_ids": [self.ITEM_ID],
                     "sources": {self.ITEM_ID: "model"}},
                ]
                for body in bad_bodies:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     (body, response.text))
                self.assertEqual(
                    [item for item in cli.calls()
                     if item["argv"][0] == "plan"], [])
                self.assertEqual(
                    (cli.state().get("plan") or {}).get("plan_version", 0), 0)


class Cp51H5PlanPatchTurnTests(unittest.TestCase):
    """CP5.1 H5 (wire §10.5): patch turn/source determination + origin."""

    def _conversation(self, client, base, cli, patch=None):
        result = copy.deepcopy(DEFAULT_VLM_DISCUSS)
        if patch is not None:
            result["patch"] = copy.deepcopy(patch)
        cli.update(discuss_result=result)
        response = client.post(base + "/review/discussions",
                               json=_discuss_request())
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["conversation_id"]

    def test_identical_patch_is_vlm_and_edited_patch_is_user_with_origin(self):
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan/patches"
                conversation_id = self._conversation(client, base, cli)
                # The archived assistant patch, submitted unchanged -> source vlm.
                identical = client.post(route, json={
                    "base_plan_version": 0,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "vlm",
                    "conversation_id": conversation_id, "turn": 2})
                self.assertEqual(identical.status_code, 200, identical.text)
                entry = identical.json()["entries"][0]
                self.assertEqual(entry["source"], "vlm")
                self.assertEqual(entry["origin"], {
                    "kind": "conversation",
                    "conversation_id": conversation_id, "turn": 2,
                    "model": VLM_MODEL, "prompt_version": "peel-reviews@1"})
                # A user-edited patch is recorded as source user but keeps the
                # conversation origin (wire §10.5).
                edited_patch = copy.deepcopy(VLM_DISCUSS_PATCH)
                edited_patch["changes"][0]["params"] = {"rows": [1]}
                edited = client.post(route, json={
                    "base_plan_version": identical.json()["plan_version"],
                    "patch": edited_patch, "source": "vlm",
                    "conversation_id": conversation_id, "turn": 2})
                self.assertEqual(edited.status_code, 200, edited.text)
                entry = edited.json()["entries"][0]
                self.assertEqual(entry["source"], "user")
                self.assertEqual(entry["origin"]["kind"], "conversation")
                self.assertEqual(entry["origin"]["turn"], 2)
                self.assertEqual(entry["action"]["params"], {"rows": [1]})

    def test_turn_validation_and_unknown_conversation(self):
        with temp_data_root() as root:
            with _cp5_env(root) as cli:
                client = _client()
                base = _extracted_review_source(client, root)
                route = base + "/review/plan/patches"
                conversation_id = self._conversation(client, base, cli)
                cli.clear_calls()
                # No archived assistant patch with that number -> validation.
                missing_turn = client.post(route, json={
                    "base_plan_version": 0,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "vlm",
                    "conversation_id": conversation_id, "turn": 8})
                self.assertEqual(missing_turn.status_code, 400,
                                 missing_turn.text)
                self.assertIn("validation", missing_turn.json()["detail"])
                # Unknown conversation -> 404.
                unknown = client.post(route, json={
                    "base_plan_version": 0,
                    "patch": copy.deepcopy(VLM_DISCUSS_PATCH),
                    "source": "vlm",
                    "conversation_id": "conv-20260101T000000-ffff",
                    "turn": 2})
                self.assertEqual(unknown.status_code, 404, unknown.text)
                self.assertIn("not_found", unknown.json()["detail"])


class Cp51H9ReingestTests(unittest.TestCase):
    """CP5.1 H9 (wire §10.9): a re-extracting source is never marked early."""

    def test_slow_reingest_of_ready_peel_stays_queued_until_worker_finishes(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                first = client.post(
                    "/api/datasets/%s/sources/%s/extract"
                    % (dataset["id"], sha), json={})
                self.assertEqual(first.status_code, 200, first.text)
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "extracted")
                self.assertTrue(row["extraction"]["peel_id"])
                # Re-ingest an already-ready peel: the fake keeps the previous
                # ``ready`` peel visible for one second, exactly like peelfeed
                # before it publishes the new build.
                cli.update(ingest_seconds=1.0, ingest_keep_previous_state=True)
                second = client.post(
                    "/api/datasets/%s/sources/%s/extract"
                    % (dataset["id"], sha), json={})
                self.assertEqual(second.status_code, 200, second.text)
                self.assertTrue(second.json()["peel"]["reused"], second.text)
                # While the worker runs, GET …/sources must report only
                # queued/running -- never the previous peel's ``extracted``.
                deadline = time.time() + 0.6
                window_states = []
                while time.time() < deadline:
                    state = _sources(client, dataset["id"])[0][
                        "extraction"]["state"]
                    window_states.append(state)
                    self.assertNotIn(
                        state, ("extracted", "failed"),
                        "H9: source became terminal while the re-ingest "
                        "worker was still running: %r" % (window_states,))
                    time.sleep(0.05)
                self.assertTrue(set(window_states) <= {"queued", "running"},
                                window_states)
                # An index build during the window must not see ``extracted``.
                index = client.post("/api/datasets/%s/index" % dataset["id"],
                                    json={})
                self.assertEqual(index.status_code, 409, index.text)
                self.assertIn("no_extracted_sources", index.json()["detail"])
                # After the worker finishes its authoritative write the source
                # is extracted again (the registry entry is gone).
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "extracted")

    def test_restart_recovery_is_unchanged_without_an_active_worker(self):
        """No worker active: a terminal peelfeed state is still persisted."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with peelfeed_env(CLI=str(cli.path), POLL="0.05"):
                client = _client()
                dataset, _pdf = _upload(client, root)
                sha = _sources(client, dataset["id"])[0]["sha256"]
                client.post("/api/datasets/%s/sources/%s/extract"
                            % (dataset["id"], sha), json={})
                row = _wait_source(client, dataset["id"], sha)
                self.assertEqual(row["extraction"]["state"], "extracted")
                from peekseed import app as app_module, config, ingest
                directory = config.paths().datasets_dir / dataset["id"]
                # Simulate a crashed worker: the dataset says running, peelfeed
                # says ready, and no registry entry exists.
                stale = dict(row["extraction"])
                stale.update({"state": "running", "finished_at": None})
                ingest.update_source(directory, sha, status="running",
                                     extraction=stale)
                recovered = _sources(client, dataset["id"])[0]
                self.assertEqual(recovered["extraction"]["state"], "extracted")
                self.assertFalse(app_module._extraction_active(directory, sha))


class NoHttpContractTests(unittest.TestCase):
    def test_the_adapter_has_no_http_or_token_code_path(self):
        """The seam stays CLI-only. CP5 (wire §1.3) may pass a link's
        ``base_url`` to the report/discuss **child env**, but the adapter still
        has no HTTP client, no Authorization header and no token in argv."""
        import inspect
        from peekseed import peelfeed
        source = inspect.getsource(peelfeed)
        for marker in ("httpx", "Authorization", "Bearer",
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

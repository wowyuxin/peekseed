"""CP3 W5: Review UI contract tests for ``peekseed/web/index.html``.

The CP3 UI adds the candidate-batch flow (estimate -> build -> gates -> actual
diff -> publish/discard), persistent job polling with an explicit retry for
``interrupted``, the published-batch list + rollback, the
``corrections_conflict`` banners and the explicit ``review_schema`` mismatch
message (wire-contract §2.5/§2.7/§2.9, contract v1.3 §4/§9, design §6).

These tests assert the **served** HTML/JS contract: the controls, the frozen
routes and the Chinese labels for stable codes live in the UI file; the backend
routes are covered by ``tests/test_peelfeed.py``. Fake-CLI round trips for the
CP3 batch/job commands are owned by the backend stream's fixtures, so this file
does not depend on them (the real-payload DOM render driver belongs to the CP3
evidence stream).
"""

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "peekseed" / "web" / "index.html"


def _ui():
    return UI_PATH.read_text(encoding="utf-8")


@contextlib.contextmanager
def _served_home():
    """Point peekseed at a throwaway home + shared folder for the served page."""
    from peekseed import app as app_module
    from peekseed import config, links

    previous = {key: os.environ.get(key)
                for key in ("PEEKSEED_HOME", "PEEKSEED_SHARED_DIR")}
    base = Path(tempfile.mkdtemp(prefix="peekseed-review-ui-"))
    home = base / "home"
    shared = base / "shared"
    home.mkdir()
    shared.mkdir()
    os.environ["PEEKSEED_HOME"] = str(home)
    os.environ["PEEKSEED_SHARED_DIR"] = str(shared)
    config.reset_cache()
    links.reset_cache()
    app_module.reset_state()
    try:
        yield home
    finally:
        app_module.reset_state()
        config.reset_cache()
        links.reset_cache()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(str(base), ignore_errors=True)


class ReviewUiCp3Tests(unittest.TestCase):
    def test_review_modal_ships_cp3_batch_and_conflict_containers(self):
        text = _ui()
        for marker in ('id="reviewBatch"', 'id="reviewConflict"',
                       'id="reviewMsg"', 'id="reviewSummary"',
                       'id="reviewClusters"', 'id="reviewDetail"'):
            self.assertIn(marker, text)
        # the batch section is inside the review dialog, before the cluster grid
        dialog = text.index('id="reviewModal"')
        batch = text.index('id="reviewBatch"')
        clusters = text.index('id="reviewClusters"')
        self.assertLess(dialog, batch)
        self.assertLess(batch, clusters)

    def test_batch_section_build_and_r0_noop(self):
        text = _ui()
        for marker in (
                "构建本批候选", "无需重建 (R0/no-op)",
                "R0/暂缓/仅报告不会触发构建，也不会创建批次",
                "已确认 operation 条目", "构建/发布期间这些条目只读",
                'data-r="build"', "reviewConfirmedEntries",
                "entry_ids:ctx.lastBuildEntryIds",
                "reviewBase(ctx.dataset,ctx.sha)+'/batches'",
                "function reviewBuildCandidate",
                "function reviewBatchSectionHtml",
                "function reviewConfirmedEntries",
                "REVIEW_BATCH_ACTIVE", "REVIEW_BATCH_RELEASED"):
            self.assertIn(marker, text)
        # the request carries only entry_ids (wire §2.6: no tier/scope)
        build = text.index("async function reviewBuildCandidate")
        build_body = text[build:text.index("async function reviewPublishBatch")]
        self.assertIn("JSON.stringify({entry_ids:ctx.lastBuildEntryIds})",
                      build_body)
        for forbidden in ("tier:", "scope:", "operations:"):
            self.assertNotIn(forbidden, build_body)

    def test_job_panel_statuses_and_interrupted_retry(self):
        text = _ui()
        for label in ("排队中", "运行中", "已完成", "失败", "已中断"):
            self.assertIn(label, text)
        for marker in (
                "REVIEW_JOB_STATUS_LABEL", "REVIEW_JOB_TYPE_LABEL",
                "REVIEW_JOB_TERMINAL", "任务中断，可手动重试",
                "不会自动重跑", 'data-r="job-retry"',
                "function reviewPollJob", "function reviewJobsHtml",
                "function reviewJobRowHtml", "function reviewJobError",
                "progress.done", "progress.total", "rr-progress"):
            self.assertIn(marker, text)
        # the job panel re-attaches persisted descriptors by id (never reruns)
        self.assertIn("extraction.review_jobs", text)
        self.assertIn("ctx.jobs[jobId]={job_id:jobId", text)

    def test_candidate_panel_estimate_gates_diff_and_actions(self):
        text = _ui()
        for marker in (
                "function reviewCandidatePanelHtml", "估算", "Docling 重跑",
                "实际变更", "assets_changed", "blocks_changed",
                "chunks_changed", "new_warnings", "changed_assets",
                "samples", "carried_corrections", "gates ",
                "gates.errors", "build.carried_corrections",
                "batch.estimate", "batch.build", "batch.failures",
                'data-r="publish"', 'data-r="discard"',
                "reviewBase(ctx.dataset,ctx.sha)\n      +'/batches/'",
                "/publish", "/discard"):
            self.assertIn(marker, text)
        for label in ("待发布", "发布中", "已发布", "已放弃", "已过期",
                      "估算中", "构建中", "校验中"):
            self.assertIn(label, text)

    def test_stale_publish_blocks_with_rebuild_message(self):
        text = _ui()
        for marker in ("候选已过期，需重新构建", "ctx.candidateStale=true",
                       "busy||stale?' disabled'", "function reviewPublishBatch"):
            self.assertIn(marker, text)
        publish = text.index("async function reviewPublishBatch")
        publish_body = text[publish:text.index("async function reviewDiscardBatch")]
        self.assertIn("e.status===409", publish_body)
        self.assertIn("ctx.candidateStale=true", publish_body)
        self.assertIn("ready_to_publish", publish_body)

    def test_published_batches_and_rollback(self):
        text = _ui()
        for marker in (
                "已发布批次", "回滚最近一批", 'data-r="rollback"',
                "function reviewPublishedBatches",
                "function reviewPublishedBatchesHtml",
                "function reviewRollback",
                "reviewBase(ctx.dataset,ctx.sha)+'/rollback'",
                "仅最近一批", "published"):
            self.assertIn(marker, text)

    def test_batch_entries_read_only_while_building(self):
        text = _ui()
        for marker in (
                "function reviewBatchEntryLocked", "function reviewBatchActive",
                "构建/发布期间只读", "批次中", "entry.batch_id",
                "reviewBatchEntryLocked(item&&item.entry)",
                "reviewBatchEntryLocked(entry)"):
            self.assertIn(marker, text)
        actions = text.index("function reviewActionsHtml")
        actions_body = text[actions:text.index("function reviewItemHtml")]
        # select/confirm/lock/unlock/clear/defer are disabled for a locked entry
        self.assertIn("batchLocked", actions_body)
        self.assertIn("canConfirm=hasEntry&&(!stale||refreshable)&&!batchLocked",
                      actions_body)
        self.assertIn("canClear=hasEntry&&!locked&&!batchLocked&&!busy",
                      actions_body)

    def test_corrections_conflict_banner_dataset_and_review(self):
        text = _ui()
        for marker in (
                "corrections_conflict", "修正冲突", "ds-conflict",
                "ds-src-reason", "function datasetConflictHtml",
                "function reviewConflictHtml",
                "function reviewConflictFailuresHtml",
                "function reviewConflictSummaryText",
                "function paintReviewConflict",
                "function reviewConflictJump",
                "conflicting", "source_batches", "failures",
                "打开审阅处理", "定位冲突条目",
                "历史修正失效", "current 保持不变",
                'data-r="conflict-jump"'):
            self.assertIn(marker, text)
        # the dataset row and the source chip both surface the conflict state
        self.assertIn("datasetConflictHtml(ds)", text)
        self.assertIn("state==='corrections_conflict'", text)
        self.assertIn("corrections_conflict:'修正冲突'", text)

    def test_review_schema_mismatch_is_explicit(self):
        text = _ui()
        for marker in (
                "peelfeed review schema 不一致",
                "请同步升级 peelfeed 与 peekseed",
                "reviewLoadErrText", "reviewErrText",
                "review[_ ]schema"):
            self.assertIn(marker, text)

    def test_publish_and_rollback_refresh_datasets(self):
        text = _ui()
        for marker in ("reviewRefreshDatasets", "await loadDatasets()",
                       "DATASET_SOURCES", "refreshReview(",
                       "所有引用该 peel 的 dataset 已刷新",
                       "conversion 变化时索引需要 reindex"):
            self.assertIn(marker, text)
        publish = text.index("async function reviewPublishBatch")
        publish_body = text[publish:text.index("async function reviewDiscardBatch")]
        self.assertIn("await reviewRefreshDatasets()", publish_body)
        rollback = text.index("async function reviewRollback")
        rollback_body = text[rollback:text.index("async function reviewRetryJob")]
        self.assertIn("await reviewRefreshDatasets()", rollback_body)

    def test_polling_never_reruns_a_job_automatically(self):
        text = _ui()
        poll_start = text.index("async function reviewPollJob(jobId)")
        poll_end = text.index("async function reviewRefreshDatasets()")
        poll_body = text[poll_start:poll_end]
        for forbidden in ("reviewBuildCandidate(", "reviewRollback(",
                          "reviewRetryJob(", "generateReviewReport(",
                          "setInterval"):
            self.assertNotIn(forbidden, poll_body)
        # interrupted only offers the explicit retry button
        self.assertIn("任务中断，可手动重试（不会自动重跑）", text)
        self.assertIn('data-r="job-retry"', text)

    def test_cp2_review_contract_still_present(self):
        text = _ui()
        for marker in ("reviewModal", "reviewStrategyParamsHtml",
                       "REVIEW_STALE_LABEL", "REVIEW_ACCEPT_REASON_LABEL",
                       "plan.accept", "parent_candidates",
                       "data-r=\"accept-pick\"", "确认选中项",
                       "reviewReportBtn", "reviewRefreshBtn"):
            self.assertIn(marker, text)
        # CP1 removed the flat Accept/Reject + Apply & rebuild UI; it must stay
        # removed (CP3 replaces it with the batch flow).
        for gone in ("reviewRebuildBtn", "decideReview", "rebuildReviewed"):
            self.assertNotIn(gone, text)

    def test_served_page_ships_cp3_controls(self):
        with _served_home():
            from fastapi.testclient import TestClient
            from peekseed import app as app_module

            response = TestClient(app_module.create_app()).get("/")
            self.assertEqual(response.status_code, 200, response.text)
            page = response.text
        for marker in ('id="reviewBatch"', 'id="reviewConflict"',
                       "构建本批候选", "无需重建 (R0/no-op)",
                       "任务中断，可手动重试", "候选已过期，需重新构建",
                       "回滚最近一批", "修正冲突",
                       "peelfeed review schema 不一致",
                       "REVIEW_BATCH_STATUS_LABEL", "REVIEW_JOB_STATUS_LABEL",
                       "reviewPublishBatch", "reviewRollback",
                       "datasetConflictHtml", "review_schema"):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# One fake-CLI-driven HTTP round trip: the UI consumes the frozen §2.5/§2.7
# payloads (job record with progress/result_ref, ApplyBatch with
# estimate/build.gates/build.diff, publish {batch,current}, rollback job).
# The backend stream's fixtures now implement the CP3 CLI commands, so this
# verifies the shapes the JS renders instead of only asserting static text.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _peelfeed_cli_env(cli_path):
    """Point the peelfeed adapter at one fake CLI and reset its caches."""
    from peekseed import peelfeed

    keys = ("PEEKSEED_PEELFEED_CLI", "PEEKSEED_PEELFEED_POLL")
    previous = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    os.environ["PEEKSEED_PEELFEED_CLI"] = str(cli_path)
    os.environ["PEEKSEED_PEELFEED_POLL"] = "0.01"
    peelfeed.reset_cache()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        peelfeed.reset_cache()


def _test_client():
    from fastapi.testclient import TestClient
    from peekseed import app as app_module

    app_module.reset_state()
    return TestClient(app_module.create_app())


def _wait_extracted(client, dataset_id, sha, timeout=10.0):
    import time

    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        rows = client.get("/api/datasets/%s/sources" % dataset_id).json()["sources"]
        row = next((item for item in rows if item["sha256"] == sha), {})
        state = (row.get("extraction") or {}).get("state")
        if state in ("extracted", "failed", "stale"):
            return state
        time.sleep(0.02)
    raise AssertionError("source %s did not extract: %r" % (sha[:8], state))


class ReviewUiCp3ApiRoundTripTests(unittest.TestCase):
    def test_batch_job_publish_rollback_payloads_match_the_ui(self):
        from tests.fixtures import FakePeelfeedCli, make_pdf, temp_data_root

        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _test_client()
                pdf = make_pdf(root / "manual.pdf")
                created = client.post(
                    "/api/datasets", data={"name": "Docs"},
                    files=[("files", ("manual.pdf", pdf.read_bytes(),
                                      "application/pdf"))])
                self.assertEqual(created.status_code, 201, created.text)
                dataset_id = created.json()["dataset"]["id"]
                sources = client.get(
                    "/api/datasets/%s/sources" % dataset_id).json()["sources"]
                sha = sources[0]["sha256"]
                base = "/api/datasets/%s/sources/%s" % (dataset_id, sha)
                extracted = client.post(base + "/extract", json={})
                self.assertEqual(extracted.status_code, 200, extracted.text)
                self.assertEqual(_wait_extracted(client, dataset_id, sha),
                                 "extracted")
                review = client.get(base + "/review?group=cluster")
                self.assertEqual(review.status_code, 200, review.text)
                # CP2 frozen plan route: select + confirm one operation entry.
                selected = client.post(base + "/review/plan", json={
                    "base_plan_version": 0, "action": "select",
                    "entry": {"item_id": "it-3f9c2a4b6d8e0f1a",
                              "strategy": {"id": "use_first_row_header",
                                           "version": 1},
                              "params": {"rows": [0]}}})
                self.assertEqual(selected.status_code, 200, selected.text)
                entry_id = selected.json()["entry"]["entry_id"]
                confirmed = client.post(base + "/review/plan", json={
                    "base_plan_version": selected.json()["plan_version"],
                    "action": "confirm", "entry_id": entry_id})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                # POST …/review/batches -> 202 {job_id,batch_id,status}
                batch_response = client.post(base + "/review/batches",
                                             json={"entry_ids": [entry_id]})
                self.assertEqual(batch_response.status_code, 202,
                                 batch_response.text)
                created_batch = batch_response.json()
                self.assertEqual(set(created_batch),
                                 {"job_id", "batch_id", "status"})
                self.assertEqual(created_batch["status"], "queued")
                # GET …/review/jobs/{id} -> §2.5 record the job panel renders
                job = client.get(base + "/review/jobs/"
                                 + created_batch["job_id"]).json()
                self.assertEqual(job["type"], "build")
                self.assertEqual(job["status"], "done")
                for key in ("job_id", "status", "progress", "result_ref",
                            "batch_id"):
                    self.assertIn(key, job)
                self.assertEqual(job["progress"]["total"], 1)
                # GET …/review/batches/{id} -> §2.3 record the panel renders
                batch = client.get(base + "/review/batches/"
                                   + created_batch["batch_id"]).json()
                self.assertEqual(batch["status"], "ready_to_publish")
                for key in ("batch_id", "status", "changes", "base", "estimate",
                            "build", "publish", "failures"):
                    self.assertIn(key, batch)
                for key in ("items", "assets", "pages", "docling_rerun",
                            "seconds", "reindex"):
                    self.assertIn(key, batch["estimate"])
                for key in ("build_id", "candidate_conversion_id", "gates",
                            "diff", "carried_corrections"):
                    self.assertIn(key, batch["build"])
                for key in ("all_pass", "checks", "errors"):
                    self.assertIn(key, batch["build"]["gates"])
                for key in ("assets_changed", "blocks_changed",
                            "chunks_changed", "new_warnings", "samples"):
                    self.assertIn(key, batch["build"]["diff"])
                # POST …/publish -> 200 {batch,current}
                published = client.post(
                    base + "/review/batches/" + created_batch["batch_id"]
                    + "/publish", json={})
                self.assertEqual(published.status_code, 200, published.text)
                publish_payload = published.json()
                self.assertEqual(set(publish_payload), {"batch", "current"})
                self.assertTrue(publish_payload["current"]["published"])
                self.assertEqual(publish_payload["batch"]["status"],
                                 "published")
                # POST …/rollback -> 202 {job_id,batch_id,status}
                rolled = client.post(base + "/review/rollback", json={})
                self.assertEqual(rolled.status_code, 202, rolled.text)
                rollback = rolled.json()
                self.assertEqual(set(rollback),
                                 {"job_id", "batch_id", "status"})
                rollback_job = client.get(base + "/review/jobs/"
                                          + rollback["job_id"]).json()
                self.assertEqual(rollback_job["type"], "rollback")
                self.assertEqual(rollback_job["status"], "done")
                # dataset info carries the banner data (empty here) + the
                # source extraction carries review_jobs for restart re-attach.
                listing = client.get("/api/datasets").json()["datasets"]
                info = next(item for item in listing
                            if item["id"] == dataset_id)
                self.assertIn("corrections_conflict", info)
                self.assertEqual(info["corrections_conflict"], [])
                rows = client.get(
                    "/api/datasets/%s/sources" % dataset_id).json()["sources"]
                extraction = rows[0]["extraction"]
                self.assertIn("review_jobs", extraction)
                self.assertIn(created_batch["job_id"],
                              extraction["review_jobs"])
                self.assertEqual(
                    extraction["review_jobs"][created_batch["job_id"]]["type"],
                    "build")


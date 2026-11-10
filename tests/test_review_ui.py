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
import json
import os
import shutil
import subprocess
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

    def test_candidate_panel_shows_content_diff_and_reindex(self):
        """CP3.1 F3: the panel separates content diff from the reindex scope."""
        text = _ui()
        for marker in (
                "实际变更", "内容 diff，不含 conversion/build",
                "索引影响", "function reviewReindexText",
                "function reviewReindexReasonLabel",
                "REVIEW_REINDEX_REASON_LABEL", "diff.reindex",
                "reindex.required", "reindex.chunks",
                "需要重建索引", "无需重建索引", "全量",
                "conversion 变化", "尚无当前构建",
                "未知（peelfeed 未返回 diff.reindex）",
                "索引需重建", "rr-reindex"):
            self.assertIn(marker, text)
        panel = text.index("function reviewCandidatePanelHtml")
        panel_body = text[panel:text.index("function reviewPublishedBatches")]
        for key in ("assets_changed", "blocks_changed", "chunks_changed"):
            self.assertIn(key, panel_body)
        # the content counts and the reindex line are rendered separately
        self.assertIn("reviewReindexText(diff.reindex)", panel_body)
        self.assertIn("assets_changed", panel_body)

    def test_rollback_button_opens_confirmation_dialog(self):
        """CP3.1 F4: the rollback control opens a dialog, not the request."""
        text = _ui()
        for marker in (
                'id="reviewRollbackConfirm"', 'role="alertdialog"',
                'aria-label="确认回滚最近一批"', "确认回滚最近一批",
                "撤销最近一批仍生效的修正并自动发布",
                "已有索引会变", "stale", "需要重建",
                "更早批次的修正保持不变", "回滚只作用于最近一批",
                "取消不会发送任何请求", "current 与正式账本保持不变",
                "确认回滚并发布", "取消",
                'id="reviewRollbackConfirmBtn"',
                'id="reviewRollbackCancelBtn"',
                'id="reviewRollbackConfirmClose"',
                "function reviewRollbackConfirmOpen",
                "function reviewRollbackConfirmClose",
                "function reviewRollbackConfirmed"):
            self.assertIn(marker, text)
        handler = text.index("$('#reviewBatch').addEventListener('click'")
        handler_body = text[handler:text.index(
            "$('#reviewConflict').addEventListener('click'")]
        self.assertIn("kind==='rollback'", handler_body)
        self.assertIn("reviewRollbackConfirmOpen()", handler_body)
        # the button itself must never send the rollback request
        self.assertNotIn("reviewRollback()", handler_body)

    def test_rollback_cancel_sends_no_request(self):
        """CP3.1 F4: cancel only hides the dialog; no request is issued."""
        text = _ui()
        close = text.index("function reviewRollbackConfirmClose")
        close_body = text[close:text.index("function reviewRollbackConfirmed")]
        for forbidden in ("fetch(", "reviewFetchJSON", "reviewRollback(",
                          "/rollback", "method:'POST'", "POST"):
            self.assertNotIn(forbidden, close_body)
        self.assertIn("hidden=true", close_body)
        # cancel is wired only to the close handler
        self.assertIn("$('#reviewRollbackCancelBtn').onclick="
                      "reviewRollbackConfirmClose", text)
        self.assertIn("$('#reviewRollbackConfirmClose').onclick="
                      "reviewRollbackConfirmClose", text)

    def test_rollback_confirm_sends_the_existing_rollback_request(self):
        """CP3.1 F4: only the explicit confirm button sends POST …/rollback."""
        text = _ui()
        confirmed = text.index("function reviewRollbackConfirmed")
        confirmed_body = text[confirmed:text.index("async function reviewRetryJob")]
        self.assertIn("reviewRollbackConfirmClose()", confirmed_body)
        self.assertIn("reviewRollback()", confirmed_body)
        self.assertIn("$('#reviewRollbackConfirmBtn').onclick="
                      "reviewRollbackConfirmed", text)
        rollback = text.index("async function reviewRollback()")
        rollback_body = text[rollback:text.index("function reviewRollbackConfirmOpen")]
        self.assertIn("reviewBase(ctx.dataset,ctx.sha)+'/rollback'", rollback_body)
        self.assertIn("method:'POST'", rollback_body)
        # an interrupted rollback retry is destructive too: it goes through the
        # same confirmation instead of firing the request immediately
        retry = text.index("async function reviewRetryJob")
        retry_body = text[retry:text.index("function reviewRunAction")]
        self.assertIn("reviewRollbackConfirmOpen()", retry_body)
        self.assertNotIn("await reviewRollback()", retry_body)

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
                       'id="reviewRollbackConfirm"', "构建本批候选",
                       "无需重建 (R0/no-op)",
                       "任务中断，可手动重试", "候选已过期，需重新构建",
                       "回滚最近一批", "确认回滚并发布",
                       "撤销最近一批仍生效的修正并自动发布",
                       "取消不会发送任何请求", "索引影响",
                       "需要重建索引", "无需重建索引", "修正冲突",
                       "peelfeed review schema 不一致",
                       "REVIEW_BATCH_STATUS_LABEL", "REVIEW_JOB_STATUS_LABEL",
                       "REVIEW_REINDEX_REASON_LABEL",
                       "reviewPublishBatch", "reviewRollback",
                       "reviewRollbackConfirmOpen", "reviewReindexText",
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



# ---------------------------------------------------------------------------
# CP3.1 F4: a Node stub-DOM behavior harness for the rollback confirmation.
# It runs the real review JS block from ``index.html`` and proves that
# clicking 回滚最近一批 only opens the dialog, cancel sends no request, and the
# explicit confirm button sends the existing POST …/review/rollback request.
# ``fetch`` is stubbed, so the harness never touches the network. Node is only
# needed for this behavior check; the static contract tests above always run.
# ---------------------------------------------------------------------------

NODE_ROLLBACK_HARNESS = r"""
/*
 * Drive the real review JS block with a stub DOM. No browser, no network: the
 * fetch stub records requests and rejects, so only the dialog wiring is tested.
 */
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("/* ---- review report dialog", 150000);
const end = html.indexOf("/* ---- workspace dialog", start);
if (start < 0 || end < 0) throw new Error("review block markers not found");
const block = html.slice(start, end);

const elements = new Map();
function element(key) {
  if (elements.has(key)) return elements.get(key);
  const listeners = {};
  const value = {
    id: key, innerHTML: "", textContent: "", hidden: true, disabled: false,
    value: "", files: [], checked: false, listeners, onclick: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    appendChild() {}, focus() {}, blur() {}, closest() { return null; },
    remove() {}, querySelector() { return element(key + ":child"); },
    querySelectorAll() { return []; }, getAttribute() { return null; },
    setAttribute() {}, style: {}, dataset: {}, scrollIntoView() {},
  };
  elements.set(key, value);
  return value;
}
const document = {
  querySelector(selector) { return element(selector); },
  querySelectorAll() { return []; },
  addEventListener() {}, getElementById(id) { return element("#" + id); },
  createElement() { return element("created"); },
  createTextNode() { return {}; },
  body: { style: {}, classList: { toggle() {} }, appendChild() {} },
  documentElement: { style: {}, classList: { toggle() {} } },
  head: { appendChild() {} },
};
const fetchCalls = [];
function fetchStub(url, opts) {
  fetchCalls.push({ url: String(url),
                    method: String((opts || {}).method || "GET") });
  return Promise.reject(new Error("network disabled in the harness"));
}
const window = {
  addEventListener() {}, removeEventListener() {}, fetch: fetchStub,
  matchMedia() { return { matches: false, addEventListener() {} }; },
  location: { hash: "", origin: "http://127.0.0.1:0", href: "" },
  history: { replaceState() {}, pushState() {} },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  sessionStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  innerWidth: 1400, innerHeight: 900,
};
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escAttr(value) { return esc(value); }
function callJSON() { throw new Error("network call in harness"); }
function getJSON() { return Promise.reject(new Error("network in harness")); }
function $(selector) { return element(selector); }
function toast() {}
function fmtBytes(value) { return String(value) + " B"; }
function fmtTime(value) { return String(value || ""); }

const sandbox = {
  document, window, DATASETS: [], DATASET_SOURCES: {}, REVIEW_COUNTS: {},
  REVIEW_CTX: null, $, esc, escAttr, callJSON, getJSON, toast, fmtBytes,
  fmtTime, console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Number, String, JSON, Math, Date, Error, RegExp, Array, Object,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  fetch: fetchStub, location: window.location, history: window.history,
  localStorage: window.localStorage, sessionStorage: window.sessionStorage,
  FormData: function () {}, Headers: function () {},
  AbortController: function () { this.signal = {}; this.abort = function () {}; },
  navigator: { clipboard: { writeText() { return Promise.resolve(); } } },
  requestAnimationFrame(fn) { return setTimeout(fn, 0); },
  matchMedia: window.matchMedia,
  alert() {}, confirm() { return true; }, prompt() { return ""; },
  innerWidth: window.innerWidth, innerHeight: window.innerHeight,
  addEventListener() {}, removeEventListener() {},
};
sandbox.globalThis = sandbox;

const code = block
  + "\n;globalThis.__review = {reviewRollbackConfirmOpen,"
  + " reviewRollbackConfirmClose, reviewRollbackConfirmed, reviewRollback};";
const context = vm.createContext(sandbox);
vm.runInContext(code, context, { filename: "index.html#review-block" });

sandbox.REVIEW_CTX = {
  dataset: "docs", sha: "a".repeat(64), data: null, itemsById: {},
  selected: null, busy: false, source: null, conflict: null, jobs: {},
  batchesById: {}, candidateBatchId: null, candidate: null,
  candidateStale: false, building: false, publishing: false, current: null,
  lastBuildEntryIds: [],
};
const batchId = "batch-20261108T120000-abcd";
sandbox.REVIEW_CTX.batchesById[batchId] = {
  batch_id: batchId, status: "published", created_at: "2026-11-08T12:00:05Z",
  build: {
    build_id: "b20261108T120000-12345678",
    diff: { assets_changed: 1, blocks_changed: 0, chunks_changed: 2,
            reindex: { required: true, reason: "conversion_changed",
                       chunks: 2829 } },
  },
  publish: { committed_at: "2026-11-08T12:00:05Z",
             current_build_id: "b20261108T120000-12345678" },
};

function tick() { return new Promise((resolve) => setTimeout(resolve, 20)); }
function rollbackButton() {
  return { getAttribute(name) { return name === "data-r" ? "rollback" : null; } };
}

async function main() {
  const results = [];
  const batchEl = elements.get("#reviewBatch");
  const clickHandlers = batchEl.listeners.click || [];
  if (!clickHandlers.length) throw new Error("no #reviewBatch click listener");
  // 1. clicking 回滚最近一批 only opens the confirmation dialog.
  clickHandlers.forEach((handler) => handler({
    target: { closest() { return rollbackButton(); } } }));
  await tick();
  const dialog = elements.get("#reviewRollbackConfirm");
  results.push({ step: "open", dialogVisible: dialog.hidden === false,
                 requests: fetchCalls.slice() });
  // 2. cancel closes the dialog and sends no request.
  elements.get("#reviewRollbackCancelBtn").onclick();
  await tick();
  results.push({ step: "cancel", dialogVisible: dialog.hidden === false,
                 requests: fetchCalls.slice() });
  // 3. confirm sends the existing POST …/review/rollback request.
  elements.get("#reviewRollbackConfirmBtn").onclick();
  await tick();
  results.push({ step: "confirm", dialogVisible: dialog.hidden === false,
                 requests: fetchCalls.slice() });
  console.log(JSON.stringify(results));
}

main().catch((error) => {
  console.error((error && error.stack) || error);
  process.exit(1);
});
"""


@unittest.skipUnless(shutil.which("node"),
                     "node is needed for the DOM behavior harness")
class ReviewUiRollbackConfirmBehaviorTests(unittest.TestCase):
    def test_rollback_opens_dialog_cancel_sends_nothing_confirm_sends_request(
            self):
        base = Path(tempfile.mkdtemp(prefix="peekseed-rollback-ui-"))
        try:
            harness = base / "rollback_confirm.js"
            harness.write_text(NODE_ROLLBACK_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [shutil.which("node"), str(harness), str(UI_PATH)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            results = json.loads(completed.stdout)
        finally:
            shutil.rmtree(str(base), ignore_errors=True)
        by_step = {row["step"]: row for row in results}
        opened = by_step["open"]
        self.assertTrue(opened["dialogVisible"], opened)
        self.assertEqual(opened["requests"], [], opened)
        cancelled = by_step["cancel"]
        self.assertFalse(cancelled["dialogVisible"], cancelled)
        self.assertEqual(cancelled["requests"], [], cancelled)
        confirmed = by_step["confirm"]
        self.assertFalse(confirmed["dialogVisible"], confirmed)
        self.assertTrue(any("/review/rollback" in row["url"]
                            and row["method"] == "POST"
                            for row in confirmed["requests"]),
                        confirmed["requests"])


# ---------------------------------------------------------------------------
# CP4 (M2.1): on-demand OCR in the Review UI.
#
# Static contract tests assert the served HTML/JS ships the OCR bar (status
# chip, [检查 OCR], read-only config dialog), the coverage summary, per-item
# OCR evidence (labels + confidence + low-confidence badge), the explicit
# per-selection refresh action and the auto-create-only-once behaviour.
# The Node stub-DOM harness then drives the real review JS with canned fetch
# responses (no browser, no network) to prove the behaviour end to end.
# ---------------------------------------------------------------------------


class ReviewUiCp4OcrTests(unittest.TestCase):
    def test_review_modal_ships_the_ocr_bar_and_config_dialog(self):
        text = _ui()
        for marker in (
                'id="reviewOcr"', 'id="reviewOcrConfig"',
                'id="reviewOcrConfigList"', 'id="reviewOcrConfigOk"',
                'id="reviewOcrConfigClose"', "OCR 配置说明",
                "检查 OCR", "OCR 已配置", "OCR 未配置（相关策略置灰）",
                "OCR 不可用", "只读", "不会从浏览器写入任何配置或密钥",
                "PEELFEED_OCR_PROVIDER", "PEELFEED_OCR_ENGINE",
                "PEELFEED_OCR_PARAMS", "PEELFEED_OCR_TIMEOUT",
                "PEELFEED_OCR_MIN_CONFIDENCE",
                "reviewOcrBarHtml", "paintReviewOcr", "reviewOcrStatusText",
                "reviewOcrCoverageText", "reviewOcrConfigHtml",
                "reviewOcrConfigOpen", "reviewOcrConfigClose",
                'data-r="ocr-check"', 'data-r="ocr-config"',
                "reviewBase(ctx.dataset,ctx.sha)+'/ocr/check'"):
            self.assertIn(marker, text)
        # the OCR bar sits above the batch section inside the review modal.
        dialog = text.index('id="reviewModal"')
        ocr = text.index('id="reviewOcr"')
        batch = text.index('id="reviewBatch"')
        clusters = text.index('id="reviewClusters"')
        self.assertLess(dialog, ocr)
        self.assertLess(ocr, batch)
        self.assertLess(batch, clusters)

    def test_ocr_coverage_and_summary_line(self):
        text = _ui()
        for marker in (
                "OCR 覆盖 ", "未覆盖 ", "低置信 ", "空 ",
                "with_evidence", "uncovered", "low_confidence",
                "evidence_files", "invalid_evidence",
                "ocr_min_confidence", "ocr_engine", "ocr_status",
                "min_confidence", "payload.ocr"):
            self.assertIn(marker, text)
        summary = text.index("function reviewSummaryHtml")
        summary_body = text[summary:text.index("function paintReviewSummary")]
        self.assertIn("ocr", summary_body)
        self.assertIn("OCR 覆盖", summary_body)

    def test_per_item_ocr_evidence_labels_low_confidence_and_empty(self):
        text = _ui()
        for marker in (
                "function reviewOcrEvidenceHtml", "evidence.ocr_evidence",
                "ocr_evidence", "OCR 证据", "label_count",
                "max_confidence", "min_confidence", "created_at",
                "OCR 低置信", "OCR 未识别到文字", "rr-ocr-labels",
                "rr-ocr-label-text", "rr-ocr-conf", "reviewAssetUrl(path)",
                "OCR 未识别到文字"):
            self.assertIn(marker, text)
        evidence = text.index("function reviewOcrEvidenceHtml")
        evidence_body = text[evidence:text.index("function reviewOcrUncoveredItemIds")]
        self.assertIn("threshold>0&&maxConf<threshold", evidence_body)
        self.assertIn("labels.map", evidence_body)
        self.assertIn("confidence.toFixed(2)", evidence_body)
        # rendered in the per-item card after the native evidence line
        item = text.index("function reviewItemHtml")
        item_body = text[item:text.index("function reviewSuggestionsHtml")]
        self.assertIn("reviewOcrEvidenceHtml(item)", item_body)

    def test_explicit_ocr_refresh_action_uses_the_selection(self):
        text = _ui()
        for marker in (
                "为选中项重新获取证据", 'data-r="ocr-refresh"',
                "function reviewOcrRefreshSelected", "reviewSelectedItemIds()",
                "function reviewStartOcrRefresh",
                "reviewBase(ctx.dataset,ctx.sha)+'/ocr/refresh'",
                "body:JSON.stringify({item_ids:ids})",
                "function reviewOcrRefreshRowHtml",
                "function reviewSyncOcrRefreshButton",
                "function reviewLoadOcrCounts",
                "function reviewOcrDoneText", "已刷新 ", "缓存命中 ",
                "失败 ", "低置信 ", "OCR 已刷新",
                "function reviewRetryJob", "type==='ocr'",
                "OCR 任务中断，可手动重试（不会自动重跑）"):
            self.assertIn(marker, text)
        # CP4.1 G1: neither OCR fetch body may carry a provider object. Assert
        # on the exact request snippets (the word "provider" appears elsewhere
        # in the UI, e.g. the read-only config dialog).
        refresh_at = text.index(
            "reviewBase(ctx.dataset,ctx.sha)+'/ocr/refresh'")
        refresh_snippet = text[refresh_at:refresh_at + 240]
        self.assertIn("body:JSON.stringify({item_ids:ids})", refresh_snippet)
        self.assertNotIn("provider", refresh_snippet)
        check_at = text.index("reviewBase(ctx.dataset,ctx.sha)+'/ocr/check'")
        check_snippet = text[check_at:check_at + 240]
        self.assertIn("body:JSON.stringify({})", check_snippet)
        self.assertNotIn("provider", check_snippet)
        self.assertNotIn("provider:{}", text)
        refresh = text.index("function reviewStartOcrRefresh")
        refresh_body = text[refresh:text.index("function reviewOcrRefreshSelected")]
        self.assertIn("reviewPollJob(jobId)", refresh_body)
        self.assertIn("await refreshReview()", refresh_body)
        self.assertIn("reviewJobError(record)", refresh_body)
        # the job panel includes type=ocr and the manual retry stays explicit
        jobs = text.index("function reviewJobsHtml")
        jobs_body = text[jobs:text.index("function reviewCandidatePanelHtml")]
        self.assertIn("type==='ocr'", jobs_body)
        retry = text.index("async function reviewRetryJob")
        retry_body = text[retry:text.index("function reviewRunAction")]
        self.assertIn("reviewStartOcrRefresh(ids,{auto:false})", retry_body)

    def test_ocr_auto_create_only_once_never_auto_retries(self):
        text = _ui()
        for marker in (
                "REVIEW_OCR_AUTO_TRIED", "function reviewMaybeAutoOcr",
                "function reviewOcrUncoveredItemIds",
                "reviewStartOcrRefresh(ids,{auto:true})",
                "reviewOcrUsable(caps)", "payload.ocr.uncovered",
                "extraction.review_jobs", "==='ocr'",
                "首次打开该来源", "仅一次；中断/失败后只能手动重试",
                "await reviewMaybeAutoOcr()"):
            self.assertIn(marker, text)
        auto = text.index("async function reviewMaybeAutoOcr")
        auto_body = text[auto:text.index("function reviewKey(dsId,sha)")]
        for guard in ("reviewOcrUsable(caps)", "uncovered", "review_jobs",
                      "REVIEW_OCR_AUTO_TRIED[key]", "auto:true"):
            self.assertIn(guard, auto_body)
        # polling never creates another OCR job automatically
        poll = text.index("async function reviewPollJob(jobId)")
        poll_body = text[poll:text.index("async function reviewRefreshDatasets")]
        for forbidden in ("reviewStartOcrRefresh(", "reviewOcrRefreshSelected(",
                          "reviewMaybeAutoOcr(", "setInterval"):
            self.assertNotIn(forbidden, poll_body)
        # only the explicit manual retry / refresh action creates a new job
        self.assertIn("auto:false", text)
        self.assertIn("reviewOcrRefreshSelected();", text)

    def test_ocr_gray_out_reasons_are_chinese(self):
        text = _ui()
        for marker in (
                "REVIEW_OCR_REASON_LABEL",
                "ocr_unavailable:'OCR 未配置或不可用'",
                "ocr_evidence_missing:'需要先运行局部 OCR'",
                "ocr_labels_empty:'OCR 结果为空'",
                "not_ready:'尚无当前构建'",
                "function reviewStrategyUnavailableText",
                "OCR 未配置（相关策略置灰）", "需要先运行局部 OCR",
                "OCR 结果为空", "不可用：",
                "reviewStrategyUnavailableText(s,item)",
                "reviewStrategyUnavailableText(s)"):
            self.assertIn(marker, text)
        reason = text.index("function reviewStrategyUnavailableText")
        reason_body = text[reason:text.index("function reviewOcrConfigHtml")]
        self.assertIn("use_ocr_labels", reason_body)
        self.assertIn("caps.ocr", reason_body)
        self.assertIn("ocr_evidence", reason_body)

    def test_served_page_ships_cp4_ocr_controls(self):
        with _served_home():
            from fastapi.testclient import TestClient
            from peekseed import app as app_module

            response = TestClient(app_module.create_app()).get("/")
            self.assertEqual(response.status_code, 200, response.text)
            page = response.text
        for marker in ('id="reviewOcr"', 'id="reviewOcrConfig"',
                       "检查 OCR", "OCR 配置说明", "为选中项重新获取证据",
                       "OCR 覆盖 ", "OCR 低置信", "OCR 未识别到文字",
                       "OCR 已配置", "OCR 未配置（相关策略置灰）",
                       "OCR 不可用", "PEELFEED_OCR_PROVIDER",
                       "PEELFEED_OCR_MIN_CONFIDENCE",
                       "REVIEW_OCR_REASON_LABEL", "REVIEW_OCR_AUTO_TRIED",
                       "reviewOcrEvidenceHtml", "reviewMaybeAutoOcr",
                       "reviewStartOcrRefresh", "reviewOcrCheck",
                       "reviewOcrConfigOpen", "reviewOcrRefreshSelected",
                       "ocr/refresh", "ocr/check", "只读"):
            self.assertIn(marker, page)
        # CP4.1 G1: the served UI never sends a provider override in either
        # OCR request body (the config dialog still names PEELFEED_OCR_PROVIDER).
        self.assertNotIn("provider:{}", page)
        self.assertIn("body:JSON.stringify({item_ids:ids})", page)
        check_at = page.index("reviewBase(ctx.dataset,ctx.sha)+'/ocr/check'")
        self.assertNotIn("provider",
                         page[check_at:check_at + 240])


# ---------------------------------------------------------------------------
# CP4 Node stub-DOM behaviour harness: runs the real review JS block against
# canned fetch responses (no browser, no network) and proves:
#   * the first review open auto-creates ONE OCR refresh job for the uncovered
#     no_native_labels items and polls it to done, refetching coverage;
#   * reopening does not auto-create again (descriptor/trie guard);
#   * an interrupted OCR job shows the manual-retry message and only the
#     explicit retry creates a new job;
#   * capabilities ocr=false grays use_ocr_labels with a Chinese reason, sends
#     no auto job, and [检查 OCR] maps the 503 to a readable message;
#   * OCR evidence labels/confidence and the low-confidence badge render.
# ---------------------------------------------------------------------------

NODE_OCR_HARNESS = r"""
/*
 * Drive the real review JS block with a stub DOM + canned fetch. No browser, no
 * network: every request is recorded and answered from the scenario tables.
 */
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("/* ---- review report dialog", 150000);
const end = html.indexOf("/* ---- workspace dialog", start);
if (start < 0 || end < 0) throw new Error("review block markers not found");
const block = html.slice(start, end);

const elements = new Map();
function element(key) {
  if (elements.has(key)) return elements.get(key);
  const listeners = {};
  const value = {
    id: key, innerHTML: "", textContent: "", hidden: true, disabled: false,
    value: "", files: [], checked: false, listeners, onclick: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    appendChild() {}, focus() {}, blur() {}, closest() { return null; },
    remove() {}, querySelector() { return element(key + ":child"); },
    querySelectorAll() { return []; }, getAttribute() { return null; },
    setAttribute() {}, style: {}, dataset: {}, scrollIntoView() {},
  };
  elements.set(key, value);
  return value;
}
const document = {
  querySelector(selector) { return element(selector); },
  querySelectorAll() { return []; },
  addEventListener() {}, getElementById(id) { return element("#" + id); },
  createElement() { return element("created"); },
  createTextNode() { return {}; },
  body: { style: {}, classList: { toggle() {} }, appendChild() {} },
  documentElement: { style: {}, classList: { toggle() {} } },
  head: { appendChild() {} },
};

const calls = [];
let mode = "auto";
const sha = "a".repeat(64);
const sha2 = "b".repeat(64);
const sha3 = "c".repeat(64);

const STRATS_AVAILABLE = [
  { id: "use_first_row_header", version: 1, name: "本表首行作表头",
    available: true, unavailable_reason: null, recommended: true,
    capabilities: ["native_lines"], params_schema: {},
    action: { type: "operation", name: "set_header_rows" } },
  { id: "use_ocr_labels", version: 1, name: "OCR 标签",
    available: true, unavailable_reason: null, recommended: true,
    capabilities: ["ocr"], params_schema: {},
    action: { type: "operation", name: "set_asset_labels_from_ocr" } },
];
const STRATS_UNCOVERED = [
  STRATS_AVAILABLE[0],
  { id: "use_ocr_labels", version: 1, name: "OCR 标签",
    available: false, unavailable_reason: "需要先运行局部 OCR",
    recommended: false, capabilities: ["ocr"], params_schema: {},
    action: { type: "operation", name: "set_asset_labels_from_ocr" } },
];
const STRATS_NO_OCR = [
  STRATS_AVAILABLE[0],
  { id: "use_ocr_labels", version: 1, name: "OCR 标签",
    available: false, unavailable_reason: "缺少能力：局部 OCR（M2.1）",
    recommended: false, capabilities: ["ocr"], params_schema: {},
    action: { type: "operation", name: "set_asset_labels_from_ocr" } },
  { id: "keep_original_figure", version: 1, name: "保留原始图",
    available: true, unavailable_reason: null, recommended: true,
    capabilities: [], params_schema: {},
    action: { type: "resolution", name: "keep_original_figure" } },
];

function caps(configured, available) {
  return {
    review_schema: 2, native_lines: true,
    ocr: configured, ocr_configured: configured,
    ocr_detail: configured ? null : "OCR 未配置：设置 PEELFEED_OCR_PROVIDER/ENGINE",
    ocr_engine: configured ? "rapidocr@1.4.4" : null,
    ocr_min_confidence: configured ? 0.6 : null,
    ocr_status: configured
      ? { available: available, checked_at: "2026-11-09T03:10:00Z", error: null }
      : { available: false, checked_at: null, error: null },
    vlm: false,
  };
}
function coverage(withEvidence, uncovered) {
  return {
    items: 1, with_evidence: withEvidence, empty: 0, low_confidence: 0,
    uncovered: uncovered, invalid_evidence: 0,
    evidence_files: withEvidence, engine: "rapidocr@1.4.4",
    min_confidence: 0.6,
  };
}
const OCR_LABELS = [
  { text: "Compute Express Link", bbox: [4, 6, 210, 30], confidence: 0.99 },
  { text: "CXL 3.1", bbox: [4, 40, 96, 64], confidence: 0.96 },
];
function ocrEntry(labels) {
  const confidences = labels.map(function (label) { return label.confidence; });
  return {
    path: "extract/ocr/figure-1-1/ab12cd34ef567890-rapidocr@1.4.4.json",
    crop_ref: "images/fig_1_1.png", image_sha256: "ab".repeat(32),
    engine: "rapidocr@1.4.4", params_sha256: "0123456789abcdef",
    evidence_sha256: "cd".repeat(32), label_count: labels.length,
    max_confidence: confidences.length ? Math.max(...confidences) : null,
    min_confidence: confidences.length ? Math.min(...confidences) : null,
    created_at: "2026-11-09T03:10:00Z", labels: labels,
  };
}
function figureItem(evidence, strategies) {
  return {
    id: "it-fig", code: "no_native_labels", kind: "figure",
    severity: "warning", blocking: false, asset_id: "figure-1-1",
    pages: [2], cluster_id: "cl-fig", message: "no_native_labels: figure-1-1",
    triage: "needs_ocr",
    status: evidence && evidence.length ? "needs_decision" : "pending_evidence",
    evidence: { hash: "h", crops: ["images/fig_1_1.png"],
                native_line_ids: [], ocr_evidence: evidence || [] },
    current: {}, candidates: [], strategies: strategies,
    entry: null, resolution: null, stale: false,
  };
}
function cluster(item, strategies) {
  return { cluster_id: "cl-fig", cluster_key: {}, code: "no_native_labels",
    title: "图内没有 native 文本", kind: "figure", item_count: 1,
    human_pending: 0, representative_item_id: item.id,
    states: { pending_evidence: 1 }, conclusion: null, suggestions: [],
    strategies: strategies, item_ids: [item.id] };
}
function reviewPayload(capabilities, cov, item) {
  return {
    peel_id: "peel", review_schema: 2,
    generated_at: "2026-11-09T03:10:00Z", profile: "generic",
    capabilities: capabilities,
    counts: { raw_items: 1, items: 1, clusters: 1, human_pending: 1,
              human_pending_clusters: 1, pending_evidence: 1,
              auto_resolved: 0, needs_decision: 0, draft: 0, confirmed: 0,
              applied: 0, deferred: 0, failed_check: 0, stale: 0,
              unsupported_items: 0 },
    triage_counts: { deterministic_candidate: 0, needs_decision: 0,
                     needs_ocr: 1, informational: 0, unsupported: 0 },
    plan: { plan_version: 0, entries: 0, draft: 0, confirmed: 0, locked: 0,
            stale: 0 },
    report: null, ocr: cov, clusters: [cluster(item, item.strategies)],
    items: [item],
  };
}

const UNCOVERED = reviewPayload(caps(true, true), coverage(0, 1),
  figureItem([], STRATS_UNCOVERED));
const COVERED = reviewPayload(caps(true, true), coverage(1, 0),
  figureItem([ocrEntry(OCR_LABELS)], STRATS_AVAILABLE));
const NO_OCR = reviewPayload(caps(false, false), coverage(0, 1),
  figureItem([], STRATS_NO_OCR));
const COVERED_2 = reviewPayload(caps(true, true), coverage(1, 0),
  figureItem([ocrEntry(OCR_LABELS)], STRATS_AVAILABLE));
const COVERED_3 = reviewPayload(caps(true, true), coverage(1, 0),
  figureItem([ocrEntry(OCR_LABELS)], STRATS_AVAILABLE));
const LOW_EVIDENCE = ocrEntry([
  { text: "模糊的图注文字", bbox: [4, 6, 120, 26], confidence: 0.55 },
]);

let reviewPayloadCurrent = UNCOVERED;
let sourcesPayload = { sources: [] };
let checkStatus = 200;
let checkPayload = { available: true, engine: "rapidocr@1.4.4",
  provider: {}, probe: { image_sha256: "ab".repeat(32), labels: 0,
  seconds: 0.85 }, checked_at: "2026-11-09T03:10:00Z", error: null };

function sourceRow(shaValue, jobs) {
  return { name: "manual.pdf", sha256: shaValue, status: "extracted",
    extraction: { state: "extracted", peel_id: "peel",
      conversion_id: "c", review_jobs: jobs || {} } };
}
function response(status, payload) {
  return { ok: status >= 200 && status < 300, status: status,
           json: async function () { return payload; },
           text: async function () { return JSON.stringify(payload); } };
}
function jobRecord(status, id, resultRef) {
  return { job_id: id, type: "ocr", status: status,
    progress: { stage: "ocr", done: 1, total: 1 },
    result_ref: resultRef || null,
    error: status === "failed"
      ? { code: "ocr_unavailable", message: "OCR provider unavailable" }
      : (status === "interrupted"
        ? { code: "interrupted", message: "the job process is no longer running" }
        : null) };
}
function fetchStub(url, opts) {
  const method = String((opts || {}).method || "GET").toUpperCase();
  const body = String((opts || {}).body || "");
  calls.push({ url: String(url), method: method, body: body });
  const u = String(url);
  if (method === "POST" && u.indexOf("/review/ocr/check") >= 0) {
    return Promise.resolve(response(checkStatus, checkStatus === 200
      ? checkPayload
      : { detail: "peelfeed ocr check failed: error [ocr_unavailable] "
          + "PEELFEED_OCR_PROVIDER is not set" }));
  }
  if (method === "POST" && u.indexOf("/review/ocr/refresh") >= 0) {
    const parsed = JSON.parse(body || "{}");
    if (mode === "auto") {
      reviewPayloadCurrent = COVERED;
      sourcesPayload = { sources: [sourceRow(sha,
        { "ocr-job-1": { type: "ocr", created_at: "2026-11-09T03:10:00Z",
                         batch_id: null } })] };
      return Promise.resolve(response(202, {
        job_id: "ocr-job-1", status: "queued", result_ref: null }));
    }
    if (mode === "interrupted") {
      reviewPayloadCurrent = COVERED_2;
      sourcesPayload = { sources: [sourceRow(sha2,
        { "ocr-int-1": { type: "ocr", created_at: "2026-11-09T03:10:00Z",
                         batch_id: null },
          "ocr-retry-2": { type: "ocr", created_at: "2026-11-09T03:11:00Z",
                           batch_id: null } })] };
      return Promise.resolve(response(202, {
        job_id: "ocr-retry-2", status: "queued", result_ref: null }));
    }
    if (mode === "noocr") {
      return Promise.resolve(response(503, {
        detail: "peelfeed ocr refresh failed: error [ocr_unavailable] "
          + "PEELFEED_OCR_PROVIDER is not set" }));
    }
    return Promise.resolve(response(202, {
      job_id: "ocr-other", status: "queued", result_ref: null }));
  }
  /* Route on the most specific path first: every review/job/asset URL also
     contains "/sources/<sha>", so the sources route must come last. Asset URLs
     contain "/review/jobs/" too, so they are matched before the job route. */
  if (u.indexOf("/assets/") >= 0) {
    return Promise.resolve(response(200, { counts: {
      requested: 1, refreshed: 1, cache_hits: 0, failed: 0, empty: 0,
      low_confidence: 0 } }));
  }
  if (u.indexOf("/review/jobs/") >= 0) {
    if (u.indexOf("ocr-int-1") >= 0) {
      return Promise.resolve(response(200,
        jobRecord("interrupted", "ocr-int-1")));
    }
    if (u.indexOf("ocr-retry-2") >= 0) {
      return Promise.resolve(response(200,
        jobRecord("done", "ocr-retry-2",
                  "jobs/ocr-retry-2.result.json")));
    }
    if (u.indexOf("ocr-job-1") >= 0) {
      return Promise.resolve(response(200,
        jobRecord("done", "ocr-job-1", "jobs/ocr-job-1.result.json")));
    }
    return Promise.resolve(response(404, { detail: "unknown job" }));
  }
  if (u.indexOf("/review?") >= 0 || u.endsWith("/review")) {
    return Promise.resolve(response(200, reviewPayloadCurrent));
  }
  if (u.indexOf("/sources") >= 0) {
    return Promise.resolve(response(200, sourcesPayload));
  }
  if (u.indexOf("/api/jobs/") >= 0) {
    return Promise.resolve(response(200, { state: "succeeded" }));
  }
  return Promise.reject(new Error("unexpected fetch " + method + " " + u));
}

const PF_STATUS = {
  handshake: { available: true, version: "0.1.0", review_schema: 2,
    ocr: { configured: true, provider: "ocr_provider_rapidocr.py",
           engine: "rapidocr@1.4.4", timeout_seconds: 30.0,
           min_confidence: 0.6, params: { lang: "en" } } },
  config: { configured: true },
};

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escAttr(value) { return esc(value); }
function toast() {}
function fmtBytes(value) { return String(value) + " B"; }
function fmtTime(value) { return String(value || ""); }
function $(selector) { return element(selector); }
function getJSON(path) {
  return fetchStub(path).then(function (r) {
    if (!r.ok) throw new Error(path + " returned HTTP " + r.status);
    return r.json();
  });
}
function callJSON() { throw new Error("callJSON not used in the harness"); }
async function loadDatasets() { return []; }

const sandbox = {
  document, window: {}, DATASETS: [], DATASET_SOURCES: {},
  REVIEW_COUNTS: {}, REVIEW_CTX: null, PF_STATUS: PF_STATUS,
  $, esc, escAttr, callJSON, getJSON, toast, fmtBytes, fmtTime,
  loadDatasets, console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Number, String, JSON, Math, Date, Error, RegExp, Array, Object,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  fetch: fetchStub,
  location: { hash: "", origin: "http://127.0.0.1:0", href: "" },
  history: { replaceState() {}, pushState() {} },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  sessionStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  navigator: { clipboard: { writeText() { return Promise.resolve(); } } },
  requestAnimationFrame(fn) { return setTimeout(fn, 0); },
  matchMedia() { return { matches: false, addEventListener() {} }; },
  alert() {}, confirm() { return true; }, prompt() { return ""; },
  innerWidth: 1400, innerHeight: 900,
  addEventListener() {}, removeEventListener() {},
};
sandbox.globalThis = sandbox;

const code = block
  + "\n;globalThis.__review = {"
  + " openReview: openReview, reviewOcrCheck: reviewOcrCheck,"
  + " reviewOcrConfigOpen: reviewOcrConfigOpen,"
  + " reviewOcrConfigClose: reviewOcrConfigClose,"
  + " reviewOcrEvidenceHtml: reviewOcrEvidenceHtml,"
  + " reviewRetryJob: reviewRetryJob,"
  + " reviewOcrUncoveredItemIds: reviewOcrUncoveredItemIds,"
  + " getContext: function(){ return REVIEW_CTX; },"
  + " setData: function(data){ REVIEW_CTX={dataset:'docs',sha:'" + sha
  + "',data:data}; },"
  + " element: function(key){ return element(key); },"
  + " autoTried: function(){ return REVIEW_OCR_AUTO_TRIED; } };";
const context = vm.createContext(sandbox);
vm.runInContext(code, context, { filename: "index.html#review-block" });

function tick() { return new Promise((resolve) => setTimeout(resolve, 30)); }
function postRefreshCalls() {
  return calls.filter(function (call) {
    return call.method === "POST" && call.url.indexOf("/review/ocr/refresh") >= 0;
  });
}
function postCheckCalls() {
  return calls.filter(function (call) {
    return call.method === "POST" && call.url.indexOf("/review/ocr/check") >= 0;
  });
}
function htmlOf(selector) { return element(selector).innerHTML; }

async function runAutoFlow() {
  mode = "auto";
  reviewPayloadCurrent = UNCOVERED;
  sourcesPayload = { sources: [sourceRow(sha)] };
  calls.length = 0;
  const steps = [];
  await sandbox.__review.openReview("docs", sha);
  await tick();
  steps.push({ step: "auto-open", refreshPosts: postRefreshCalls().length,
               refreshBody: postRefreshCalls().length
                 ? postRefreshCalls()[0].body : "",
               bar: htmlOf("#reviewOcr"),
               msg: htmlOf("#reviewMsg"),
               batch: htmlOf("#reviewBatch") });
  const before = calls.length;
  await sandbox.__review.openReview("docs", sha);
  await tick();
  steps.push({ step: "reopen",
               newRefreshPosts: calls.slice(before).filter(function (call) {
                 return call.method === "POST"
                   && call.url.indexOf("/review/ocr/refresh") >= 0;
               }).length,
               callsSinceReopen: calls.slice(before).length,
               autoTried: Object.keys(sandbox.__review.autoTried()) });
  await sandbox.__review.reviewOcrCheck();
  await tick();
  steps.push({ step: "check", checks: postCheckCalls().length,
               checkBody: postCheckCalls().length
                 ? postCheckCalls()[0].body : "",
               bar: htmlOf("#reviewOcr"), msg: htmlOf("#reviewMsg") });
  sandbox.__review.reviewOcrConfigOpen();
  await tick();
  steps.push({ step: "config", hidden: element("#reviewOcrConfig").hidden,
               list: htmlOf("#reviewOcrConfigList") });
  sandbox.__review.reviewOcrConfigClose();
  await tick();
  steps.push({ step: "config-close",
               hidden: element("#reviewOcrConfig").hidden });
  return steps;
}

async function runInterruptedFlow() {
  mode = "interrupted";
  reviewPayloadCurrent = reviewPayload(caps(true, true), coverage(0, 1),
    figureItem([], STRATS_UNCOVERED));
  sourcesPayload = { sources: [sourceRow(sha2,
    { "ocr-int-1": { type: "ocr", created_at: "2026-11-09T03:10:00Z",
                     batch_id: null } })] };
  calls.length = 0;
  const steps = [];
  await sandbox.__review.openReview("docs2", sha2);
  await tick();
  steps.push({ step: "interrupted-open", refreshPosts: postRefreshCalls().length,
               batch: htmlOf("#reviewBatch") });
  const btn = { getAttribute(name) {
    if (name === "data-type") return "ocr";
    if (name === "data-job") return "ocr-int-1";
    return null;
  } };
  await sandbox.__review.reviewRetryJob(btn);
  await tick();
  steps.push({ step: "manual-retry", refreshPosts: postRefreshCalls().length,
               refreshBody: postRefreshCalls().length
                 ? postRefreshCalls()[0].body : "",
               msg: htmlOf("#reviewMsg") });
  return steps;
}

async function runNoOcrFlow() {
  mode = "noocr";
  reviewPayloadCurrent = NO_OCR;
  sourcesPayload = { sources: [sourceRow(sha3)] };
  calls.length = 0;
  const steps = [];
  await sandbox.__review.openReview("docs3", sha3);
  await tick();
  steps.push({ step: "noocr-open", refreshPosts: postRefreshCalls().length,
               bar: htmlOf("#reviewOcr"),
               detail: htmlOf("#reviewDetail"),
               msg: htmlOf("#reviewMsg") });
  checkStatus = 503;
  await sandbox.__review.reviewOcrCheck();
  await tick();
  steps.push({ step: "noocr-check", checks: postCheckCalls().length,
               bar: htmlOf("#reviewOcr"), msg: htmlOf("#reviewMsg") });
  checkStatus = 200;
  return steps;
}

async function runEvidenceFlow() {
  mode = "evidence";
  reviewPayloadCurrent = COVERED;
  sourcesPayload = { sources: [sourceRow(sha)] };
  sandbox.__review.setData(COVERED);
  const lowItem = figureItem([LOW_EVIDENCE], STRATS_AVAILABLE);
  const emptyItem = figureItem([ocrEntry([])], STRATS_AVAILABLE);
  return [{ step: "evidence",
            low: sandbox.__review.reviewOcrEvidenceHtml(lowItem),
            empty: sandbox.__review.reviewOcrEvidenceHtml(emptyItem),
            labels: sandbox.__review.reviewOcrEvidenceHtml(
              figureItem([ocrEntry(OCR_LABELS)], STRATS_AVAILABLE)),
            uncoveredIds: sandbox.__review.reviewOcrUncoveredItemIds(
              UNCOVERED) }];
}

async function main() {
  const results = [];
  results.push(...await runAutoFlow());
  results.push(...await runInterruptedFlow());
  results.push(...await runNoOcrFlow());
  results.push(...await runEvidenceFlow());
  console.log(JSON.stringify(results));
}

main().catch(function (error) {
  console.error((error && error.stack) || error);
  process.exit(1);
});
"""


@unittest.skipUnless(shutil.which("node"),
                     "node is needed for the OCR DOM behavior harness")
class ReviewUiOcrBehaviorTests(unittest.TestCase):
    def _run_harness(self):
        base = Path(tempfile.mkdtemp(prefix="peekseed-ocr-ui-"))
        try:
            harness = base / "ocr_behavior.js"
            harness.write_text(NODE_OCR_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [shutil.which("node"), str(harness), str(UI_PATH)],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            return {row["step"]: row for row in json.loads(completed.stdout)}
        finally:
            shutil.rmtree(str(base), ignore_errors=True)

    def test_first_open_auto_creates_one_ocr_job_and_polls_to_done(self):
        by_step = self._run_harness()
        opened = by_step["auto-open"]
        self.assertEqual(opened["refreshPosts"], 1, opened)
        body = json.loads(opened["refreshBody"])
        self.assertEqual(body["item_ids"], ["it-fig"], body)
        # CP4.1 G1: the UI never sends a provider object.
        self.assertNotIn("provider", body)
        self.assertIn("OCR 覆盖 1/1", opened["bar"], opened)
        self.assertIn("OCR 已配置", opened["bar"], opened)
        self.assertIn("自动创建 OCR 任务", opened["msg"], opened)
        self.assertIn("OCR 已刷新", opened["msg"], opened)
        self.assertIn("已刷新 1", opened["msg"], opened)
        self.assertIn("OCR", opened["batch"], opened)
        self.assertIn("已完成", opened["batch"], opened)
        # reopening the same source never auto-creates a second job.
        reopen = by_step["reopen"]
        self.assertEqual(reopen["newRefreshPosts"], 0, reopen)
        self.assertIn("docs|" + "a" * 64, reopen["autoTried"], reopen)
        # [检查 OCR] posts an empty body ({}), never a provider object.
        checked = by_step["check"]
        self.assertEqual(checked["checks"], 1, checked)
        self.assertEqual(json.loads(checked["checkBody"]), {}, checked)
        self.assertIn("检查结果：可用", checked["bar"], checked)
        self.assertIn("探测 0.85s", checked["bar"], checked)
        # read-only config dialog lists the effective env-only config.
        config = by_step["config"]
        self.assertFalse(config["hidden"], config)
        for marker in ("ocr_provider_rapidocr.py", "rapidocr@1.4.4",
                       "timeout_seconds", "30", "min_confidence", "0.6",
                       "lang=en", "可用"):
            self.assertIn(marker, config["list"], config)
        self.assertTrue(by_step["config-close"]["hidden"])

    def test_interrupted_ocr_job_only_retries_on_explicit_action(self):
        by_step = self._run_harness()
        opened = by_step["interrupted-open"]
        # An existing type=ocr descriptor disables auto-create entirely.
        self.assertEqual(opened["refreshPosts"], 0, opened)
        self.assertIn("任务中断，可手动重试", opened["batch"], opened)
        self.assertIn("不会自动重跑", opened["batch"], opened)
        retried = by_step["manual-retry"]
        self.assertEqual(retried["refreshPosts"], 1, retried)
        body = json.loads(retried["refreshBody"])
        self.assertEqual(body["item_ids"], ["it-fig"], body)
        self.assertNotIn("provider", body)

    def test_no_ocr_fallback_grays_out_and_check_maps_503(self):
        by_step = self._run_harness()
        opened = by_step["noocr-open"]
        self.assertEqual(opened["refreshPosts"], 0, opened)
        self.assertIn("OCR 未配置（相关策略置灰）", opened["bar"], opened)
        self.assertIn("缺少能力：局部 OCR（M2.1）", opened["detail"], opened)
        self.assertIn("保留原始图", opened["detail"], opened)
        checked = by_step["noocr-check"]
        self.assertEqual(checked["checks"], 1, checked)
        self.assertIn("OCR 不可用", checked["bar"], checked)
        self.assertIn("OCR 检查失败", checked["msg"], checked)
        self.assertIn("peelfeed 不可用", checked["msg"], checked)

    def test_ocr_evidence_labels_confidence_and_low_confidence_badge(self):
        by_step = self._run_harness()
        evidence = by_step["evidence"]
        self.assertIn("OCR 证据", evidence["labels"], evidence)
        self.assertIn("Compute Express Link", evidence["labels"], evidence)
        self.assertIn("0.99", evidence["labels"], evidence)
        self.assertIn("引擎 rapidocr@1.4.4", evidence["labels"], evidence)
        self.assertIn("证据文件", evidence["labels"], evidence)
        self.assertIn("OCR 低置信", evidence["low"], evidence)
        self.assertIn("模糊的图注文字", evidence["low"], evidence)
        self.assertIn("0.55", evidence["low"], evidence)
        self.assertIn("OCR 未识别到文字", evidence["empty"], evidence)
        self.assertEqual(evidence["uncoveredIds"], ["it-fig"], evidence)


# ---------------------------------------------------------------------------
# CP5 (M2.2) served-HTML assertions: the VLM bar, the explicit privacy
# confirmation dialog, the VLM job panel, the report provider summary, the
# clarifications and the discussion/patch panel all ship with the frozen stable
# ids and Chinese labels (wire-contract §7).
# ---------------------------------------------------------------------------

class ReviewUiCp5Tests(unittest.TestCase):
    def test_review_modal_ships_the_cp5_vlm_containers(self):
        text = _ui()
        for marker in (
                'id="reviewVlmBar"', 'id="reviewVlmJob"',
                'id="reviewReportProvider"', 'id="reviewClarifications"',
                'id="reviewDiscuss"', 'id="reviewVlmConfirm"',
                'id="reviewVlmConfirmGo"', 'id="reviewVlmConfirmCancel"',
                'id="reviewVlmConfirmCancelBtn"',
                'id="reviewVlmScopeCrop"', 'id="reviewVlmScopeOriginalPage"',
                'id="reviewVlmScopeAdjacentPages"', 'id="reviewVlmScopeNative"',
                'id="reviewVlmScopeOcr"', 'id="reviewVlmTextOnly"',
                'id="reviewDiscussScope"', 'id="reviewDiscussInput"',
                'id="reviewDiscussSend"', 'id="reviewDiscussTurns"',
                'id="reviewDiscussPatch"', 'id="reviewDiscussAdoptPatch"',
                'id="reviewDiscussMembers"', 'id="reviewPatchDiff"',
                'id="reviewPatchConflict"'):
            self.assertIn(marker, text)
        # The generated bar buttons carry the frozen stable ids.
        self.assertIn('id="reviewVlmGenerate"', text)
        self.assertIn('id="reviewVlmDiscuss"', text)
        # Dialog lives inside the review modal, before the workspace modal.
        modal = text.index('id="reviewModal"')
        confirm = text.index('id="reviewVlmConfirm"')
        workspace = text.index('id="workspaceModal"')
        self.assertLess(modal, confirm)
        self.assertLess(confirm, workspace)

    def test_vlm_actions_are_gated_and_never_implicit(self):
        text = _ui()
        for marker in (
                "function reviewVlmBarHtml", "function paintReviewVlmBar",
                "function reviewVlmGenerateOpen", "function reviewVlmConfirmOpen",
                "function reviewVlmConfirmClose", "function reviewVlmConfirmed",
                "function reviewVlmStartReport", "function reviewVlmStartDiscuss",
                "function reviewVlmPollJob", "function reviewVlmRetry",
                "function reviewVlmResultSummary", "function reviewVlmJobs",
                "function paintReviewVlmJob", "function paintReviewReportProvider",
                "function paintReviewClarifications", "function reviewDiscussOpen",
                "function reviewDiscussSendOpen", "function reviewDiscussAdoptPatch",
                "function reviewClarificationAnswer", "function paintReviewPatchDiff",
                "function paintReviewPatchConflict", "function reviewVlmProviderLabel",
                "REVIEW_VLM_INTENT_LABEL", "REVIEW_VLM_JOB_TYPES"):
            self.assertIn(marker, text)
        # Painting the bar/provider/clarifications never issues a request.
        bar_start = text.index("function reviewVlmBarHtml")
        bar_end = text.index("function reviewReportProviderInfo")
        bar_body = text[bar_start:bar_end]
        for forbidden in ("reviewFetchJSON", "/reports", "/discussions",
                          "fetch("):
            self.assertNotIn(forbidden, bar_body)
        provider_start = text.index("function reviewReportProviderHtml")
        provider_end = text.index("function paintReviewClarifications")
        self.assertNotIn("reviewFetchJSON",
                         text[provider_start:provider_end])
        # Explicit confirmation gate: the only confirmed:true is built in
        # reviewVlmConfirmed, and both start functions are called from there.
        confirmed_start = text.index("function reviewVlmConfirmed")
        confirmed_end = text.index("async function reviewVlmStartReport")
        confirmed_body = text[confirmed_start:confirmed_end]
        self.assertIn("confirmed:true", confirmed_body)
        self.assertIn("reviewVlmStartReport(payload,confirmation)",
                      confirmed_body)
        self.assertIn("reviewVlmStartDiscuss(payload,confirmation)",
                      confirmed_body)
        # Every provider call uses the frozen routes.
        self.assertIn("+'/reports'", text)
        self.assertIn("+'/discussions'", text)
        self.assertIn("+'/plan/patches'", text)
        # Patch adoption is synchronous and never a /review/plan action.
        adopt_start = text.index("async function reviewDiscussAdoptPatch")
        adopt_end = text.index("function reviewVlmRetry")
        adopt_body = text[adopt_start:adopt_end]
        self.assertNotIn("reviewVlmPollJob", adopt_body)
        self.assertNotIn("+'/plan'", adopt_body)
        self.assertIn("source:'vlm'", adopt_body)
        self.assertIn("patchConflict", adopt_body)
        self.assertIn("采纳按钮已禁用", text)
        # A clarification option sends a discussion message, never an
        # operation: it only opens the confirmation dialog.
        clar_start = text.index("function reviewClarificationAnswer")
        clar_end = text.index("async function reviewDiscussAdoptPatch")
        clar_body = text[clar_start:clar_end]
        self.assertIn("reviewVlmConfirmOpen('clarify'", clar_body)
        self.assertNotIn("+'/plan'", clar_body)
        self.assertNotIn("+'/batches'", clar_body)
        # Interrupted report/discuss jobs only retry on an explicit action.
        self.assertIn('data-r="vlm-retry"', text)
        self.assertIn("任务中断，可手动重试", text)
        self.assertIn("不会自动重跑", text)
        self.assertIn("function reviewVlmRetry", text)
        # No-VLM degradation stays fully usable and shows a Chinese reason.
        self.assertIn("VLM 未配置", text)
        self.assertIn("规则报告仍可用", text)
        self.assertIn("reviewVlmUsable", text)
        # Report provider / source badge / partial failures.
        self.assertIn("reviewSuggestionSourceBadge", text)
        self.assertIn("VLM 建议", text)
        self.assertIn("部分簇失败", text)
        self.assertIn("partial_failures", text)
        self.assertIn("text-only", text)
        self.assertIn("vlm_provider", text)
        self.assertIn("VLM 后台任务", text)
        # Discussion scope selector labels (current item / current cluster /
        # whole peel) and the member/boundary/exclusion preview.
        for label in ("当前项", "当前簇", "全 peel", "成员预览", "正例",
                      "边界例", "排除项"):
            self.assertIn(label, text)
        # Nothing in the UI sends a provider override or a token.
        self.assertNotIn("PEELFEED_LLM_TOKEN", text)
        self.assertNotIn("api_key", text[text.index("function reviewVlmBarHtml"):
                                        text.index("/* ---- workspace dialog")])

    def test_served_page_ships_cp51_source_choice_and_skipped_controls(self):
        """CP5.1 H5/H7/H2/H3: source choice, vlm_disagrees, pricing, skipped."""
        text = _ui()
        for marker in (
                "function reviewItemSourceChoice",
                "function reviewItemSourceChoiceHtml",
                "function reviewItemSourceNoticeHtml",
                "function reviewSelectedSources",
                "function reviewSuggestionIdentity",
                "function reviewItemCluster",
                "data-r=\"accept-source\"",
                "vlm_disagrees",
                "采纳来源",
                "ambiguous_suggestion",
                "source_missing",
                "REVIEW_SKIPPED_REASON_LABEL",
                "reviewSkippedReasonLabel",
                "跳过的簇/项",
                "费用未计价",
                "vlm_priced",
                "vlm_max_calls_per_peel",
                "pendingPatchTurn",
                "body.turn=ctx.pendingPatchTurn",
                "每 peel 调用上限"):
            self.assertIn(marker, text)
        # The confirm-selected request carries the sources map only when a
        # source choice is rendered for a ticked item.
        accept_start = text.index("}else if(kind==='accept-selected'){")
        accept_end = text.index("}else if(kind==='clear'){", accept_start)
        accept_body = text[accept_start:accept_end]
        self.assertIn("reviewSelectedSources()", accept_body)
        self.assertIn("body.sources=sources", accept_body)
        # Adopting a discussion patch sends the assistant turn number.
        adopt_start = text.index("async function reviewDiscussAdoptPatch")
        adopt_end = text.index("function reviewVlmRetry", adopt_start)
        adopt_body = text[adopt_start:adopt_end]
        self.assertIn("body.turn=ctx.pendingPatchTurn", adopt_body)
        # Skipped reasons are rendered in the VLM report area, not the plan.
        provider_start = text.index("function reviewReportProviderHtml")
        provider_end = text.index("function paintReviewReportProvider",
                                  provider_start)
        provider_body = text[provider_start:provider_end]
        self.assertIn("skipped", provider_body)
        self.assertIn("reviewSkippedReasonLabel", provider_body)

    def test_served_page_ships_cp5_vlm_controls(self):
        with _served_home():
            from fastapi.testclient import TestClient
            from peekseed import app as app_module

            response = TestClient(app_module.create_app()).get("/")
            self.assertEqual(response.status_code, 200, response.text)
            page = response.text
        for marker in ('id="reviewVlmBar"', 'id="reviewVlmJob"',
                       'id="reviewVlmConfirm"', 'id="reviewVlmConfirmGo"',
                       'id="reviewReportProvider"', 'id="reviewClarifications"',
                       'id="reviewDiscuss"', 'id="reviewPatchDiff"',
                       'id="reviewPatchConflict"',
                       "生成/更新 VLM 报告", "与 VLM 讨论",
                       "确认发送审阅证据给 VLM？", "确认并发送",
                       "仅文本（不发送图片）", "VLM 后台任务",
                       "任务中断，可手动重试", "不会自动重跑",
                       "VLM 未配置", "规则报告仍可用",
                       "reviewVlmGenerateOpen", "reviewVlmConfirmed",
                       "reviewDiscussSendOpen", "reviewDiscussAdoptPatch",
                       "reviewClarificationAnswer", "paintReviewVlmBar",
                       "paintReviewReportProvider", "paintReviewClarifications",
                       "paintReviewVlmJob", "paintReviewDiscuss",
                       "'/reports'", "'/discussions'", "'/plan/patches'"):
            self.assertIn(marker, page)
        # CP5: the confirmation dialog is the only path to a provider call; the
        # UI never carries the child-only token.
        self.assertNotIn("PEELFEED_LLM_TOKEN", page)
        self.assertNotIn("PEELFEED_LLM_BASE", page)


# ---------------------------------------------------------------------------
# CP5 Node stub-DOM behaviour harness: runs the real review JS block against
# canned fetch responses (no browser, no network) and proves:
#   * opening the review paints the VLM bar from capabilities.vlm/vlm_provider
#     without any implicit provider call;
#   * the confirmation dialog is a hard gate: cancel sends nothing, only the
#     explicit Go click builds confirmed:true and POSTs the frozen bodies;
#   * a VLM report job polls to done, refreshes the report and renders the
#     provider summary (kind/model/vision/cached/text-only/partial failures);
#   * a discussion send polls to done, reads the result summary through the
#     asset route, renders turns + the draft patch and adopts it through the
#     synchronous plan/patches route (never a /review/plan action);
#   * a 409 patch conflict shows the conflict list and disables adopt;
#   * a clarification option opens the discussion confirmation (never a plan
#     operation);
#   * capabilities.vlm=false disables both actions with a Chinese reason while
#     the rules report still works;
#   * persisted report/discuss descriptors re-attach after a reload, an
#     interrupted job shows an explicit retry, and nothing auto-reruns.
# ---------------------------------------------------------------------------

NODE_CP5_HARNESS = r"""
/*
 * Drive the real review JS block with a stub DOM + canned fetch. No browser, no
 * network: every request is recorded and answered from the scenario tables.
 */
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("/* ---- review report dialog", 150000);
const end = html.indexOf("/* ---- workspace dialog", start);
if (start < 0 || end < 0) throw new Error("review block markers not found");
const block = html.slice(start, end);

const elements = new Map();
function element(key) {
  if (elements.has(key)) return elements.get(key);
  const listeners = {};
  const value = {
    id: key, innerHTML: "", textContent: "", hidden: true, disabled: false,
    value: "", files: [], checked: false, listeners, onclick: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    appendChild() {}, focus() {}, blur() {}, closest() { return null; },
    remove() {}, querySelector() { return element(key + ":child"); },
    querySelectorAll() { return []; }, getAttribute() { return null; },
    setAttribute() {}, style: {}, dataset: {}, scrollIntoView() {},
    isConnected: true,
  };
  elements.set(key, value);
  return value;
}
const document = {
  querySelector(selector) { return element(selector); },
  querySelectorAll() { return []; },
  addEventListener() {}, getElementById(id) { return element("#" + id); },
  createElement() { return element("created"); },
  createTextNode() { return {}; },
  body: { style: {}, classList: { toggle() {} }, appendChild() {} },
  documentElement: { style: {}, classList: { toggle() {} } },
  head: { appendChild() {} },
};

const calls = [];
const reportVlmPosts = [];
const reportRulesPosts = [];
const discussPosts = [];
const patchPosts = [];
const planActionBodies = [];
let planActionPosts = 0;
let reviewPayloadCurrent = null;
let sourcesPayload = { sources: [] };
let conflictMode = false;
const sha = "a".repeat(64);

function item(id, clusterId) {
  return { id: id, code: "table_header_absent", kind: "table",
    severity: "warning", blocking: false, asset_id: "table-" + id, pages: [1],
    cluster_id: clusterId, message: "m", triage: "deterministic_candidate",
    status: "needs_decision",
    evidence: { hash: "h", crops: [], native_line_ids: [], ocr_evidence: [] },
    current: {}, candidates: [], strategies: [], entry: null, resolution: null,
    stale: false };
}
function cluster(cid, ids) {
  return { cluster_id: cid, code: "table_header_absent", title: "t",
    kind: "table", item_count: ids.length, human_pending: ids.length,
    representative_item_id: ids[0], states: { needs_decision: ids.length },
    conclusion: "c",
    suggestions: [{ item_id: ids[0],
      strategy: { id: "use_first_row_header", version: 1 }, params: {},
      evidence_refs: [], validator: { status: "ready", reasons: [] },
      in_plan: false, state: "ready", source: "vlm" }],
    clarifications: [{ question: "第 0 行是否是表头？",
      options: ["是", "否"], item_id: ids[0] }],
    strategies: [], item_ids: ids };
}
const ITEMS = [item("it-a", "cl-a"), item("it-b", "cl-b")];
const CLUSTERS = [cluster("cl-a", ["it-a"]), cluster("cl-b", ["it-b"])];

const VLM_CAPS = {
  review_schema: 2, native_lines: true,
  ocr: false, ocr_configured: false, ocr_detail: null,
  ocr_status: { available: false, checked_at: null, error: null },
  vlm: true, vlm_detail: null, vlm_provider: "answer_link",
  vlm_vision: true, vlm_model: "qwen2.5-vl-72b",
  vlm_link_id: "answer-main", vlm_budget_usd: 2.0, vlm_spent_usd: 0.35,
  vlm_calls: 1, vlm_vision_checked_at: "2026-11-09T03:10:00Z",
  /* CP5.1 H7 (wire §10.7): prices not configured -> honest "费用未计价". */
  vlm_priced: false, vlm_max_calls_per_peel: 100,
};
const VLM_CAPS_OFF = Object.assign({}, VLM_CAPS, {
  vlm: false,
  vlm_detail: "VLM 不可用：no enabled answer link",
  vlm_model: null, vlm_link_id: null, vlm_vision: null,
});
const VLM_REPORT = {
  status: "partial", generated_at: "2026-11-09T03:10:00Z", stale: false,
  provider: { kind: "vlm", model: "qwen2.5-vl-72b",
    prompt_version: "peel-reviews@1", link_id: "answer-main",
    vision: true, cached: true, text_only: false },
  report: {
    schema_version: 1, status: "partial", stale: false,
    generated_at: "2026-11-09T03:10:00Z",
    provider: { kind: "vlm", model: "qwen2.5-vl-72b",
      prompt_version: "peel-reviews@1", link_id: "answer-main",
      vision: true, cached: true, text_only: false },
    counts: { vlm_suggestions: 2, clarifications: 1, failed_checks: 0 },
    partial_failures: [{ cluster_id: "cl-b", code: "vlm_bad_response",
      message: "cluster response was void" }],
    /* CP5.1 H2/H3 (wire §10.3): additive skipped entries with reason codes. */
    skipped: [
      { cluster_id: "cl-c", item_ids: ["it-c", "it-d"],
        reason: "ocr_not_ready" },
      { cluster_id: "cl-d", item_ids: ["it-e"], reason: "item_cap" },
      { cluster_id: "cl-e", item_ids: [], reason: "budget_exhausted" },
    ],
    llm_usage: { calls: 2, tokens_input: 1200, tokens_output: 400,
      cost_usd: "0.00" },
    clusters: [],
  },
};
function reviewPayload(caps, report, planVersion) {
  return {
    peel_id: "peel", review_schema: 2,
    generated_at: "2026-11-09T03:10:00Z", profile: "generic",
    capabilities: caps, report: report || null, ocr: null,
    counts: { raw_items: 2, items: 2, clusters: 2, human_pending: 2,
      human_pending_clusters: 2, pending_evidence: 0, auto_resolved: 0,
      needs_decision: 2, draft: 0, confirmed: 0, applied: 0, deferred: 0,
      failed_check: 0, stale: 0, unsupported_items: 0 },
    triage_counts: { deterministic_candidate: 2, needs_decision: 0,
      needs_ocr: 0, informational: 0, unsupported: 0 },
    plan: { plan_version: planVersion || 0, entries: 0, draft: 0,
      confirmed: 0, locked: 0, stale: 0 },
    clusters: CLUSTERS, items: ITEMS,
  };
}
const REVIEW_VLM = reviewPayload(VLM_CAPS, VLM_REPORT, 0);
const REVIEW_VLM_DONE = reviewPayload(VLM_CAPS, VLM_REPORT, 0);
const REVIEW_VLM_PLAN1 = reviewPayload(VLM_CAPS, VLM_REPORT, 1);
const REVIEW_VLM_OFF = reviewPayload(VLM_CAPS_OFF, null, 0);
/* CP5.1 H5 (wire §10.5): a cluster flagged ``vlm_disagrees`` whose item has a
   rule and a VLM ready suggestion with different action identities. */
const REVIEW_VLM_SOURCE = (function () {
  const payload = reviewPayload(VLM_CAPS, VLM_REPORT, 0);
  const sourceCluster = Object.assign({}, payload.clusters[0], {
    vlm_disagrees: true,
    suggestions: [
      { item_id: "it-a", strategy: { id: "use_first_row_header", version: 1 },
        params: { rows: [0] }, evidence_refs: [], in_plan: false,
        state: "ready", validator: { status: "ready", reasons: [] },
        source: "rule" },
      { item_id: "it-a", strategy: { id: "confirm_no_header", version: 1 },
        params: {}, evidence_refs: [], in_plan: false,
        state: "ready", validator: { status: "ready", reasons: [] },
        source: "vlm" },
    ],
  });
  payload.clusters = [sourceCluster, payload.clusters[1]];
  return payload;
})();
const VLM_PATCH = {
  scope: { kind: "cluster", item_id: null, cluster_id: "cl-a" },
  members: ["it-a"], exclusions: ["it-b"], boundary_cases: ["it-b"],
  changes: [{ op: "select", item_id: "it-a",
    strategy: { id: "use_first_row_header", version: 1 },
    params: { rows: [0] }, evidence_refs: ["asset:table-it-a"],
    uncertainties: ["row 0"] }],
};
const DISCUSS_RESULT = {
  schema_version: 1, intent: "modify", turn: 2,
  reply: "建议把第 0 行作为表头。", uncertainties: ["row 0 与数据行相似"],
  clarifications: [], patch: VLM_PATCH,
  model: "qwen2.5-vl-72b", link_id: "answer-main",
  prompt_version: "peel-reviews@1", vision: true, cached: false,
  at: "2026-11-09T03:10:02Z",
  usage: { input_tokens: 900, output_tokens: 200, cost_usd: "0.00" },
  evidence_hashes: {}, base_plan_version: 0,
};

function sourceRow(jobs) {
  return { name: "manual.pdf", sha256: sha, status: "extracted",
    extraction: { state: "extracted", peel_id: "peel",
      conversion_id: "c", review_jobs: jobs || {} } };
}
function response(status, payload) {
  return { ok: status >= 200 && status < 300, status: status,
           json: async function () { return payload; },
           text: async function () { return JSON.stringify(payload); } };
}
function jobRecord(type, id, status, resultRef) {
  return { job_id: id, type: type, status: status,
    progress: { stage: "done", done: 1, total: 1 },
    result_ref: resultRef || null,
    error: status === "failed"
      ? { code: "vlm_bad_response", message: "void" }
      : (status === "interrupted"
        ? { code: "interrupted",
            message: "the job process is no longer running" }
        : null) };
}
function fetchStub(url, opts) {
  const method = String((opts || {}).method || "GET").toUpperCase();
  const body = String((opts || {}).body || "");
  calls.push({ url: String(url), method: method, body: body });
  const u = String(url);
  /* Asset URLs contain "/review/jobs/" too, so match assets first. */
  if (u.indexOf("/assets/") >= 0) {
    return Promise.resolve(response(200, DISCUSS_RESULT));
  }
  if (method === "POST" && u.indexOf("/review/plan/patches") >= 0) {
    patchPosts.push(JSON.parse(body || "{}"));
    if (conflictMode) {
      return Promise.resolve(response(409, {
        detail: "peelfeed plan patch failed: error [conflict] patch touches "
          + "locked/batch-bound entries (conflicts: it-a)" }));
    }
    reviewPayloadCurrent = REVIEW_VLM_PLAN1;
    return Promise.resolve(response(200, {
      peel_id: "peel", review_schema: 2, plan_version: 1, entries: [],
      diff: { added: [{ item_id: "it-a" }], changed: [],
              removed: [], unchanged: 0 } }));
  }
  if (method === "POST" && u.indexOf("/review/reports") >= 0) {
    const parsed = JSON.parse(body || "{}");
    if (parsed.mode === "vlm") {
      reportVlmPosts.push(parsed);
      return Promise.resolve(response(202,
        { job_id: "report-vlm-1", status: "queued" }));
    }
    reportRulesPosts.push(parsed);
    return Promise.resolve(response(202,
      { job_id: "report-rules-1", status: "queued" }));
  }
  if (method === "POST" && u.indexOf("/review/discussions") >= 0) {
    discussPosts.push(JSON.parse(body || "{}"));
    return Promise.resolve(response(202, { job_id: "discuss-1",
      conversation_id: "conv-1", status: "queued" }));
  }
  if (u.indexOf("/review/jobs/") >= 0) {
    if (u.indexOf("report-vlm-1") >= 0) {
      reviewPayloadCurrent = REVIEW_VLM_DONE;
      return Promise.resolve(response(200,
        jobRecord("report", "report-vlm-1", "done", "report.json")));
    }
    if (u.indexOf("report-rules-1") >= 0) {
      reviewPayloadCurrent = REVIEW_VLM;
      return Promise.resolve(response(200,
        jobRecord("report", "report-rules-1", "done", "report.json")));
    }
    if (u.indexOf("discuss-1") >= 0) {
      return Promise.resolve(response(200, jobRecord("discuss", "discuss-1",
        "done", "conversations/conv-1.jsonl")));
    }
    if (u.indexOf("report-old") >= 0) {
      return Promise.resolve(response(200,
        jobRecord("report", "report-old", "interrupted")));
    }
    if (u.indexOf("discuss-old") >= 0) {
      return Promise.resolve(response(200, jobRecord("discuss", "discuss-old",
        "done", "conversations/conv-old.jsonl")));
    }
    return Promise.resolve(response(404, { detail: "unknown job" }));
  }
  if (method === "POST" && (u.endsWith("/review/plan")
                            || u.indexOf("/review/plan?") >= 0)) {
    planActionPosts += 1;
    const parsedPlan = JSON.parse(body || "{}");
    planActionBodies.push(parsedPlan);
    return Promise.resolve(response(200, {
      plan_version: 1, entries: [],
      accepted: [{ item_id: (parsedPlan.item_ids || ["it-a"])[0] }] }));
  }
  if (u.indexOf("/review?") >= 0 || u.endsWith("/review")) {
    return Promise.resolve(response(200, reviewPayloadCurrent));
  }
  if (u.indexOf("/sources") >= 0) {
    return Promise.resolve(response(200, sourcesPayload));
  }
  return Promise.reject(new Error("unexpected fetch " + method + " " + u));
}

const PF_STATUS = {
  handshake: { available: true, version: "0.1.0", review_schema: 2,
    ocr: { configured: false } },
  config: { configured: true },
};
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escAttr(value) { return esc(value); }
function toast() {}
function fmtBytes(value) { return String(value) + " B"; }
function fmtTime(value) { return String(value || ""); }
function $(selector) { return element(selector); }
function getJSON(path) {
  return fetchStub(path).then(function (r) {
    if (!r.ok) throw new Error(path + " returned HTTP " + r.status);
    return r.json();
  });
}
function callJSON() { throw new Error("callJSON not used in the harness"); }
async function loadDatasets() { return []; }

const sandbox = {
  document, window: {}, DATASETS: [], DATASET_SOURCES: {},
  REVIEW_COUNTS: {}, REVIEW_CTX: null, PF_STATUS: PF_STATUS,
  $, esc, escAttr, callJSON, getJSON, toast, fmtBytes, fmtTime,
  loadDatasets, console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Number, String, JSON, Math, Date, Error, RegExp, Array, Object,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  fetch: fetchStub,
  location: { hash: "", origin: "http://127.0.0.1:0", href: "" },
  history: { replaceState() {}, pushState() {} },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  sessionStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  navigator: { clipboard: { writeText() { return Promise.resolve(); } } },
  requestAnimationFrame(fn) { return setTimeout(fn, 0); },
  matchMedia() { return { matches: false, addEventListener() {} }; },
  alert() {}, confirm() { return true; }, prompt() { return ""; },
  innerWidth: 1400, innerHeight: 900,
  addEventListener() {}, removeEventListener() {},
};
sandbox.globalThis = sandbox;

const code = block
  + "\n;globalThis.__review = {"
  + " openReview: openReview,"
  + " reviewVlmGenerateOpen: reviewVlmGenerateOpen,"
  + " reviewVlmConfirmClose: reviewVlmConfirmClose,"
  + " reviewVlmConfirmed: reviewVlmConfirmed,"
  + " reviewDiscussOpen: reviewDiscussOpen,"
  + " reviewDiscussSendOpen: reviewDiscussSendOpen,"
  + " reviewDiscussAdoptPatch: reviewDiscussAdoptPatch,"
  + " reviewClarificationAnswer: reviewClarificationAnswer,"
  + " reviewVlmRetry: reviewVlmRetry,"
  + " reviewAction: reviewAction,"
  + " reviewSelectedSources: reviewSelectedSources,"
  + " reviewItemSourceChoice: reviewItemSourceChoice,"
  + " generateReviewReport: generateReviewReport,"
  + " reviewLoadSource: reviewLoadSource,"
  + " paintReviewVlmJob: paintReviewVlmJob,"
  + " paintReviewVlmConfirmScope: paintReviewVlmConfirmScope,"
  + " getContext: function(){ return REVIEW_CTX; },"
  + " setData: function(data){ REVIEW_CTX={dataset:'docs',sha:'" + sha
  + "',data:data}; },"
  + " element: function(key){ return element(key); },"
  + " calls: function(){ return calls; } };";
const context = vm.createContext(sandbox);
vm.runInContext(code, context, { filename: "index.html#review-block" });

function tick(ms) { return new Promise((resolve) => setTimeout(resolve, ms || 80)); }
function htmlOf(selector) { return element(selector).innerHTML; }
function vlmPosts() {
  return calls.filter(function (call) {
    return call.method === "POST" && (call.url.indexOf("/review/reports") >= 0
      || call.url.indexOf("/review/discussions") >= 0
      || call.url.indexOf("/review/plan/patches") >= 0);
  });
}
function resetCounters() {
  calls.length = 0;
  reportVlmPosts.length = 0;
  reportRulesPosts.length = 0;
  discussPosts.length = 0;
  patchPosts.length = 0;
  planActionBodies.length = 0;
  planActionPosts = 0;
  conflictMode = false;
}
function button(attrs) {
  return { getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(attrs, name)
      ? attrs[name] : null;
  } };
}

async function runVlmFlow() {
  resetCounters();
  reviewPayloadCurrent = REVIEW_VLM;
  sourcesPayload = { sources: [sourceRow({})] };
  const steps = [];
  await sandbox.__review.openReview("docs", sha);
  await tick();
  steps.push({ step: "vlm-open",
    bar: htmlOf("#reviewVlmBar"),
    provider: htmlOf("#reviewReportProvider"),
    clarifications: htmlOf("#reviewClarifications"),
    vlmPosts: vlmPosts().length,
    planPosts: planActionPosts });
  /* Opening the confirmation dialog must not send anything. */
  sandbox.__review.reviewVlmGenerateOpen();
  await tick(20);
  steps.push({ step: "vlm-confirm-open",
    hidden: element("#reviewVlmConfirm").hidden,
    info: htmlOf("#reviewVlmConfirmInfo"),
    target: element("#reviewVlmConfirmTarget").textContent,
    scope: element("#reviewVlmConfirmScope").textContent,
    vlmPosts: vlmPosts().length });
  sandbox.__review.reviewVlmConfirmClose();
  await tick(20);
  steps.push({ step: "vlm-confirm-cancel",
    hidden: element("#reviewVlmConfirm").hidden,
    vlmPosts: vlmPosts().length });
  /* Explicit Go: report body + confirmation gate. */
  sandbox.__review.reviewVlmGenerateOpen();
  sandbox.__review.reviewVlmConfirmed();
  await tick(160);
  steps.push({ step: "vlm-report-sent",
    posts: reportVlmPosts.slice(),
    bar: htmlOf("#reviewVlmBar"),
    provider: htmlOf("#reviewReportProvider"),
    vlmJob: htmlOf("#reviewVlmJob"),
    msg: htmlOf("#reviewMsg"),
    vlmPosts: vlmPosts().length,
    planPosts: planActionPosts });
  /* Discussion: open panel, scope cluster, send via confirmation. */
  sandbox.__review.reviewDiscussOpen();
  element("#reviewDiscussScope").value = "cluster";
  element("#reviewDiscussInput").value = "为什么把第 0 行作为表头？";
  sandbox.__review.reviewDiscussSendOpen();
  await tick(20);
  steps.push({ step: "discuss-confirm",
    hidden: element("#reviewVlmConfirm").hidden,
    info: htmlOf("#reviewVlmConfirmInfo"),
    target: element("#reviewVlmConfirmTarget").textContent,
    discussPosts: discussPosts.length });
  sandbox.__review.reviewVlmConfirmed();
  await tick(200);
  steps.push({ step: "discuss-sent",
    posts: discussPosts.slice(),
    turns: htmlOf("#reviewDiscussTurns"),
    patch: htmlOf("#reviewDiscussPatch"),
    members: htmlOf("#reviewDiscussMembers"),
    adoptDisabled: element("#reviewDiscussAdoptPatch").disabled,
    msg: htmlOf("#reviewMsg"),
    planPosts: planActionPosts });
  /* Adopt the draft patch through the synchronous plan/patches route. */
  await sandbox.__review.reviewDiscussAdoptPatch();
  await tick(120);
  steps.push({ step: "patch-adopted",
    posts: patchPosts.slice(),
    diff: htmlOf("#reviewPatchDiff"),
    conflict: htmlOf("#reviewPatchConflict"),
    adoptDisabled: element("#reviewDiscussAdoptPatch").disabled,
    msg: htmlOf("#reviewMsg"),
    planPosts: planActionPosts });
  /* A 409 conflict disables adopt and shows the conflict list. */
  conflictMode = true;
  await sandbox.__review.reviewDiscussAdoptPatch();
  await tick(120);
  steps.push({ step: "patch-conflict",
    posts: patchPosts.slice(),
    conflict: htmlOf("#reviewPatchConflict"),
    adoptDisabled: element("#reviewDiscussAdoptPatch").disabled,
    msg: htmlOf("#reviewMsg") });
  /* A clarification option opens the discussion confirmation; no operation. */
  const postsBefore = vlmPosts().length;
  sandbox.__review.reviewClarificationAnswer(button({
    "data-option": "是", "data-question": "第 0 行是否是表头？",
    "data-item": "it-a", "data-cluster": "cl-a" }));
  await tick(20);
  steps.push({ step: "clarify-answer",
    hidden: element("#reviewVlmConfirm").hidden,
    target: element("#reviewVlmConfirmTarget").textContent,
    input: element("#reviewDiscussInput").value,
    newPosts: vlmPosts().length - postsBefore,
    planPosts: planActionPosts });
  sandbox.__review.reviewVlmConfirmClose();
  return steps;
}

async function runTextOnlyFlow() {
  resetCounters();
  reviewPayloadCurrent = REVIEW_VLM;
  sourcesPayload = { sources: [sourceRow({})] };
  await sandbox.__review.openReview("docs", sha);
  await tick();
  sandbox.__review.reviewVlmGenerateOpen();
  element("#reviewVlmTextOnly").checked = true;
  sandbox.__review.paintReviewVlmConfirmScope();
  sandbox.__review.reviewVlmConfirmed();
  await tick(160);
  return [{ step: "text-only",
    posts: reportVlmPosts.slice(),
    scope: element("#reviewVlmConfirmScope").textContent,
    msg: htmlOf("#reviewMsg") }];
}

async function runNoVlmFlow() {
  resetCounters();
  reviewPayloadCurrent = REVIEW_VLM_OFF;
  sourcesPayload = { sources: [sourceRow({})] };
  const steps = [];
  await sandbox.__review.openReview("docs", sha);
  await tick();
  steps.push({ step: "novlm-open",
    bar: htmlOf("#reviewVlmBar"),
    vlmPosts: vlmPosts().length,
    msg: htmlOf("#reviewMsg") });
  sandbox.__review.reviewVlmGenerateOpen();
  await tick(20);
  steps.push({ step: "novlm-generate-blocked",
    hidden: element("#reviewVlmConfirm").hidden,
    vlmPosts: vlmPosts().length,
    msg: htmlOf("#reviewMsg") });
  await sandbox.__review.generateReviewReport();
  await tick(160);
  steps.push({ step: "novlm-rules-report",
    posts: reportRulesPosts.slice(),
    vlmPosts: vlmPosts().length,
    msg: htmlOf("#reviewMsg") });
  return steps;
}

async function runReattachFlow() {
  resetCounters();
  reviewPayloadCurrent = REVIEW_VLM;
  sourcesPayload = { sources: [sourceRow({
    "report-old": { type: "report", created_at: "2026-11-09T03:00:00Z",
      batch_id: null, conversation_id: null },
    "discuss-old": { type: "discuss", created_at: "2026-11-09T03:05:00Z",
      batch_id: null, conversation_id: "conv-old" },
  })] };
  const steps = [];
  await sandbox.__review.openReview("docs", sha);
  await tick(120);
  steps.push({ step: "reattach-open",
    vlmJob: htmlOf("#reviewVlmJob"),
    vlmPosts: vlmPosts().length,
    msg: htmlOf("#reviewMsg") });
  sandbox.__review.reviewVlmRetry(button({ "data-type": "report",
    "data-job": "report-old" }));
  await tick(20);
  steps.push({ step: "reattach-retry",
    hidden: element("#reviewVlmConfirm").hidden,
    title: element("#reviewVlmConfirmTitle").textContent,
    vlmPosts: vlmPosts().length,
    msg: htmlOf("#reviewMsg") });
  sandbox.__review.reviewVlmConfirmClose();
  return steps;
}

async function runSourceChoiceFlow() {
  resetCounters();
  reviewPayloadCurrent = REVIEW_VLM_SOURCE;
  sourcesPayload = { sources: [sourceRow({})] };
  const steps = [];
  await sandbox.__review.openReview("docs", sha);
  await tick();
  steps.push({ step: "source-open",
    detail: htmlOf("#reviewDetail"),
    choice: sandbox.__review.reviewItemSourceChoice(
      { id: "it-a", cluster_id: "cl-a" }),
    planPosts: planActionPosts });
  /* Tick it-a and choose VLM as its adoption source, then confirm-selected.
     The stub DOM returns the checkbox/source elements the real render emits. */
  const detail = element("#reviewDetail");
  detail.querySelectorAll = function (selector) {
    if (String(selector).indexOf("accept-pick") >= 0) {
      return [{ checked: true, value: "it-a" }];
    }
    if (String(selector).indexOf("accept-source") >= 0) {
      return [{ value: "vlm", getAttribute: function (name) {
        return name === "data-item" ? "it-a" : null;
      } }];
    }
    return [];
  };
  await sandbox.__review.reviewAction({
    getAttribute: function (name) {
      return name === "data-r" ? "accept-selected" : null;
    },
    closest: function () { return null; },
    disabled: false,
  });
  await tick(160);
  steps.push({ step: "source-accept",
    posts: planActionBodies.slice(),
    msg: htmlOf("#reviewMsg"),
    planPosts: planActionPosts });
  detail.querySelectorAll = function () { return []; };
  return steps;
}

async function main() {
  const results = [];
  results.push(...await runVlmFlow());
  results.push(...await runTextOnlyFlow());
  results.push(...await runNoVlmFlow());
  results.push(...await runReattachFlow());
  results.push(...await runSourceChoiceFlow());
  console.log(JSON.stringify(results));
}

main().catch(function (error) {
  console.error((error && error.stack) || error);
  process.exit(1);
});
"""


@unittest.skipUnless(shutil.which("node"),
                     "node is needed for the CP5 DOM behaviour harness")
class ReviewUiCp5BehaviorTests(unittest.TestCase):
    def _run_harness(self):
        base = Path(tempfile.mkdtemp(prefix="peekseed-cp5-ui-"))
        try:
            harness = base / "cp5_behavior.js"
            harness.write_text(NODE_CP5_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [shutil.which("node"), str(harness), str(UI_PATH)],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            return {row["step"]: row for row in json.loads(completed.stdout)}
        finally:
            shutil.rmtree(str(base), ignore_errors=True)

    def test_open_review_paints_vlm_bar_without_implicit_calls(self):
        by_step = self._run_harness()
        opened = by_step["vlm-open"]
        self.assertEqual(opened["vlmPosts"], 0, opened)
        self.assertEqual(opened["planPosts"], 0, opened)
        bar = opened["bar"]
        for marker in ("VLM 已配置", "qwen2.5-vl-72b", "answer-main",
                       "支持图片", "预算 $2.00", "生成/更新 VLM 报告",
                       "与 VLM 讨论", "费用未计价", "每 peel 调用上限 100"):
            self.assertIn(marker, bar, opened)
        provider = opened["provider"]
        for marker in ("VLM", "qwen2.5-vl-72b", "支持图片", "缓存命中",
                       "VLM 建议 2", "澄清问题 1", "部分簇失败",
                       "vlm_bad_response", "跳过的簇/项", "OCR 未就绪",
                       "超过单项上限", "预算耗尽", "cl-c", "cl-d"):
            self.assertIn(marker, provider, opened)
        self.assertIn("第 0 行是否是表头？", opened["clarifications"], opened)
        self.assertIn("是", opened["clarifications"], opened)

    def test_confirmation_gate_sends_nothing_until_confirmed(self):
        by_step = self._run_harness()
        confirm = by_step["vlm-confirm-open"]
        self.assertFalse(confirm["hidden"], confirm)
        self.assertEqual(confirm["vlmPosts"], 0, confirm)
        for marker in ("answer_link", "answer-main", "qwen2.5-vl-72b",
                       "支持图片", "项 2 / 簇 2"):
            self.assertIn(marker, confirm["info"], confirm)
        self.assertIn("生成/更新全 peel VLM 报告", confirm["target"], confirm)
        self.assertIn("crop ✓", confirm["scope"], confirm)
        cancel = by_step["vlm-confirm-cancel"]
        self.assertTrue(cancel["hidden"], cancel)
        self.assertEqual(cancel["vlmPosts"], 0, cancel)
        sent = by_step["vlm-report-sent"]
        self.assertEqual(len(sent["posts"]), 1, sent)
        body = sent["posts"][0]
        self.assertEqual(body["mode"], "vlm")
        confirmation = body["confirmation"]
        self.assertIs(confirmation["confirmed"], True)
        self.assertEqual(confirmation["provider"], "answer_link")
        self.assertEqual(confirmation["link_id"], "answer-main")
        self.assertEqual(confirmation["model"], "qwen2.5-vl-72b")
        self.assertEqual(confirmation["scope"]["item_ids"],
                         ["it-a", "it-b"])
        self.assertEqual(confirmation["scope"]["cluster_ids"],
                         ["cl-a", "cl-b"])
        for flag in ("include_crop", "include_original_page",
                     "include_adjacent_pages", "include_native",
                     "include_ocr"):
            self.assertIs(confirmation["scope"][flag], True, confirmation)
        self.assertIn("VLM 报告已生成", sent["msg"], sent)
        self.assertIn("VLM 后台任务", sent["vlmJob"], sent)
        self.assertIn("已完成", sent["vlmJob"], sent)
        # A report job never edits the plan (no /review/plan action).
        self.assertEqual(sent["planPosts"], 0, sent)

    def test_discussion_send_poll_reply_and_patch_adoption(self):
        by_step = self._run_harness()
        confirm = by_step["discuss-confirm"]
        self.assertFalse(confirm["hidden"], confirm)
        self.assertEqual(confirm["discussPosts"], 0, confirm)
        self.assertIn("与 VLM 讨论", confirm["target"], confirm)
        sent = by_step["discuss-sent"]
        self.assertEqual(len(sent["posts"]), 1, sent)
        body = sent["posts"][0]
        self.assertEqual(body["scope"], {"cluster_id": "cl-a"})
        self.assertEqual(body["message"], "为什么把第 0 行作为表头？")
        self.assertEqual(body["base_plan_version"], 0)
        self.assertNotIn("conversation_id", body)
        self.assertIs(body["confirmation"]["confirmed"], True)
        self.assertEqual(body["confirmation"]["scope"]["item_ids"], ["it-a"])
        self.assertEqual(body["confirmation"]["scope"]["cluster_ids"],
                         ["cl-a"])
        turns = sent["turns"]
        self.assertIn("为什么把第 0 行作为表头？", turns, sent)
        self.assertIn("建议把第 0 行作为表头。", turns, sent)
        self.assertIn("修改决定", turns, sent)
        self.assertIn("返回了计划 patch", turns, sent)
        self.assertIn("成员预览", sent["members"], sent)
        self.assertIn("正例 1", sent["members"], sent)
        self.assertIn("排除项 1", sent["members"], sent)
        self.assertIn("边界例 1", sent["members"], sent)
        self.assertIn("VLM 计划 patch 预览", sent["patch"], sent)
        self.assertFalse(sent["adoptDisabled"], sent)
        adopted = by_step["patch-adopted"]
        self.assertEqual(len(adopted["posts"]), 1, adopted)
        patch_body = adopted["posts"][0]
        self.assertEqual(patch_body["base_plan_version"], 0)
        self.assertEqual(patch_body["source"], "vlm")
        self.assertEqual(patch_body["conversation_id"], "conv-1")
        # CP5.1 H5: the adopted patch names the archived assistant turn.
        self.assertEqual(patch_body["turn"], 2, patch_body)
        self.assertEqual(patch_body["patch"]["scope"]["kind"], "cluster")
        self.assertIn("计划 diff", adopted["diff"], adopted)
        self.assertIn("新增 1", adopted["diff"], adopted)
        self.assertIn("it-a", adopted["diff"], adopted)
        self.assertTrue(adopted["conflict"] == "", adopted)
        # Adoption only edited the draft plan: no /review/plan action ran.
        self.assertEqual(adopted["planPosts"], 0, adopted)
        self.assertIn("未确认、未应用、未触发构建", adopted["msg"], adopted)

    def test_patch_conflict_disables_adopt(self):
        by_step = self._run_harness()
        conflict = by_step["patch-conflict"]
        self.assertEqual(len(conflict["posts"]), 2, conflict)
        self.assertTrue(conflict["adoptDisabled"], conflict)
        self.assertIn("采纳冲突（409）", conflict["conflict"], conflict)
        self.assertIn("conflicts: it-a", conflict["conflict"], conflict)
        self.assertIn("采纳按钮已禁用", conflict["conflict"], conflict)

    def test_clarification_option_sends_a_message_not_an_operation(self):
        by_step = self._run_harness()
        clarify = by_step["clarify-answer"]
        self.assertFalse(clarify["hidden"], clarify)
        self.assertEqual(clarify["newPosts"], 0, clarify)
        self.assertEqual(clarify["planPosts"], 0, clarify)
        self.assertIn("澄清回答（讨论消息）", clarify["target"], clarify)
        self.assertEqual(clarify["input"], "是（回答：第 0 行是否是表头？）",
                         clarify)

    def test_text_only_toggle_disables_image_scopes(self):
        by_step = self._run_harness()
        text_only = by_step["text-only"]
        self.assertEqual(len(text_only["posts"]), 1, text_only)
        scope = text_only["posts"][0]["confirmation"]["scope"]
        self.assertIs(scope["include_crop"], False)
        self.assertIs(scope["include_original_page"], False)
        self.assertIs(scope["include_adjacent_pages"], False)
        self.assertIs(scope["include_native"], True)
        self.assertIs(scope["include_ocr"], True)
        self.assertIn("crop ×", text_only["scope"], text_only)

    def test_no_vlm_degrades_with_chinese_reason_and_rules_still_work(self):
        by_step = self._run_harness()
        opened = by_step["novlm-open"]
        self.assertEqual(opened["vlmPosts"], 0, opened)
        bar = opened["bar"]
        self.assertIn("VLM 未配置", bar, opened)
        self.assertIn("disabled", bar, opened)
        self.assertIn("no enabled answer link", bar, opened)
        self.assertIn("规则报告仍可用", bar, opened)
        blocked = by_step["novlm-generate-blocked"]
        self.assertTrue(blocked["hidden"], blocked)
        self.assertEqual(blocked["vlmPosts"], 0, blocked)
        self.assertIn("规则报告仍可用", blocked["msg"], blocked)
        rules = by_step["novlm-rules-report"]
        self.assertEqual(len(rules["posts"]), 1, rules)
        self.assertEqual(rules["posts"][0], {}, rules)
        self.assertEqual(rules["vlmPosts"], 1, rules)
        self.assertIn("报告已生成", rules["msg"], rules)

    def test_reattach_and_explicit_interrupted_retry(self):
        by_step = self._run_harness()
        reattach = by_step["reattach-open"]
        self.assertEqual(reattach["vlmPosts"], 0, reattach)
        job_panel = reattach["vlmJob"]
        self.assertIn("VLM 后台任务", job_panel, reattach)
        self.assertIn("report-old", job_panel, reattach)
        self.assertIn("discuss-old", job_panel, reattach)
        self.assertIn("已中断", job_panel, reattach)
        self.assertIn("已完成", job_panel, reattach)
        self.assertIn("手动重试", job_panel, reattach)
        retry = by_step["reattach-retry"]
        self.assertFalse(retry["hidden"], retry)
        self.assertEqual(retry["vlmPosts"], 0, retry)
        self.assertIn("确认生成/更新 VLM 报告？", retry["title"], retry)

    def test_source_choice_and_vlm_disagrees_confirm_selected(self):
        """CP5.1 H5: ambiguous/vlm_disagrees items render a source choice and
        the confirm-selected request carries the ``sources`` map."""
        by_step = self._run_harness()
        opened = by_step["source-open"]
        self.assertEqual(opened["planPosts"], 0, opened)
        detail = opened["detail"]
        self.assertIn("规则建议与 VLM 建议不一致", detail, opened)
        self.assertIn("vlm_disagrees", detail, opened)
        self.assertIn('data-r="accept-source"', detail, opened)
        self.assertIn("采纳来源", detail, opened)
        self.assertIn("规则", detail, opened)
        self.assertIn("VLM", detail, opened)
        choice = opened["choice"]
        self.assertTrue(choice["needsChoice"], opened)
        self.assertTrue(choice["ambiguous"], opened)
        self.assertTrue(choice["disagrees"], opened)
        self.assertEqual(choice["sources"], ["rule", "vlm"], opened)
        self.assertEqual(choice["defaultSource"], "rule", opened)
        accepted = by_step["source-accept"]
        self.assertEqual(len(accepted["posts"]), 1, accepted)
        body = accepted["posts"][0]
        self.assertEqual(body["action"], "accept", body)
        self.assertEqual(body["item_ids"], ["it-a"], body)
        self.assertEqual(body["sources"], {"it-a": "vlm"}, body)
        self.assertEqual(accepted["planPosts"], 1, accepted)


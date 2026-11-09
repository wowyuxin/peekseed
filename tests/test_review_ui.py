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
                "item_ids:ids,provider:{}",
                "function reviewOcrRefreshRowHtml",
                "function reviewSyncOcrRefreshButton",
                "function reviewLoadOcrCounts",
                "function reviewOcrDoneText", "已刷新 ", "缓存命中 ",
                "失败 ", "低置信 ", "OCR 已刷新",
                "function reviewRetryJob", "type==='ocr'",
                "OCR 任务中断，可手动重试（不会自动重跑）"):
            self.assertIn(marker, text)
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
        self.assertEqual(body["provider"], {}, body)
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
        # [检查 OCR] posts provider {} and shows availability + probe seconds.
        checked = by_step["check"]
        self.assertEqual(checked["checks"], 1, checked)
        self.assertEqual(json.loads(checked["checkBody"]), {"provider": {}},
                         checked)
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
        self.assertEqual(body["provider"], {}, body)

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

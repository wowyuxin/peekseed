"""M3 CP0.5: Settings-UI contract tests for the decision link.

Static checks assert the served ``index.html`` carries the decision role end
to end (role option, typesafe protocol, pinned-model validation, the
no-implicit-probe status line, the model-alias warning, the separately
confirmed image probe). A Node stub-DOM harness then drives the **real**
settings JS block: protocol options, chunk visibility, form validation, the
decision status line, and the Test action's body for confirm true/false.
No browser and no network are needed.
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


class DecisionSettingsStaticTests(unittest.TestCase):
    def test_the_role_select_lists_decision(self):
        ui = _ui()
        self.assertIn('<option value="decision">Decision (auto-review)</option>',
                      ui)

    def test_the_settings_js_knows_the_decision_role(self):
        ui = _ui()
        self.assertIn("SET_ROLE_ORDER=['embed','rerank','answer','decision']",
                      ui)
        self.assertIn("decision:'Decision (auto-review)'", ui)
        self.assertIn("decision:[{v:'typesafe',l:'TypeSafe System One "
                      "(/v1/systemone)'}]", ui)

    def test_the_form_requires_a_pinned_model_for_decision(self):
        ui = _ui()
        self.assertIn("if(role==='decision'&&!p.model)", ui)
        self.assertIn("A decision link pins one model version", ui)

    def test_status_line_never_probes_decision_providers(self):
        ui = _ui()
        self.assertIn("if(status.decision){", ui)
        self.assertIn("use Test (synthetic probe)", ui)
        # The row copy says the quiet part out loud.
        self.assertIn("synthetic Test only — status never calls it", ui)

    def test_the_alias_warning_and_the_image_probe_confirm(self):
        ui = _ui()
        self.assertIn("if(link.model_warning)", ui)
        self.assertIn("window.confirm('Also send the experimental image "
                      "probe", ui)

    def test_the_chunk_field_stays_embed_only(self):
        ui = _ui()
        self.assertIn("role==='embed'?'':'none'", ui)


NODE_SETTINGS_HARNESS = r"""
/* Drive the real settings JS block with a stub DOM. No browser, no network:
 * callJSON is stubbed and records requests. */
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
// The marker also opens the CSS block near the top; the JS block is the
// second occurrence (the review harness uses the same skip-the-CSS trick).
const start = html.indexOf("/* ---- model-link settings", 200000);
const end = html.indexOf("/* ---- datasets dialog", start);
if (start < 0 || end < 0) throw new Error("settings block markers not found");
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
    reset() {},
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
function callJSON(url, opts) {
  calls.push({ url: String(url), method: String((opts || {}).method || "GET"),
               contentType: (opts || {}).headers
                 ? (opts || {}).headers["Content-Type"] : undefined,
               body: (opts || {}).body });
  return Promise.resolve({ ok: true, detail: "decision probe ok (jev-1.14.0)",
                           resolved_model: "jev-1.14.0", model_match: true,
                           usage: { input_tokens: 1, output_tokens: 1 },
                           latency_ms: 2, probe_version: 1, images: true });
}
function getJSON() { return Promise.reject(new Error("network in harness")); }
function $(selector) { return element(selector); }
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escAttr(value) { return esc(value); }
function statusOk(st) { return !!(st && st.ok === true); }
function toast() {}
function loadLinks() { return Promise.resolve(); }
function refreshAskStatus() {}
const window = {
  addEventListener() {}, removeEventListener() {},
  confirm() { return false; },
  location: { hash: "", origin: "http://127.0.0.1:0", href: "" },
  history: { replaceState() {}, pushState() {} },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  sessionStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
};

const sandbox = {
  document, window, $, esc, escAttr, callJSON, getJSON, toast, statusOk,
  loadLinks, refreshAskStatus, LINKS: [], LINK_STATUS: {},
  console, setTimeout: function () {}, clearTimeout() {},
  setInterval: function () {}, clearInterval() {},
  Promise, Number, String, JSON, Math, Date, Error, RegExp, Array, Object,
  URL, encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  history: window.history,
  localStorage: window.localStorage, sessionStorage: window.sessionStorage,
  alert() {}, confirm() { return false; }, prompt() { return ""; },
  addEventListener() {}, removeEventListener() {},
};
sandbox.globalThis = sandbox;

const code = block
  + "\n;globalThis.__s = {setProtocolOptions, setChunkVisible, setFormProblem,"
  + " setStatusLine, settingsAction, settingsRow, paintSettings};";
vm.runInContext(code, vm.createContext(sandbox),
                { filename: "index.html#settings-block" });
const S = sandbox.__s;

function tick() { return new Promise((resolve) => setTimeout(resolve, 5)); }

async function main() {
  const results = {};
  // 1. The decision role offers exactly the typesafe protocol.
  S.setProtocolOptions("decision", null);
  const protocol = element("#setProtocol");
  results.protocolOptions = {
    count: (protocol.innerHTML.match(/<option/g) || []).length,
    hasTypesafe: protocol.innerHTML.indexOf('value="typesafe"') >= 0,
  };
  // 2. The chunk field hides for decision links.
  S.setChunkVisible("decision");
  results.chunkHidden = element("#setChunkField").style.display === "none";
  S.setChunkVisible("embed");
  results.chunkShownForEmbed = element("#setChunkField").style.display === "";
  // 3. Form validation demands a pinned model for decision.
  const built = { role: "decision", protocol: "typesafe",
                  payload: { label: "D", base_url: "http://127.0.0.1:1",
                             model: "" } };
  results.missingModelProblem = S.setFormProblem(built);
  built.payload.model = "jev-1.14.0";
  results.modelOkProblem = S.setFormProblem(built);
  // 4. The decision status line never claims reachable/unreachable.
  const statusHtml = S.setStatusLine(
    { decision: true, configured: true, key_set: true,
      detail: "not probed (use the synthetic Test)" });
  results.statusLine = {
    isDecision: statusHtml.indexOf("decision link") >= 0,
    noUnreachable: statusHtml.indexOf("unreachable") < 0,
    pointsAtTest: statusHtml.indexOf("use Test") >= 0,
  };
  // 5. Row rendering surfaces the alias warning and the quiet-status copy.
  const decisionLink = { id: "d1", role: "decision", label: "D",
                         protocol: "typesafe", base_url: "http://127.0.0.1:1",
                         model: "jev-latest", model_warning: "moving alias",
                         enabled: true, chunk: null, api_key_set: false,
                         api_key_env: "" };
  const row = S.settingsRow(decisionLink, null);
  results.row = {
    badge: row.indexOf("Decision (auto-review)") >= 0,
    warning: row.indexOf("moving alias") >= 0,
    quietStatus: row.indexOf("synthetic Test only") >= 0,
    typesafeBadge: row.indexOf("typesafe") >= 0,
  };
  // 6. Test action: decision + confirm(false) -> no body; confirm(true) ->
  //    {"images":true}; other roles -> no confirm, no body.
  sandbox.LINKS = [decisionLink,
                   { id: "a1", role: "answer", label: "A", protocol: "openai",
                     base_url: "http://127.0.0.1:1", model: "m",
                     enabled: true }];
  let confirms = 0;
  window.confirm = () => { confirms += 1; return false; };
  await S.settingsAction("d1", "test");
  const textCall = calls[calls.length - 1];
  const confirmsAfterText = confirms;
  window.confirm = () => { confirms += 1; return true; };
  await S.settingsAction("d1", "test");
  const imageCall = calls[calls.length - 1];
  window.confirm = () => { confirms += 1; return true; };
  await S.settingsAction("a1", "test");
  const answerCall = calls[calls.length - 1];
  results.testAction = {
    textBody: textCall.body === undefined && confirmsAfterText === 1,
    imageBody: imageCall.body === JSON.stringify({ images: true }),
    answerNoConfirmNoBody: answerCall.body === undefined,
    confirmsTotal: confirms,
  };
  console.log(JSON.stringify(results));
}

main().catch((error) => {
  console.error((error && error.stack) || error);
  process.exit(1);
});
"""


@unittest.skipUnless(shutil.which("node"),
                     "node is needed for the DOM behavior harness")
class DecisionSettingsBehaviorTests(unittest.TestCase):
    def test_the_real_settings_block_handles_decision_links(self):
        base = Path(tempfile.mkdtemp(prefix="peekseed-decision-ui-"))
        try:
            harness = base / "settings_harness.js"
            harness.write_text(NODE_SETTINGS_HARNESS, encoding="utf-8")
            run = subprocess.run(
                [shutil.which("node"), str(harness), str(UI_PATH)],
                capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            results = json.loads(run.stdout.decode().strip().splitlines()[-1])
            self.assertEqual(results["protocolOptions"],
                             {"count": 1, "hasTypesafe": True})
            self.assertTrue(results["chunkHidden"])
            self.assertTrue(results["chunkShownForEmbed"])
            self.assertIn("pins one model version",
                          results["missingModelProblem"])
            self.assertEqual(results["modelOkProblem"], "")
            self.assertEqual(results["statusLine"],
                             {"isDecision": True, "noUnreachable": True,
                              "pointsAtTest": True})
            self.assertEqual(results["row"],
                             {"badge": True, "warning": True,
                              "quietStatus": True, "typesafeBadge": True})
            self.assertEqual(results["testAction"],
                             {"textBody": True, "imageBody": True,
                              "answerNoConfirmNoBody": True,
                              # decision asks once per test; the answer link
                              # never prompts (short-circuit, not skipped).
                              "confirmsTotal": 2})
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

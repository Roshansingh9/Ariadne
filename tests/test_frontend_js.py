"""Unit tests for pure helpers in public/app.js, executed in Node with a stub DOM.

Skipped when Node is not installed. Browser behaviour is covered by the
Playwright run documented in the README; these keep the core helpers honest in CI.
"""

import json
import shutil
import subprocess

import pytest

from tests.conftest import ROOT

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const elements = {};
const el = (id) => (elements[id] ||= { id, value: "", hidden: false, disabled: false, textContent: "",
  innerHTML: "", title: "", dataset: {}, classList: { toggle() {}, add() {}, remove() {} },
  addEventListener() {}, focus() {}, setAttribute() {} });
const document = { querySelector: (s) => el(s.replace(/^#/, "")), querySelectorAll: () => [],
  addEventListener() {}, documentElement: {}, body: { addEventListener() {} } };
const ctx = { document, window: {}, location: { hash: "", pathname: "/" }, history: { replaceState() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }), setTimeout, clearTimeout, console,
  fetch: async () => { throw new TypeError("Failed to fetch"); } };
vm.createContext(ctx);
vm.runInContext(src + `
;globalThis.__t = { h, errorDetail, impactDepth, nextSeq, isLatest, api, statusHtml, updateActions, state, renderAnswer };`, ctx);
const t = ctx.__t, out = {};
out.h = t.h(`<img src=x onerror="a">&'`);
out.detailStr = t.errorDetail({ detail: "Repository 'x' is not indexed." }, 404);
out.detail422 = t.errorDetail({ detail: [{ loc: ["query", "depth"], msg: "Input should be a valid integer" }] }, 422);
out.detailNone = t.errorDetail(null, 502);
const depths = {};
for (const v of ["", "abc", "0", "-3", "4", "99", "2.7"]) { el("impact-depth").value = v; depths[v] = [t.impactDepth(), el("impact-depth").value]; }
out.depths = depths;
const a = t.nextSeq("k"), b = t.nextSeq("k");
out.seq = [t.isLatest("k", a), t.isLatest("k", b)];
out.status = t.statusHtml("error", "<b>boom</b>", "retry-ask");
t.state.repo = null; t.state.current = null; t.state.writesEnabled = false; t.updateActions();
out.noRepo = { ask: el("ask-btn").disabled, impact: el("impact-symbol-btn").disabled, scenario: el("impact-btn").disabled, del: el("repo-delete").disabled,
               index: el("index-btn").disabled, indexTitle: el("index-btn").title };
t.state.repo = "r"; t.state.current = "u"; t.state.currentNode = null; t.updateActions();
out.loading = { impact: el("impact-symbol-btn").disabled, title: el("impact-symbol-btn").title };
t.state.currentNode = { uid: "u" }; t.state.writesEnabled = true; t.state.busy.ask = true; t.updateActions();
el("impact-scenario").value = "If I change x"; t.updateActions();
out.withRepo = { ask: el("ask-btn").disabled, askText: el("ask-btn").textContent, impact: el("impact-symbol-btn").disabled,
                 scenario: el("impact-btn").disabled,
                 del: el("repo-delete").disabled, index: el("index-btn").disabled };
const ev = (id, kind) => ({ id, kind, title: "t" + id, facts: ["f"], uid: "u" + id, location: "a.py:1", snippet: null });
out.answers = {
  noMatch: t.renderAnswer({ mode: "no_match", answer: "No symbol matching `order` was found.", citations: [], evidence: [],
    complete: true, grounded: false, unmatched: ["order"],
    suggestions: [{ uid: "x.py::reorder<b>", name: "reorder<b>", qualname: "reorder<b>", label: "Function", path: "x.py", line_start: 3 }] }),
  incomplete: t.renderAnswer({ mode: "model", model: "m", answer: "**Answer** partial [E1]", citations: ["E1"],
    evidence: [ev("E1", "definition"), ev("E2", "callers")], complete: false, grounded: true, truncated: true }),
  uncited: t.renderAnswer({ mode: "model", model: "m", answer: "It is fine.", citations: [], evidence: [ev("E1", "definition")],
    complete: true, grounded: false, unsupported_citations: [] }),
  weak: t.renderAnswer({ mode: "model", model: "m", answer: "ok [E1]", citations: ["E1"], evidence: [ev("E1", "definition")],
    complete: true, grounded: true, unsupported_citations: ["E1"], unmatched: ["GhostThing"] }),
};
t.api("/api/health").then(() => { out.network = "resolved?!"; }, (e) => { out.network = e.message; })
  .then(() => console.log(JSON.stringify(out)));
"""


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    harness = tmp_path_factory.mktemp("js") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(harness), str(ROOT / "public" / "app.js")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_escaping(js):
    assert js["h"] == "&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;"
    assert "<b>" not in js["status"] and "&lt;b&gt;boom" in js["status"]
    assert 'data-action="retry-ask"' in js["status"]


def test_error_detail_formats_fastapi_errors(js):
    assert js["detailStr"] == "Repository 'x' is not indexed."
    assert js["detail422"] == "depth: Input should be a valid integer"      # not a raw JSON array
    assert js["detailNone"] == "HTTP 502"


def test_network_failure_is_explained(js):
    assert js["network"] == "Network error: the Ariadne server could not be reached."


def test_impact_depth_is_clamped_and_written_back(js):
    assert js["depths"] == {"": [3, 3], "abc": [3, 3], "0": [1, 1], "-3": [1, 1], "4": [4, 4],
                            "99": [6, 6], "2.7": [2, 2]}


def test_latest_request_wins(js):
    assert js["seq"] == [False, True]


def test_impact_waits_for_the_selected_symbol_to_load(js):
    assert js["loading"] == {"impact": True, "title": "Loading the selected symbol…"}   # symbol mode waits for the load


def test_action_states(js):
    assert js["noRepo"] == {"ask": True, "impact": True, "scenario": True, "del": True, "index": True,
                            "indexTitle": "Indexing is disabled: the server has no ARIADNE_INDEX_TOKEN configured."}
    assert js["withRepo"] == {"ask": True, "askText": "Asking…", "impact": False, "scenario": False, "del": False, "index": False}


def test_no_match_answer_shows_suggestions_not_an_answer(js):
    a = js["answers"]["noMatch"]
    assert "no matching symbol" in a and "Close matches in the graph" in a
    assert 'data-uid="x.py::reorder&lt;b&gt;"' in a and "<b>" not in a.split("Close matches")[1]   # escaped
    assert "Evidence (" not in a


def test_truncated_answer_is_visibly_incomplete(js):
    a = js["answers"]["incomplete"]
    assert a.index("Incomplete answer") < a.index('class="answer incomplete"')       # banner above the text
    assert 'data-action="retry-ask"' in a
    assert 'id="ev-E2" class="uncited"' in a and "not cited" in a and 'id="ev-E1" class=""' in a


def test_uncited_and_weakly_cited_answers_are_flagged(js):
    assert "cited no graph evidence" in js["answers"]["uncited"]
    weak = js["answers"]["weak"]
    assert "Check these citations: E1" in weak and "Not found in the graph: <code>GhostThing</code>" in weak

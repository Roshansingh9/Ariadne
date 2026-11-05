"""Static checks that the UI and API agree (no browser required)."""

import re
import shutil
import subprocess

import pytest

from ariadne.api import app
from tests.conftest import ROOT

PUBLIC = ROOT / "public"


def test_every_dom_id_used_by_app_js_exists():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    js = (PUBLIC / "app.js").read_text(encoding="utf-8")
    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)', js))
    missing = used - ids_in_html
    assert not missing, missing


def test_every_api_path_used_by_ui_exists():
    js = (PUBLIC / "app.js").read_text(encoding="utf-8")
    routes = {r.path for r in app.routes}
    suffixes = set(re.findall(r'repoPath\(`?"?(/[a-z]+)', js))
    for s in suffixes:
        assert f"/api/repos/{{repo_id}}{s}" in routes, s
    for p in ("/api/health", "/api/repos", "/api/index"):
        assert p in js and p in routes


def test_ui_escapes_server_text():
    js = (PUBLIC / "app.js").read_text(encoding="utf-8")
    assert "function h(" in js and "&lt;" in js
    # Raw unescaped interpolation of common server fields would be an XSS risk.
    assert not re.search(r"\$\{(n|e|x|r)\.(name|path|title|message|qualname)\}", js)


def test_assets_referenced():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    for asset in ("/app.js", "/styles.css", "/favicon.svg"):
        assert asset in html and (PUBLIC / asset.lstrip("/")).is_file()
    assert "cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/" in html


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_app_js_syntax():
    subprocess.run(["node", "--check", str(PUBLIC / "app.js")], check=True)


def _js():
    return (PUBLIC / "app.js").read_text(encoding="utf-8")


def _fn_body(js, name):
    m = re.search(r"(?:async )?function " + name + r"\([^)]*\) \{", js)
    assert m, name
    i, depth = m.end(), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(js[i], 0)
        i += 1
    return js[m.end():i]


def test_selecting_a_repo_loads_the_repository_graph():
    # Root cause of the blank canvas: selectRepo only fetched /overview, so no graph request was made.
    js = _js()
    assert "showOverviewGraph()" in _fn_body(js, "selectRepo")
    assert 'repoPath(`/graph?' in _fn_body(js, "loadGraph")


def test_graph_controls_are_wired_to_real_behaviour():
    js = _js()
    fit = _fn_body(js, "fitGraph")
    assert "cy.resize()" in fit and "fit" in fit and ':visible' in fit
    assert '"#fit-btn").addEventListener("click", () => fitGraph(' in js
    assert '"#rel-filters").addEventListener("change", () => loadGraph())' in js
    assert '"#direction").addEventListener("change"' in js
    load = _fn_body(js, "loadGraph")
    assert "direction=${dir}" in load and "rels=${rels.join" in load
    assert "seq !== graph.seq" in load                       # stale responses are dropped
    assert 'graphStatus("loading"' in load and 'graphStatus("empty"' in load and 'graphStatus("error"' in load


def test_cytoscape_renders_into_dedicated_canvas():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    assert 'id="graph-canvas"' in html and 'id="graph-status"' in html
    assert 'container: $("#graph-canvas")' in _js()


def test_every_data_action_has_a_handler():
    js = _js()
    used = set(re.findall(r'data-action="([a-z-]+)"', js)) | set(re.findall(r'statusHtml\([^)]*"(retry-[a-z-]+)"\)', js))
    handlers = set(re.findall(r'^\s+"([a-z-]+)": \(\) =>', js, re.M))
    assert used, "expected retry actions"
    assert used <= handlers, used - handlers


def test_every_button_in_html_is_wired():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    js = _js()
    for bid in re.findall(r'<button[^>]*id="([^"]+)"', html):
        if bid.startswith("tab-"):               # tabs share one listener on ".tabs button"
            continue
        assert f'"#{bid}"' in js, f"button #{bid} has no handler"
    assert '$$(".tabs button").forEach((b) => b.addEventListener("click"' in js
    for fid in re.findall(r'<form[^>]*id="([^"]+)"', html):
        assert f'$("#{fid}").addEventListener("submit"' in js, fid


def test_duplicate_submission_guards():
    js = _js()
    for fn, flag in (("ask", "state.busy.ask"), ("runImpact", "state.busy.impact"),
                     ("indexRepo", "state.busy.index"), ("confirmDelete", "state.busy.delete")):
        assert f"if ({flag}) return" in _fn_body(js, fn) or f"|| {flag}) return" in _fn_body(js, fn), fn


def test_destructive_delete_needs_confirmation_dialog():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    js = _js()
    assert '<dialog id="confirm-dialog"' in html
    assert '"#repo-delete").addEventListener("click", openDeleteDialog)' in js
    assert 'method: "DELETE"' in _fn_body(js, "confirmDelete")
    assert 'method: "DELETE"' not in _fn_body(js, "openDeleteDialog")
    assert '$("#confirm-cancel").focus()' in _fn_body(js, "openDeleteDialog")      # safe default


def test_tabs_are_accessible():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'<button role="tab"[^>]*>', html)
    assert len(tabs) == 6 and all("aria-controls" in t and "aria-selected" in t for t in tabs)
    for t in tabs:
        pane = re.search(r'aria-controls="([^"]+)"', t).group(1)
        assert f'id="{pane}" role="tabpanel"' in html
    assert "ArrowRight" in _fn_body(_js(), "tabKeys")


def test_clickable_rows_are_keyboard_focusable():
    js = _js()
    assert 'tabindex="0" role="button"' in _fn_body(js, "resultItem")
    assert 'tabindex="0" role="button"' in _fn_body(js, "renderOverview")
    assert 'tabindex="0" role="button"' in _fn_body(js, "renderMarkdown")


# --- Regression: blank graph canvas (zero-height container) ----------------------------------

def test_graph_canvas_is_sized_inline_not_only_by_stylesheet():
    # With only the stylesheet sizing it, a stale/blocked styles.css gave a 0px-tall canvas:
    # the caption reported "27 nodes · 45 edges" while nothing was painted.
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    tag = re.search(r'<div id="graph-canvas"[^>]*>', html).group(0)
    assert "position:absolute" in tag and "inset:0" in tag
    js = _js()
    assert "ensureCanvasSize();" in _fn_body(js, "drawGraph")
    assert "has no visible size" in _fn_body(js, "drawGraph")       # never a silent blank canvas
    assert 'ro.observe($("#graph-canvas"))' in js


def test_asset_urls_carry_current_content_hashes():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stamp_assets", ROOT / "scripts" / "stamp_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    assert mod.stamped(html) == html, "run: python scripts/stamp_assets.py"
    for name in ("app.js", "styles.css"):
        assert f"/{name}?v={mod.asset_hash(name)}" in html


def test_page_and_assets_are_revalidated_not_heuristically_cached():
    from fastapi.testclient import TestClient
    c = TestClient(app)
    for path in ("/", "/app.js", "/styles.css", "/app.js?v=anything"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
        assert r.headers.get("etag"), path                        # revalidation stays a cheap 304


def test_path_early_returns_invalidate_in_flight_requests():
    # Regression: a slow earlier /path response overwrote "Tick at least one relationship type…".
    body = _fn_body(_js(), "runPath")
    assert body.index('nextSeq("path")') < body.index("if (!rels.length)") < body.index("await api(")


# --- Graph readability (layout + labels) ------------------------------------------------------

def test_layout_accounts_for_labels_and_spreads_components():
    # Root cause of the cluttered graph: cose ran with its defaults (labels ignored, gravity 1,
    # componentSpacing 40), so nodes were laid out as bare dots and labels piled up.
    body = _fn_body(_js(), "layoutOptions")
    for opt in ("nodeDimensionsIncludeLabels: p.labels", "randomize: false", "gravity: p.gravity",
                "componentSpacing: p.components", "nodeRepulsion: () => 40000", 'e.data("type") === "DEFINES"'):
        assert opt in body, opt
    draw = _fn_body(_js(), "drawGraph")
    assert 'layout: { name: "preset", fit: false }' in draw and "runLayout(layout, centerUid)" in draw
    assert "position: seeds.get(n.uid)" in draw                         # deterministic layout


def test_fit_runs_after_layout_and_labels_are_replaced_after_fit_zoom_and_resize():
    js = _js()
    assert 'lay.one("layoutstop", () => { if (state.cy === cy) fitGraph(false); });' in _fn_body(js, "runLayout")
    fit = _fn_body(js, "fitGraph")
    assert fit.count("placeLabels()") == 2                              # animated and immediate fit
    assert "placeLabels()" in _fn_body(js, "onGraphResize")
    assert 'state.cy.on("zoom", debounce(() => placeLabels(), 90))' in js
    assert "placeLabels();" in _fn_body(js, "applyClientFilter")


def test_labels_are_collision_checked_and_hidden_labels_are_announced():
    body = _fn_body(_js(), "placeLabels")
    assert "LABEL_PLACES" in body and "inside(box)" in body and "bodies.some" in body
    assert 'toggleClass("nolabel"' in body                              # labels hide; nodes/edges never do
    assert "labels shown" in body and "No nodes or edges are hidden" in body
    assert "0.9 * nearest[i]" in body                                   # enlarging nodes never creates overlaps
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    assert 'id="graph-labels"' in html and 'id="graph-tip"' in html
    # the note sits in the fixed-height caption row, outside the canvas
    row = re.search(r'<div class="graph-caption-row[^"]*">(.*?)</div>\s*</div>', html, re.S).group(1)
    assert 'id="graph-labels"' in row


def test_full_names_on_hover_and_edge_types_only_when_highlighted():
    js = _js()
    draw = _fn_body(js, "drawGraph")
    assert "full: displayName(n)" in draw and 'state.cy.on("mouseover", "node"' in draw
    assert '$("#graph-canvas").onmouseleave = () => highlight(null);' in draw   # no stuck hover
    assert 'label: "", "font-size": 9' in draw and '{ selector: "edge.hl", style: { label: "data(type)"' in draw
    assert "name.textContent = node.data(\"full\")" in _fn_body(js, "showTip")   # textContent: never HTML


# --- Impact panel wiring --------------------------------------------------------------------

def test_impact_respects_graph_filters_and_depth():
    body = _fn_body(_js(), "runImpact")
    assert 'selectedRels().filter((x) => IMPACT_RELS.includes(x))' in body
    assert "&rels=${rels.join(\",\")}" in body and "depth=${depth}" in body
    assert body.index("if (!rels.length)") < body.index("await api(")          # no request without relationships
    assert "Tick at least one of calls, imports, extends or implements" in body
    assert "renderImpact(r)" in body and 'statusHtml("error", e.message, "retry-impact")' in body


def test_impact_report_sections_and_escaping():
    body = _fn_body(_js(), "renderImpact")
    for title in ("Dependency paths", "Architecture &amp; layers", "User flows / UI impact", "Potentially affected behaviour",
                  "Affected files", "Explanation", "Suggested inspection", "Evidence &amp; coverage"):
        assert title in body, title
    assert "renderInline(" in body and "function renderInline(text) { return h(text)" in _js()
    assert 'if (item.closest("summary")) e.preventDefault();' in _js()          # chip in a group header opens, doesn't toggle


def test_impact_tab_has_selected_symbol_and_picker():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    pane = html.split('data-pane="impact"')[1].split("</section>")[0]
    for elem in ('id="impact-selected"', 'id="impact-picker"', 'id="impact-search"', 'id="impact-results"',
                 'id="impact-depth"', 'id="impact-btn"'):
        assert elem in pane, elem
    js = _js()
    assert "/search?q=${encodeURIComponent(q)}" in _fn_body(js, "renderImpactSelection") or \
        'repoPath(`/search?q=${encodeURIComponent(q)}&limit=12`)' in js          # real indexed search only
    pick = _fn_body(js, "pickForImpact")
    assert 'openNode(uid, { tab: "impact" })' in pick                          # same path as a graph click
    node = _fn_body(js, "openNode")
    assert "state.currentNode = { ...d.node, uid };" in node and node.count("renderImpactSelection(") >= 2
    assert 'const impactPick = e.target.closest("#impact-results li[data-uid]");' in js
    assert "state.currentNode = null;" in _fn_body(js, "selectRepo")


def test_impact_needs_a_loaded_selection_and_handles_legacy_servers():
    js = _js()
    run = _fn_body(js, "runImpact")
    assert run.index("state.currentNode.uid !== state.current") < run.index("await api(")
    assert "r.report ? renderImpact(r) : renderLegacyImpact(r)" in run


# --- Impact: natural-language scenario is the primary workflow -------------------------------

def test_scenario_is_the_primary_impact_input():
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    pane = html.split('data-pane="impact"')[1].split('data-pane="path"')[0]
    assert pane.index('id="impact-scenario"') < pane.index('id="impact-btn"') < pane.index('id="impact-symbol-mode"')
    assert 'placeholder="Describe the change you’re considering…"' in pane
    assert '<details class="imp-symbol" id="impact-symbol-mode">' in pane               # symbol picker is secondary
    assert 'id="impact-examples"' in pane and 'id="impact-repo"' in pane and 'id="impact-advanced"' in pane
    assert "Select a symbol first" not in pane and "select a symbol first" not in _js().lower()


def test_scenario_flow_wiring():
    js = _js()
    run = _fn_body(js, "runScenario")
    assert 'repoPath("/impact/scenario"), { method: "POST"' in run and "...(targets ? { targets } : {})" in run
    assert "if (state.busy.scenario) return;" in run and 'nextSeq("scenario")' in run and '"retry-scenario"' in run
    assert 'selectedRels().filter((x) => IMPACT_RELS.includes(x))' in run and "use_model: useModel" in run
    assert '$("#impact-btn").addEventListener("click", () => runScenario());' in js
    assert 'if (e.target.closest("#impact-reanalyze")) { const t = checkedTargets(); if (t.length) runScenario(t); return; }' in js
    assert '$("#impact-scenario").value = ex.dataset.example;' in js                    # example chips fill the input
    assert '$("#impact").innerHTML' not in _fn_body(js, "openNode")                     # opening a symbol keeps the report

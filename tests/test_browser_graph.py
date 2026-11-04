"""Browser regression test for the blank graph canvas (opt-in).

    ARIADNE_BROWSER_TESTS=1 python -m pytest -q tests/test_browser_graph.py

Runs the real app and the real public/ assets in headless Chromium against an
in-memory fixture graph (no FalkorDB, nothing written anywhere). It asserts on
*painted canvas pixels*, because the original bug had Cytoscape holding every
node while a 0px-tall canvas showed nothing. Needs Playwright with Chromium and
network access to cdnjs (Cytoscape is loaded from the CDN, as in production).
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ARIADNE_BROWSER_TESTS") != "1",
                                reason="set ARIADNE_BROWSER_TESTS=1 to run browser tests")

REPOS = ("github__acme__shop", "github__acme__shop2")
PAINTED = """() => {
  let painted = 0;
  for (const c of document.querySelectorAll('#graph-canvas canvas')) {
    if (!c.width || !c.height) continue;
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    for (let i = 3; i < d.length; i += 16) if (d[i] > 0) painted++;
  }
  const cy = state.cy;
  return { painted, nodes: cy ? cy.nodes(':visible').length : 0, edges: cy ? cy.edges(':visible').length : 0,
           h: cy ? cy.height() : 0, status: document.querySelector('#graph-status').hidden ? '' :
           document.querySelector('#graph-status').innerText };
}"""
IN_VIEW = """() => { const cy = state.cy, bb = cy.elements(':visible').renderedBoundingBox();
  return bb.x1 >= -1 && bb.y1 >= -1 && bb.x2 <= cy.width() + 1 && bb.y2 <= cy.height() + 1; }"""


class _Store:
    """Just enough of FalkorStore for the UI: two repos, always reachable."""

    def ping(self):
        return True

    def list_repos(self):
        return [{"repo_id": r, "owner": "acme", "name": r.split("__")[-1], "status": "ready"} for r in REPOS]


@pytest.fixture(scope="module")
def base_url(fixture_graph_module):
    uvicorn = pytest.importorskip("uvicorn")
    from ariadne import api
    from ariadne.config import Settings
    from tests.memory_queries import MemoryQueries

    queries = {r: MemoryQueries(fixture_graph_module, r) for r in REPOS}
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(falkordb_host="memory")
    api.app.dependency_overrides[api.get_store] = lambda: _Store()
    api.app.dependency_overrides[api.repo_queries] = lambda repo_id: queries[repo_id]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)
    api.app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def fixture_graph_module():
    from ariadne.indexer import build_graph
    from ariadne.ingest.fetch import Limits, walk_directory
    from tests.conftest import FIXTURE_DIR
    return build_graph(walk_directory(str(FIXTURE_DIR), Limits()))[0]


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _open(browser, base_url, viewport=None, stale_css=False):
    page = browser.new_page(viewport=viewport or {"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    if stale_css:
        from tests.conftest import ROOT
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        old = re.sub(r"\.graph-canvas \{[^}]*\}", "", css)          # the stylesheet before #graph-canvas existed
        page.route("**/styles.css*", lambda r: r.fulfill(status=200, content_type="text/css", body=old))
    page.goto(f"{base_url}/#{REPOS[0]}")
    _wait_drawn(page)
    return page, errors


def _wait_drawn(page, mode="overview"):
    page.wait_for_function(f"() => graph.mode === '{mode}' && state.cy && state.cy.nodes().length > 0 "
                           "&& document.querySelector('#graph-status').hidden", timeout=20000)
    page.wait_for_timeout(300)


def _assert_painted(page, label):
    r = page.evaluate(PAINTED)
    assert r["nodes"] > 0 and r["edges"] > 0, (label, r)
    assert r["h"] > 100 and r["painted"] > 200, (label, r)        # pixels actually on screen
    assert page.evaluate(IN_VIEW), (label, "graph not fitted in view")


def test_initial_load_paints_nodes_and_edges(browser, base_url):
    page, errors = _open(browser, base_url)
    _assert_painted(page, "initial load")
    assert not errors, errors
    page.close()


def test_stale_stylesheet_no_longer_blanks_the_canvas(browser, base_url):
    """The reported failure: fresh HTML/JS with a cached pre-#graph-canvas styles.css."""
    page, errors = _open(browser, base_url, stale_css=True)
    _assert_painted(page, "stale stylesheet")
    assert not errors, errors
    page.close()


def test_repo_switch_resize_and_filters_keep_painting(browser, base_url):
    page, errors = _open(browser, base_url)
    page.select_option("#repo-select", REPOS[1])
    page.wait_for_function(f"() => state.repo === '{REPOS[1]}'")
    _wait_drawn(page)
    _assert_painted(page, "after repo switch")
    for w, h in ((1100, 800), (760, 900), (1440, 900)):
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(500)
        _assert_painted(page, f"after resize to {w}px")
    before = page.evaluate("() => state.cy.edges().length")
    page.uncheck("#rel-filters input[value=DEFINES]")
    page.wait_for_timeout(400)
    _wait_drawn(page)
    _assert_painted(page, "after unticking defines")
    assert page.evaluate("() => state.cy.edges('[type = \"DEFINES\"]').length") == 0
    assert page.evaluate("() => state.cy.edges().length") < before
    page.check("#rel-filters input[value=DEFINES]")
    _wait_drawn(page)
    _assert_painted(page, "after re-ticking defines")
    assert not errors, errors
    page.close()


def test_canvas_hidden_at_load_is_drawn_once_visible(browser, base_url):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script("document.addEventListener('DOMContentLoaded', () => "
                         "{ document.querySelector('#graph').style.display = 'none'; });")
    page.goto(f"{base_url}/#{REPOS[0]}")
    page.wait_for_function("() => graph.mode === 'overview' && state.cy && state.cy.nodes().length > 0", timeout=20000)
    page.wait_for_timeout(300)
    assert "no visible size" in page.evaluate("() => document.querySelector('#graph-status').innerText")
    page.evaluate("() => { document.querySelector('#graph').style.display = ''; }")
    page.wait_for_function("() => document.querySelector('#graph-status').hidden", timeout=5000)
    page.wait_for_timeout(300)
    _assert_painted(page, "after the canvas became visible")
    page.close()


READABILITY = """() => {
  const cy = state.cy, z = cy.zoom();
  const shown = cy.nodes(':visible').filter(n => !n.hasClass('nolabel'));
  const lb = shown.map(n => n.boundingBox({ includeNodes: false, includeEdges: false, includeLabels: true }));
  const nb = cy.nodes(':visible').map(n => [n.id(), n.boundingBox({ includeLabels: false, includeEdges: false })]);
  const hit = (a, b) => a.x1 < b.x2 - 1 && b.x1 < a.x2 - 1 && a.y1 < b.y2 - 1 && b.y1 < a.y2 - 1;
  let ll = 0, ln = 0, nn = 0;
  for (let i = 0; i < lb.length; i++) for (let j = i + 1; j < lb.length; j++) if (hit(lb[i], lb[j])) ll++;
  shown.forEach((n, i) => nb.forEach(([id, b]) => { if (id !== n.id() && hit(lb[i], b)) ln++; }));
  const ns = cy.nodes(':visible').toArray();
  for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) {
    const a = ns[i].position(), b = ns[j].position();
    if (Math.hypot(a.x - b.x, a.y - b.y) < (parseFloat(ns[i].style('width')) + parseFloat(ns[j].style('width'))) / 2 - 0.5) nn++; }
  return { ll, ln, nn, shown: shown.length, nodes: ns.length, edges: cy.edges().length,
           fontPx: +(parseFloat(ns[0].style('font-size')) * z).toFixed(1),
           note: document.querySelector('#graph-labels').hidden ? '' : document.querySelector('#graph-labels').innerText };
}"""


def test_overview_is_readable_and_complete(browser, base_url, fixture_graph_module):
    """Regression for the cluttered layout: no label/label, label/node or node/node overlap;
    every node and edge still drawn; hidden labels announced; full name on hover."""
    page, errors = _open(browser, base_url, viewport={"width": 1366, "height": 768})
    r = page.evaluate(READABILITY)
    import json, urllib.request
    g = json.load(urllib.request.urlopen(f"{base_url}/api/repos/{REPOS[0]}/graph?rels=CALLS,IMPORTS,EXTENDS,IMPLEMENTS,DEFINES&limit=150"))
    assert (r["nodes"], r["edges"]) == (len(g["nodes"]), len(g["edges"])), r
    assert r["ll"] == 0 and r["ln"] == 0 and r["nn"] == 0, r
    assert r["fontPx"] == 11 and r["shown"] > 0, r
    assert (r["shown"] == r["nodes"] and r["note"] == "") or r["note"] == f"{r['shown']}/{r['nodes']} labels shown", r
    longest = page.evaluate("() => state.cy.nodes().sort((a, b) => b.data('full').length - a.data('full').length)[0].id()")
    full = page.evaluate("(id) => state.cy.$id(id).data('full')", longest)
    x, y = page.evaluate("(id) => { const p = state.cy.$id(id).renderedPosition(), b = document.querySelector('#graph-canvas').getBoundingClientRect(); return [b.left + p.x, b.top + p.y]; }", longest)
    page.mouse.move(x, y)
    page.wait_for_timeout(200)
    assert full in page.inner_text("#graph-tip")
    assert page.evaluate("() => state.cy.edges('.hl').every(e => e.style('label') !== '')")
    page.mouse.move(2, 2)                         # leave the canvas straight from the node
    page.wait_for_timeout(150)
    assert page.is_hidden("#graph-tip") and page.evaluate("() => state.cy.elements('.hl, .faded').length") == 0
    assert not errors, errors
    page.close()

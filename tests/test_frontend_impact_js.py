"""Impact panel rendering (public/app.js renderImpact) on real reports from the local fixtures.

Reports are produced by the production analysis over in-memory fixture graphs, then rendered
in Node with a stub DOM. Skipped when Node is not installed.
"""

import json
import re
import shutil
import subprocess

import pytest

from ariadne.indexer import build_graph
from ariadne.ingest.fetch import FetchResult, Limits, walk_directory
from tests.conftest import FIXTURE_DIR, FULLSTACK_DIR, ROOT
from tests.memory_queries import MemoryQueries

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const reports = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const el = () => ({ value: "", hidden: false, disabled: false, textContent: "", innerHTML: "", title: "", dataset: {},
  classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, focus() {}, setAttribute() {} });
const document = { querySelector: el, querySelectorAll: () => [], addEventListener() {}, documentElement: {},
                   body: { addEventListener() {} } };
const ctx = { document, window: {}, location: { hash: "", pathname: "/" }, history: { replaceState() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }), setTimeout, clearTimeout, console };
vm.createContext(ctx);
vm.runInContext(src + ";globalThis.__render = (r) => r.report ? renderImpact(r) : renderLegacyImpact(r);", ctx);
const out = {};
for (const [k, r] of Object.entries(reports)) out[k] = ctx.__render(r);
console.log(JSON.stringify(out));
"""

TO_CENTS = "backend/utils/money.py::to_cents"


@pytest.fixture(scope="module")
def html(tmp_path_factory):
    fetched = walk_directory(str(FULLSTACK_DIR), Limits())
    full = build_graph(fetched)[0]
    backend = build_graph(FetchResult(files=[f for f in fetched.files if f[0].startswith("backend/")], source="d"))[0]
    sample = build_graph(walk_directory(str(FIXTURE_DIR), Limits()))[0]
    reports = {
        "full": MemoryQueries(full, "r").impact(TO_CENTS, depth=6),
        "backend": MemoryQueries(backend, "r").impact(TO_CENTS, depth=6),
        "notest": MemoryQueries(sample, "r").impact("shop/utils.py::fmt", depth=3),
        "empty": MemoryQueries(full, "r").impact("backend/app.py::health", depth=3),
        "old": MemoryQueries(full, "r").as_old_graph().impact(TO_CENTS, depth=6),
    }
    reports["legacy"] = {k: v for k, v in reports["full"].items() if k != "report"}   # a pre-report server
    # an XSS probe in a symbol name must be escaped
    probe = "to_cents<img src=x onerror=alert(1)>"          # displayed name is qualname, then name
    reports["full"]["report"]["nodes"][TO_CENTS].update(name=probe, qualname=probe)
    d = tmp_path_factory.mktemp("impact")
    (d / "h.js").write_text(HARNESS, encoding="utf-8")
    (d / "r.json").write_text(json.dumps(reports), encoding="utf-8")
    run = subprocess.run(["node", str(d / "h.js"), str(ROOT / "public" / "app.js"), str(d / "r.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_report_sections_in_order(html):
    titles = [t.strip() for t in re.findall(r"<h3>([^<(]+)", html["full"])]
    assert titles == ["Summary", "Dependency paths", "Architecture &amp; layers", "User flows / UI impact",
                      "Affected files", "Suggested inspection", "Explanation &amp; evidence"]
    assert "Potentially affected layers: UI page, UI component" in html["full"]


def test_summary_counts_depth_and_coverage(html):
    a = html["full"]
    stats = re.findall(r'<div class="stat"><b>(\d+)</b><span>(\w+)</span>', a)
    assert stats == [("1", "direct"), ("3", "transitive"), ("4", "files"), ("6", "depth")]
    assert "<code>backend/utils/money.py:1-2</code>" in a
    assert "Relationships followed: <code>CALLS</code> <code>IMPORTS</code>" in a
    assert re.search(r"Coverage: graph version 2, 6 python, 3 typescript files indexed; \d+ unresolved call site\(s\) not followed\.", a)
    assert "not established: UI/API flow" in html["backend"]


def test_paths_are_full_chains_with_locations_and_clickable_symbols(html):
    a = html["full"]
    path = re.search(r'<span class="kind transitive">Transitive</span>.*?<div class="chain">(.*?)</div></div>', a, re.S).group(1)
    assert re.findall(r'data-uid="([^"]+)"', path) == ["backend/app.py::create_order", "backend/services/orders.py::place_order", TO_CENTS]
    assert re.findall(r'<span class="where">([^<]+)</span>', path) == ["backend/app.py:8-10", "backend/services/orders.py:7-12",
                                                                    "backend/utils/money.py:1-2"]
    assert 'tabindex="0" role="button"' in path and '<span class="small">calls</span>' in path
    assert "Direct" in html["notest"] or "Transitive" in html["notest"]
    assert "3 entry points" in a


def test_confirmed_and_inferred_labels_are_distinct(html):
    a = html["full"]
    assert '<span class="layer confirmed" title="Confirmed by syntax: route decorator: POST /api/orders">HTTP route</span>' in a
    assert '<span class="layer inferred" title="Inferred: utils/helpers/common module">Shared utility · inferred</span>' in a
    for label in ("Graph-confirmed", "Confirmed by syntax", "Inferred", "Graph-confirmed path", "Inferred link", "Potential impact"):
        assert f">{label}</span>" in a, label
    assert '<span class="arrow inferred" title="HTTP POST /api/orders ⇢ POST /api/orders · inferred">' in a


def test_ui_flow_only_when_an_indexed_path_supports_it(html):
    flow = html["full"].split("User flows / UI impact")[1].split("</section>")[0]
    assert re.findall(r'data-uid="([^"]+)"', flow)[:7] == [
        "frontend/src/pages/CheckoutPage.tsx::CheckoutPage", "frontend/src/components/OrderForm.tsx::OrderForm",
        "frontend/src/components/OrderForm.tsx::OrderForm.handleSubmit", "frontend/src/api/orders.ts::createOrder",
        "backend/app.py::create_order", "backend/services/orders.py::place_order", TO_CENTS]
    assert "renders" in flow and "wires onClick" in flow
    for k in ("backend", "notest", "empty"):
        sec = html[k].split("User flows / UI impact")[1].split("</section>")[0]
        assert "<b>Not established from indexed code.</b>" in sec and "data-uid" not in sec, k
    assert "no frontend code found in this repository" in html["backend"]
    assert "a normal re-index adds them" in html["old"]


def test_affected_files_list_symbols_with_line_references(html):
    files = html["full"].split("Affected files")[1].split("</section>")[0]
    assert 'data-uid="file:backend/services/orders.py"' in files
    assert re.search(r'data-uid="backend/services/orders.py::place_order"[^>]*>place_order</a><span class="where">L7–12</span>', files)
    assert "No affected files (no dependents found)." in html["empty"]


def test_explanation_and_evidence(html):
    ins = html["full"].split("<h3>Suggested inspection</h3>")[1].split("</section>")[0]
    assert "test_place_order_total" in ins and "URL match" in ins and 'data-uid="file:backend/services/orders.py"' in ins
    ex = html["full"].split("<h3>Explanation &amp; evidence</h3>")[1]
    assert "Potential impact derived from graph paths, not verified breakage." in ex
    assert "<code>create_order</code> calls <code>place_order</code>, which calls the selected symbol." in ex
    assert "Evidence &amp; coverage" in ex and "Not analysed:" in ex


def test_empty_impact_renders_honest_state(html):
    e = html["empty"]
    assert "No dependents found following CALLS, IMPORTS, EXTENDS, IMPLEMENTS up to depth 3." in e
    assert "No dependents of <code>health</code> were found" in e and "No direct callers." in e


def test_symbol_names_are_escaped(html):
    a = html["full"]
    assert "<img src=x" not in a and "to_cents&lt;img src=x onerror=alert(1)&gt;" in a


def test_legacy_server_response_is_explained_not_crashed(html):
    lg = html["legacy"]
    assert "running an older" in lg and "Restart it" in lg
    assert 'data-uid="backend/services/orders.py::place_order"' in lg      # the dependents are still listed

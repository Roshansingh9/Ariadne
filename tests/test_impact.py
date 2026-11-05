"""Impact report: paths, layers, UI/API flows, explanation and coverage (local fixtures only)."""

import pytest

from ariadne import frameworks as F
from ariadne.indexer import build_graph
from ariadne.ingest.fetch import FetchResult, Limits, walk_directory
from ariadne.queries import bfs_impact_paths
from tests.conftest import FIXTURE_DIR, FULLSTACK_DIR
from tests.memory_queries import MemoryQueries

TO_CENTS = "backend/utils/money.py::to_cents"
PLACE = "backend/services/orders.py::place_order"
CREATE = "backend/app.py::create_order"
JOB = "backend/worker.py::process_pending"
T_DIRECT = "backend/tests/test_orders.py::test_place_order_total"
T_HTTP = "backend/tests/test_orders.py::test_create_order_endpoint"
CLIENT = "frontend/src/api/orders.ts::createOrder"
HANDLER = "frontend/src/components/OrderForm.tsx::OrderForm.handleSubmit"
FORM = "frontend/src/components/OrderForm.tsx::OrderForm"
PAGE = "frontend/src/pages/CheckoutPage.tsx::CheckoutPage"


@pytest.fixture(scope="module")
def fullstack_graph():
    graph, errors = build_graph(walk_directory(str(FULLSTACK_DIR), Limits()))
    assert errors == []
    return graph


@pytest.fixture()
def fs(fullstack_graph):
    return MemoryQueries(fullstack_graph, "local__fullstack")


def report(q, uid, depth=6, rels=None):
    return q.impact(uid, depth=depth, rels=rels)


def chains(rep):
    return [tuple(p["nodes"]) for p in rep["paths"]]


# --- extraction (index time) -----------------------------------------------------------------

def test_syntax_facts_are_extracted_at_index_time(fullstack_graph):
    sym = {s.uid: s for s in fullstack_graph.symbols()}
    assert sym[CREATE].decorators == ['@app.post("/api/orders")']
    assert sym[JOB].decorators == ["@celery.task"]
    assert sym[CLIENT].http_calls == ["POST /api/orders"]
    assert sym["frontend/src/api/orders.ts::removeOrder"].http_calls == ["DELETE /api/orders/{}"]
    assert sym["frontend/src/api/orders.ts::loadReport"].http_calls == []       # external host: not this app
    assert sym[T_HTTP].http_calls == ["POST /api/orders"]
    assert sym[FORM].renders_jsx and sym[PAGE].renders_jsx and not sym[CLIENT].renders_jsx
    jsx = {(e.src, e.dst): e.props for e in fullstack_graph.edges if e.props.get("syntax")}
    assert jsx[(PAGE, FORM)]["syntax"] == "jsx-render"
    assert jsx[(FORM, HANDLER)] == {**jsx[(FORM, HANDLER)], "syntax": "jsx-event", "event": "onClick"}
    # JSX references are not counted as unresolved *calls*
    assert all("form" not in u and "button" not in u for s in fullstack_graph.symbols() for u in s.unresolved_calls)


def test_framework_patterns():
    assert F.python_routes(['@app.get("/a/{x}")', '@bp.route("/b", methods=["POST", "PUT"])', "@lru_cache()",
                            '@cache.get("key")']) == ["GET /a/{x}", "POST /b", "PUT /b"]
    assert F.python_jobs(["@shared_task(bind=True)", "@staticmethod"]) == ["shared_task"]
    assert F.js_http_calls_from_source("fetch(`/api/x/${id}?q=1`, { method: 'PATCH' })") == ["PATCH /api/x/{}"]
    assert F.http_call("GET", "https://api.github.com/x") is None and F.http_call("GET", "http://localhost:8000/x") == "GET /x"
    assert F.route_matches("GET /orders/{}", "GET /orders/<int:id>") and not F.route_matches("POST /a", "GET /a")


# --- summary, direct/transitive, paths ----------------------------------------------------------

def test_summary_and_direct_vs_transitive(fs):
    r = report(fs, TO_CENTS)
    s = r["report"]["summary"]
    assert (s["direct"], s["transitive"], s["total"], s["files"], s["depth"]) == (1, 3, 4, 4, 6)
    assert s["rels"] == ["CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS"] and s["location"] == "backend/utils/money.py:1-2"
    assert r["report"]["inspection"]["callers"] == [PLACE]
    # the legacy fields are still there for existing clients
    assert {n["uid"] for n in r["impacted"]} == {PLACE, CREATE, JOB, T_DIRECT}


def test_multi_level_paths_reach_the_target_and_cover_every_dependent(fs):
    rep = report(fs, TO_CENTS)["report"]
    assert (CREATE, PLACE, TO_CENTS) in chains(rep) and (JOB, PLACE, TO_CENTS) in chains(rep)
    covered = {u for p in rep["paths"] for u in p["nodes"]}
    assert {PLACE, CREATE, JOB, T_DIRECT} <= covered
    for p in rep["paths"]:
        assert p["nodes"][-1] == TO_CENTS and len(p["edges"]) == len(p["nodes"]) - 1
        assert p["kind"] == ("direct" if p["length"] == 1 else "transitive")
    assert all(rep["nodes"][u]["path"] for p in rep["paths"] for u in p["nodes"])     # file paths for every symbol


def test_multiple_callers_and_alternative_paths():
    # r calls t directly *and* through h: both paths must be shown (the SatyaTrace whatsapp_webhook case).
    rows = {"t": [("t", "CALLS", "resolved", "r"), ("t", "CALLS", "resolved", "h"), ("t", "CALLS", "resolved", "x")],
            "h": [("h", "CALLS", "resolved", "r")]}
    out, parents = bfs_impact_paths(["t"], lambda fr: [row for u in fr for row in rows.get(u, [])], 3)
    assert {u: m["depth"] for u, m in out.items()} == {"r": 1, "h": 1, "x": 1}
    assert [(e["to"], bool(e.get("alt"))) for e in parents["r"]] == [("t", False), ("h", True)]
    from ariadne.impact import _paths
    paths = _paths({"t": "t"}, parents, {}, ["r", "x"], max_len=3)
    assert [p["nodes"] for p in paths if p["root"] == "r"] == [["r", "t"], ["r", "h", "t"]]


def test_cycles_terminate_and_never_repeat_a_node(fs):
    rep = report(fs, "backend/services/orders.py::reconcile")["report"]
    assert chains(rep)                                           # reconcile <-> retry_reconcile is a cycle
    for p in rep["paths"]:
        assert len(set(p["nodes"])) == len(p["nodes"])
    assert ("backend/services/orders.py::retry_reconcile", "backend/services/orders.py::reconcile") in chains(rep)


def test_depth_limit_bounds_dependents_paths_and_flows(fs):
    r = report(fs, TO_CENTS, depth=1)
    rep = r["report"]
    assert {n["uid"] for n in r["impacted"]} == {PLACE}
    assert all(p["length"] <= 1 for p in rep["paths"])
    assert rep["ui"]["flows"] == [] and rep["ui"]["status"] == "not_established"   # the route is 2 hops away


def test_relationship_filters_are_respected(fs):
    rep = report(fs, TO_CENTS, rels=["IMPORTS"])["report"]
    assert rep["summary"]["rels"] == ["IMPORTS"]
    assert all(e["type"] in ("IMPORTS", "MEMBER_OF") for p in rep["paths"] for e in p["edges"])
    with pytest.raises(ValueError):
        report(fs, TO_CENTS, rels=["DEFINES"])                  # not an impact relationship


def test_empty_impact_still_reports_honestly(fs):
    rep = report(fs, "backend/app.py::health")["report"]
    assert rep["summary"]["total"] == 0 and rep["paths"] == []
    assert "No dependents of `health`" in rep["explanation"][0]["text"]
    assert [l["layer"] for l in rep["summary"]["target_layers"]] == ["HTTP route"]   # it *is* a route…
    assert rep["ui"]["flows"] == []                                                 # …that no client calls


def test_affected_file_aggregation(fs):
    files = {f["path"]: f["symbols"] for f in report(fs, TO_CENTS)["report"]["inspection"]["files"]}
    assert files == {"backend/services/orders.py": 1, "backend/app.py": 1, "backend/worker.py": 1,
                     "backend/tests/test_orders.py": 1}


def test_missing_line_information_is_handled(fs):
    fs.edges = [(t, s, d, {k: v for k, v in p.items() if k not in ("line", "lines")}) for t, s, d, p in fs.edges]
    fs.by_uid[CREATE].pop("line_start"); fs.by_uid[CREATE].pop("line_end")
    rep = report(fs, TO_CENTS)["report"]
    assert all("lines" not in e for p in rep["paths"] for e in p["edges"])
    assert rep["nodes"][CREATE]["path"] == "backend/app.py" and "line_start" not in rep["nodes"][CREATE]


# --- architecture: confirmed vs inferred --------------------------------------------------------

def layer_map(rep):
    return {(l["layer"], m["uid"]): (m["basis"], m["reach"]) for l in rep["layers"] for m in l["members"]}


def test_layers_distinguish_syntax_from_heuristics(fs):
    lm = layer_map(report(fs, TO_CENTS)["report"])
    assert lm[("HTTP route", CREATE)] == ("syntax", "graph")
    assert lm[("Background job / event handler", JOB)] == ("syntax", "graph")
    assert lm[("Service / business logic", PLACE)] == ("heuristic", "graph")
    assert lm[("Test", T_DIRECT)] == ("heuristic", "graph")
    assert lm[("API client", CLIENT)] == ("syntax", "via inferred HTTP link")
    assert lm[("UI component", FORM)] == ("syntax", "via inferred HTTP link")
    assert lm[("UI event handler", HANDLER)] == ("syntax", "via inferred HTTP link")
    assert lm[("UI page", PAGE)][0] == "heuristic"                     # pages/ is a path convention
    target_layers = report(fs, TO_CENTS)["report"]["summary"]["target_layers"]
    assert target_layers == [{"layer": "Shared utility", "basis": "heuristic", "evidence": "utils/helpers/common module"}]


def test_model_classification_from_base_class(fs):
    layers = report(fs, "backend/models.py::OrderIn")["report"]["summary"]["target_layers"]
    assert {"layer": "Data model / schema", "basis": "syntax", "evidence": "extends BaseModel (Pydantic model)"} in layers


# --- UI and user flows ------------------------------------------------------------------------

def test_full_stack_flow_ui_to_route_to_target(fs):
    rep = report(fs, TO_CENTS)["report"]
    assert rep["ui"]["status"] == "established"
    flow = rep["ui"]["flows"][0]
    assert flow["nodes"] == [PAGE, FORM, HANDLER, CLIENT, CREATE, PLACE, TO_CENTS]
    kinds = [e.get("syntax") or e["type"] for e in flow["edges"]]
    assert kinds == ["jsx-render", "jsx-event", "CALLS", "HTTP", "CALLS", "CALLS"]
    http = flow["edges"][3]
    assert http["basis"] == "heuristic" and http["confidence"] == "inferred"            # never a graph edge
    assert all(e.get("basis") != "heuristic" for i, e in enumerate(flow["edges"]) if i != 3)
    assert flow["link"] == {"client": CLIENT, "route_node": CREATE, "call": "POST /api/orders",
                            "route": "POST /api/orders", "basis": "heuristic",
                            "rule": "client URL and method match the route path"}


def test_frontend_target_gets_graph_confirmed_ui_impact(fs):
    rep = report(fs, FORM)["report"]
    assert rep["ui"]["status"] == "established" and rep["ui"]["flows"] == []
    p = rep["paths"][0]
    assert p["nodes"] == [PAGE, FORM] and p["edges"][0]["syntax"] == "jsx-render"
    assert "`CheckoutPage` renders the selected symbol." in rep["explanation"][1]["text"]


def backend_only(fullstack_graph):
    fetched = walk_directory(str(FULLSTACK_DIR), Limits())
    only = FetchResult(files=[f for f in fetched.files if f[0].startswith("backend/")], source="directory")
    graph, _ = build_graph(only)
    return MemoryQueries(graph, "local__backend")


def test_backend_only_repo_has_no_fabricated_ui(fullstack_graph):
    q = backend_only(fullstack_graph)
    rep = report(q, TO_CENTS)["report"]
    assert rep["ui"] == {"status": "no_frontend", "message": "UI impact not established — no frontend code found in this repository.",
                         "flows": [], "graph_paths": 0, "links": []}
    assert not any(l["layer"].startswith("UI") or l["layer"] == "API client" for l in rep["layers"])
    assert rep["coverage"]["frontend_files"] == 0


def test_js_without_matching_routes_is_not_established(memory_queries):
    # sample_repo: TS client axios.get(`/orders/${id}`) but no backend route for it
    rep = memory_queries.impact("shop/utils.py::fmt", depth=3)["report"]
    assert rep["ui"]["status"] == "not_established"
    assert rep["ui"]["message"] == "UI/API relationship not established from indexed code."


# --- tests, behaviour, explanation ------------------------------------------------------------

def test_tests_are_reported_only_with_evidence(fs):
    rep = report(fs, TO_CENTS)["report"]
    tests = {t["uid"]: t["basis"] for t in rep["inspection"]["tests"]}
    assert tests == {T_DIRECT: "graph", T_HTTP: "heuristic"}          # direct call vs URL match
    assert rep["inspection"]["tests_note"] is None
    none = report(fs, "backend/services/orders.py::reconcile")["report"]["inspection"]
    assert none["tests"] == [] and "does not mean the code is untested" in none["tests_note"]


def test_behaviour_and_explanation_are_grounded(fs):
    rep = report(fs, TO_CENTS)["report"]
    texts = [b["text"] for b in rep["behaviors"]]
    assert any("POST /api/orders" in t and "`create_order`" in t for t in texts)
    assert any("celery.task" in t for t in texts)
    ex = [e["text"] for e in rep["explanation"]]
    assert "`create_order` calls `place_order`, which calls the selected symbol. A change may affect the POST /api/orders request path." in ex
    assert any("(inferred from the URL; not a graph edge)" in t for t in ex)
    assert rep["explanation"][-1]["basis"] == "caveat" and "not verified breakage" in ex[-1]
    for word in ("will break", "breaks", "guaranteed"):
        assert all(word not in t for t in ex + texts)


def test_unresolved_relationships_are_counted_not_followed(fs):
    cov = report(fs, CLIENT)["report"]["coverage"]
    assert cov["unresolved_calls_in_scope"] >= 2                     # fetch, res.json are unresolved in createOrder
    assert any("dynamic dispatch" in x for x in cov["not_analysed"])


# --- compatibility with graphs indexed before these facts -----------------------------------------

def test_old_graph_without_new_metadata_still_works(fullstack_graph):
    q = MemoryQueries(fullstack_graph, "local__old").as_old_graph()
    r = report(q, TO_CENTS)
    rep = r["report"]
    assert rep["coverage"]["graph_version"] == 1 and rep["coverage"]["derived_nodes"] > 0
    assert "re-index" in rep["coverage"]["facts_source"] and "re-index" in rep["ui"]["message"]
    lm = layer_map(rep)
    assert lm[("HTTP route", CREATE)][0] == "syntax"                 # read from the stored decorator text
    flow = rep["ui"]["flows"][0]
    assert flow["nodes"][-4:] == [CLIENT, CREATE, PLACE, TO_CENTS] and FORM not in flow["nodes"]   # no JSX edges yet
    assert {n["uid"] for n in r["impacted"]} == {PLACE, CREATE, JOB, T_DIRECT}                     # core impact unchanged


def test_legacy_fixture_impact_results_unchanged(memory_queries):
    got = {n["uid"]: (n["depth"], n["confidence"])
           for n in memory_queries.impact("shop/models.py::Base.validate", depth=4)["impacted"]}
    assert got["shop/models.py::Base.save"] == (1, "resolved")
    assert all("parents" in n for n in memory_queries.impact("shop/utils.py::fmt", depth=2)["impacted"])

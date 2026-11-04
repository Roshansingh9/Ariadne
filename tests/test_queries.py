"""Traversal, impact and query-safety tests (production BFS over the fixture graph)."""

import pytest

from ariadne.queries import (GraphQueries, MAX_IMPACT_DEPTH, NodeNotFound, bfs_impact, bfs_path,
                             clamp, validate_rels)


class RecordingStore:
    """Captures Cypher + params; returns canned rows."""

    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or []

    def query(self, repo_id, cypher, params=None):
        self.calls.append((cypher, params or {}))
        return self.rows


def test_path_through_calls(memory_queries):
    r = memory_queries.path("main.py::main", "shop/models.py::Base.validate", rels=["CALLS"])
    assert r["found"] and r["length"] == 3
    assert [n["uid"] for n in r["nodes"]] == [
        "main.py::main", "shop/service.py::OrderService.place", "shop/models.py::Base.save",
        "shop/models.py::Base.validate"]
    assert [h["confidence"] for h in r["hops"]] == ["inferred", "inferred", "resolved"]


def test_path_across_imports_and_ts_js(memory_queries):
    r = memory_queries.path("file:web/index.js", "file:web/format.js", rels=["IMPORTS"])
    assert r["found"] and [n["uid"] for n in r["nodes"]] == [
        "file:web/index.js", "file:web/api.ts", "file:web/format.js"]


def test_path_respects_direction_and_depth(memory_queries):
    assert not memory_queries.path("shop/utils.py::fmt", "main.py::main", rels=["CALLS"])["found"]
    assert memory_queries.path("shop/utils.py::fmt", "shop/models.py::Order.total", rels=["CALLS"],
                               undirected=True)["found"]
    assert not memory_queries.path("main.py::main", "shop/models.py::Base.validate", rels=["CALLS"],
                                   max_depth=2)["found"]


def test_path_unknown_node(memory_queries):
    with pytest.raises(NodeNotFound):
        memory_queries.path("nope", "main.py::main")


def test_impact_of_base_validate(memory_queries):
    r = memory_queries.impact("shop/models.py::Base.validate", depth=4)
    got = {n["uid"]: (n["depth"], n["confidence"]) for n in r["impacted"]}
    assert got["shop/models.py::Base.save"] == (1, "resolved")
    assert got["shop/models.py::Order.validate"] == (1, "resolved")
    assert got["shop/service.py::OrderService.place"] == (2, "inferred")   # weakest link propagates
    assert got["main.py::main"] == (3, "inferred")
    assert got["file:main.py"] == (4, "inferred")
    files = {f["path"] for f in r["affected_files"]}
    assert files == {"shop/models.py", "shop/service.py", "main.py"}


def test_impact_of_file_includes_members_and_importers(memory_queries):
    r = memory_queries.impact("file:shop/utils.py", depth=2)
    uids = {n["uid"] for n in r["impacted"]}
    assert r["seed_count"] == 4                     # the file + 3 functions it defines
    assert {"file:shop/models.py", "file:shop/service.py",       # importers
            "shop/models.py::Order.total", "shop/service.py::OrderService.place"} <= uids
    assert "shop/utils.py::fmt" not in uids          # seeds are not reported as impacted


def test_impact_of_class_includes_subclasses(memory_queries):
    r = memory_queries.impact("web/base.ts::BaseClient", depth=1)
    assert "web/api.ts::Client" in {n["uid"] for n in r["impacted"]}


def test_bfs_handles_cycles():
    edges = {"a": ["b"], "b": ["c"], "c": ["a", "d"]}

    def expand(frontier):
        return [(u, "CALLS", "resolved", v) for u in frontier for v in edges.get(u, [])]

    def expand_rev(frontier):
        return [(v, "CALLS", "resolved", u) for u, vs in edges.items() for v in vs if v in frontier]

    assert [h["to"] for h in bfs_path("a", "d", expand, 10)] == ["b", "c", "d"]
    imp = bfs_impact(["d"], expand_rev, 10)
    assert {k: v["depth"] for k, v in imp.items()} == {"c": 1, "b": 2, "a": 3}


def test_bfs_prefers_stronger_confidence_at_same_depth():
    rows = [("t", "CALLS", "ambiguous", "x"), ("t", "CALLS", "resolved", "x")]
    imp = bfs_impact(["t"], lambda fr: rows if "t" in fr else [], 3)
    assert imp["x"]["confidence"] == "resolved"


def test_validation_and_clamping():
    assert validate_rels(None)[0] == "CALLS"
    assert validate_rels(["calls", "IMPORTS", "calls"]) == ["CALLS", "IMPORTS"]
    for bad in (["CALLS]->() DETACH DELETE n //"], ["FOO"], ["CALLS|DEFINES"]):
        with pytest.raises(ValueError):
            validate_rels(bad)
    assert clamp("999", 1, MAX_IMPACT_DEPTH, 3) == MAX_IMPACT_DEPTH
    assert clamp("x", 1, 6, 3) == 3 and clamp(-5, 1, 6, 3) == 1


@pytest.mark.parametrize("payload", [
    "x' OR 1=1 //", "}) MATCH (n) DETACH DELETE n //", "`; DROP", "a} RETURN n UNION MATCH (m) RETURN m //", "\\u0027",
])
def test_user_values_never_reach_cypher_text(payload):
    store = RecordingStore()
    gq = GraphQueries(store, "github__a__b")
    gq.search(payload, "all")
    with pytest.raises(NodeNotFound):
        gq.node(payload)
    with pytest.raises(NodeNotFound):
        gq.neighbors(payload)
    for cypher, params in store.calls:
        assert payload.lower() not in cypher.lower()
        assert payload.lower() in [str(v).lower() for v in params.values()]


def test_search_kind_is_allowlisted():
    gq = GraphQueries(RecordingStore(), "github__a__b")
    with pytest.raises(ValueError):
        gq.search("x", "Function) DETACH DELETE (n")
    with pytest.raises(ValueError):
        gq.search("x" * 201)
    assert gq.search("   ") == []


def test_search_query_shape():
    store = RecordingStore()
    GraphQueries(store, "github__a__b").search("Order", "class", limit=500)
    cypher, params = store.calls[0]
    assert "n:Class" in cypher and params == {"t": "order", "limit": 100}


# --- repository graph (initial visualization) -------------------------------

def test_repo_graph_returns_connected_nodes_and_edges(memory_queries):
    g = memory_queries.repo_graph()
    assert g["nodes"] and g["edges"]
    uids = {n["uid"] for n in g["nodes"]}
    assert all(e["src"] in uids and e["dst"] in uids for e in g["edges"])   # no dangling edges
    assert all("label" in n for n in g["nodes"])
    assert g["truncated"] is False and g["total_nodes"] == len(g["nodes"])


def test_repo_graph_respects_relationship_filter(memory_queries):
    g = memory_queries.repo_graph(["EXTENDS"])
    assert g["rels"] == ["EXTENDS"]
    assert g["edges"] and {e["type"] for e in g["edges"]} == {"EXTENDS"}
    every = memory_queries.repo_graph()
    assert len(g["nodes"]) < len(every["nodes"])


def test_repo_graph_limit_keeps_most_connected(memory_queries):
    full = memory_queries.repo_graph(limit=400)
    small = memory_queries.repo_graph(limit=3)
    assert len(small["nodes"]) == 3 and small["truncated"] is True
    assert small["total_nodes"] == full["total_nodes"]
    # The kept nodes are the highest-degree ones.
    degree = {}
    for e in full["edges"]:
        degree[e["src"]] = degree.get(e["src"], 0) + 1
        if e["dst"] != e["src"]:
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
    top = sorted(degree.values(), reverse=True)[:3]
    assert sorted((degree[n["uid"]] for n in small["nodes"]), reverse=True) == top
    kept = {n["uid"] for n in small["nodes"]}
    assert all(e["src"] in kept and e["dst"] in kept for e in small["edges"])


def test_repo_graph_is_deterministic(memory_queries):
    assert memory_queries.repo_graph(limit=5) == memory_queries.repo_graph(limit=5)


def test_repo_graph_query_shape_and_validation():
    store = RecordingStore()
    gq = GraphQueries(store, "github__a__b")
    g = gq.repo_graph(["calls", "IMPORTS"], limit=99999)
    assert g == {"nodes": [], "edges": [], "rels": ["CALLS", "IMPORTS"], "total_nodes": 0,
                 "total_edges": 0, "truncated": False}
    cypher, params = store.calls[0]
    assert "[e:CALLS|IMPORTS]" in cypher and "ORDER BY" in cypher and "LIMIT $cap" in cypher
    assert params["cap"] > 1000
    with pytest.raises(ValueError):
        gq.repo_graph(["CALLS]->() DETACH DELETE n //"])

"""Graph schema + persistence (mocked FalkorDB client)."""

import re

import pytest

from ariadne.config import Settings
from ariadne.model import EDGE_TYPES, NODE_LABELS
from ariadne.store import (FalkorStore, StoreError, build_write_plan, check_repo_id, graph_name,
                           node_rows)
from tests.fake_falkor import FakeDB


def make_store():
    store = FalkorStore(Settings(falkordb_host="fake.cloud", graph_prefix="ariadne_test"))
    store._db = FakeDB()
    return store


def test_graph_naming_and_validation():
    assert graph_name("ariadne", "github__acme__shop") == "ariadne:github__acme__shop"
    for bad in ["", "../x", "a b", "A:B", "x" * 200, "a}{b", "a`b"]:
        with pytest.raises(ValueError):
            check_repo_id(bad)
    with pytest.raises(ValueError):
        graph_name("bad prefix!", "ok")


def test_node_schema_and_unique_uids(fixture_graph):
    rows = node_rows(fixture_graph)
    assert set(rows) == set(NODE_LABELS)
    uids = [r["uid"] for rs in rows.values() for r in rs]
    assert len(uids) == len(set(uids))
    fn = next(r for r in rows["Function"] if r["uid"] == "shop/models.py::Order.total")
    assert fn["line_start"] == 21 and fn["line_end"] == 22 and fn["path"] == "shop/models.py"
    assert "def total(self)" in fn["source"]
    f = next(r for r in rows["File"] if r["uid"] == "file:web/api.ts")
    assert f["language"] == "typescript" and f["ext"] == ".ts"
    # FalkorDB properties must be scalars or flat lists — no nested maps
    for rs in rows.values():
        for r in rs:
            for v in r.values():
                assert not isinstance(v, dict)
                if isinstance(v, list):
                    assert all(isinstance(x, (str, int)) for x in v)


def test_write_plan_is_deterministic_parameterized_and_allowlisted(fixture_graph):
    plan_a = build_write_plan(fixture_graph, batch_size=7)
    plan_b = build_write_plan(fixture_graph, batch_size=7)
    assert [(s.cypher, s.params) for s in plan_a] == [(s.cypher, s.params) for s in plan_b]
    assert all(s.rows <= 7 for s in plan_a)
    allowed = set(NODE_LABELS) | set(EDGE_TYPES) | {"Entity"}
    for s in plan_a:
        assert s.cypher.startswith("UNWIND $rows AS r")
        assert "MERGE" in s.cypher
        for ident in re.findall(r":(\w+)", s.cypher):
            assert ident in allowed
        # no repository content is ever interpolated into Cypher text
        for row in s.params["rows"]:
            assert row.get("uid", row.get("src")) not in s.cypher
    node_total = sum(s.rows for s in plan_a if s.kind.startswith("node:"))
    edge_total = sum(s.rows for s in plan_a if s.kind.startswith("edge:"))
    assert node_total == 9 + 6 + 18 + 4
    assert edge_total == len(fixture_graph.edges)


def test_replace_graph_writes_everything_and_marks_ready(fixture_graph):
    store = make_store()
    info = store.replace_graph("github__acme__shop", {"url": "u"}, fixture_graph)
    g = store._db.graphs["ariadne_test:github__acme__shop"]
    assert len(g["nodes"]) == 37
    assert len(g["edges"]) == len(fixture_graph.edges)
    assert g["repo"]["status"] == "ready" and g["repo"]["url"] == "u"
    assert g["log"][0].startswith("CREATE INDEX FOR (n:Entity) ON (n.uid)")
    assert info["rows"] == 37 + len(fixture_graph.edges)


def test_reindex_does_not_duplicate_and_drops_stale(fixture_graph):
    store = make_store()
    rid = "github__acme__shop"
    store.replace_graph(rid, {}, fixture_graph)
    g1 = store._db.graphs[f"ariadne_test:{rid}"]
    first = (dict(g1["nodes"]), dict(g1["edges"]))
    # simulate stale content from an older index
    g1["nodes"]["file:deleted.py"] = {"label": "File", "uid": "file:deleted.py"}
    store.replace_graph(rid, {}, fixture_graph)
    g2 = store._db.graphs[f"ariadne_test:{rid}"]
    assert (g2["nodes"], g2["edges"]) == first
    assert "file:deleted.py" not in g2["nodes"]


def test_replaying_a_batch_is_idempotent(fixture_graph):
    """A retried batch (e.g. after a timeout) must not create duplicates."""
    store = make_store()
    rid = "github__acme__shop"
    store.replace_graph(rid, {}, fixture_graph)
    g = store._db.select_graph(f"ariadne_test:{rid}")
    before = (len(g.data["nodes"]), len(g.data["edges"]))
    for st in build_write_plan(fixture_graph):
        g.query(st.cypher, st.params)
    assert (len(g.data["nodes"]), len(g.data["edges"])) == before


def test_connection_errors_are_mapped_to_safe_messages(fixture_graph):
    store = FalkorStore(Settings(falkordb_host="fake", falkordb_password="s3cr3t-value"))
    store._db = FakeDB()
    store._db.fail_on = "UNWIND"
    with pytest.raises(StoreError) as exc:
        store.replace_graph("github__a__b", {}, fixture_graph)
    assert "Cannot reach FalkorDB" in str(exc.value)
    assert "s3cr3t" not in str(exc.value)


def test_unconfigured_store_raises():
    with pytest.raises(StoreError, match="not configured"):
        FalkorStore(Settings()).list_repos()


def test_index_lock_and_restore():
    store = make_store()
    rid = "github__acme__shop"
    assert store.mark_indexing(rid, {"url": "u"}) is None
    with pytest.raises(StoreError, match="already being indexed"):
        store.mark_indexing(rid, {"url": "u"})
    store.restore_status(rid, None, "failed early")        # no previous graph → removed
    assert store.list_repos() == []

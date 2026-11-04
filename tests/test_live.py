"""LIVE integration tests — skipped unless explicitly enabled.

  ARIADNE_LIVE_FALKORDB=1  uses FALKORDB_HOST/PORT/USERNAME/PASSWORD[/SSL] (FalkorDB Cloud or local)
  ARIADNE_LIVE_META=1      uses META_API_KEY (one real chat completion)

These exercise the real Cypher templates and the real Meta endpoint. They write
only to the graph `ariadne_livetest:local__fixture` and delete it afterwards.
"""

import os

import pytest

from ariadne.config import load_settings
from ariadne.indexer import index_directory
from ariadne.meta_client import MetaClient
from ariadne.qa import answer_question
from ariadne.queries import GraphQueries
from ariadne.store import FalkorStore
from tests.conftest import FIXTURE_DIR

RID = "local__fixture"

live_db = pytest.mark.skipif(os.getenv("ARIADNE_LIVE_FALKORDB") != "1", reason="ARIADNE_LIVE_FALKORDB not set")
live_meta = pytest.mark.skipif(os.getenv("ARIADNE_LIVE_META") != "1", reason="ARIADNE_LIVE_META not set")


LIVE_PREFIX = "ariadne_livetest"
LIVE_GRAPH = f"{LIVE_PREFIX}:{RID}"


def _cleanup(store: FalkorStore) -> None:
    from ariadne.store import graph_name
    # Hard guard: this helper may only ever delete the throwaway test graph.
    assert graph_name(store.settings.graph_prefix, RID) == LIVE_GRAPH
    store.delete_repo(RID)


@pytest.fixture(scope="module")
def live_store():
    from dataclasses import replace
    settings = replace(load_settings(), graph_prefix=LIVE_PREFIX)
    store = FalkorStore(settings)
    _cleanup(store)                      # leftovers from an aborted earlier run
    try:
        yield store
    finally:                             # pytest runs this even when a test fails
        _cleanup(store)
        assert LIVE_GRAPH not in store._graph_names(), "throwaway graph was not removed"


@live_db
@pytest.mark.live_falkordb
def test_live_index_query_reindex(live_store):
    assert live_store.ping()
    out = index_directory(str(FIXTURE_DIR), RID, live_store)
    gq = GraphQueries(live_store, RID)

    ov = gq.overview()
    assert ov["counts"] == {"File": 9, "Class": 6, "Function": 18, "Package": 4}
    assert ov["edges"]["CALLS"]["total"] == 17
    assert ov["repo"]["status"] == "ready"

    res = gq.search("order", "class")
    assert res[0]["uid"] == "shop/models.py::Order"
    assert gq.search("utils", "file")[0]["uid"] == "file:shop/utils.py"

    sym = gq.symbol("shop/models.py::Base.validate")
    assert {c["uid"] for c in sym["callers"]} == {"shop/models.py::Base.save", "shop/models.py::Order.validate"}
    assert sym["callers"][0]["edge"]["confidence"] == "resolved"
    f = gq.symbol("file:shop/service.py")
    assert {x["uid"] for x in f["imports"]} == {"file:shop/models.py", "file:shop/utils.py"}
    assert [p["name"] for p in f["packages"]] == ["requests"]

    nb = gq.neighbors("shop/models.py::Order", ["EXTENDS", "DEFINES"])
    assert {e["type"] for e in nb["edges"]} == {"EXTENDS", "DEFINES"}

    # Repository graph (initial canvas): real Cypher must agree with the in-memory double.
    from tests.memory_queries import MemoryQueries
    from ariadne.indexer import build_graph
    from ariadne.ingest.fetch import Limits, walk_directory
    expected = MemoryQueries(build_graph(walk_directory(str(FIXTURE_DIR), Limits()))[0], RID)
    for rels, limit in ((None, 150), (["CALLS", "IMPORTS"], 150), (["EXTENDS"], 150), (None, 5)):
        got, want = gq.repo_graph(rels, limit), expected.repo_graph(rels, limit)
        assert got["edges"] and got["nodes"]
        assert [n["uid"] for n in got["nodes"]] == [n["uid"] for n in want["nodes"]], rels
        assert sorted((e["src"], e["type"], e["dst"]) for e in got["edges"]) == \
            sorted((e["src"], e["type"], e["dst"]) for e in want["edges"]), rels
        assert got["total_nodes"] == want["total_nodes"] and got["truncated"] == want["truncated"]

    p = gq.path("main.py::main", "shop/models.py::Base.validate", ["CALLS"])
    assert p["found"] and p["length"] == 3

    imp = gq.impact("shop/models.py::Base.validate", depth=4)
    got = {n["uid"]: (n["depth"], n["confidence"]) for n in imp["impacted"]}
    assert got["main.py::main"] == (3, "inferred")

    # re-index: counts must be identical (no duplicates)
    index_directory(str(FIXTURE_DIR), RID, live_store)
    ov2 = gq.overview()
    assert ov2["counts"] == ov["counts"] and ov2["edges"] == ov["edges"]
    rows = live_store.query(RID, "MATCH (n:Entity) WITH n.uid AS u, count(*) AS c WHERE c > 1 RETURN u")
    assert rows == []
    assert out["stats"]["files"] == 9


@live_db
@live_meta
@pytest.mark.live_meta
def test_live_ask(live_store):
    index_directory(str(FIXTURE_DIR), RID, live_store)
    r = answer_question(GraphQueries(live_store, RID), MetaClient(load_settings()),
                        "What calls Base.validate, and what is affected if it changes?")
    assert r["mode"] == "model", r.get("model_error")
    assert r["citations"], r["answer"]


@live_meta
@pytest.mark.live_meta
def test_live_meta_ping():
    res = MetaClient(load_settings()).chat([{"role": "user", "content": "Reply with the single word: ok"}],
                                           max_completion_tokens=300)
    assert res.content.strip()
    assert res.finish_reason == "stop"


@live_meta
@pytest.mark.live_meta
def test_live_meta_small_budget_recovers():
    """A budget consumed entirely by reasoning must be retried, not surface as an empty answer."""
    res = MetaClient(load_settings()).chat([{"role": "user", "content": "Reply with the single word: ok"}],
                                           max_completion_tokens=20)
    assert res.content.strip()

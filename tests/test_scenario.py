"""Natural-language impact scenarios: resolution, confirmation, model validation, API (local fixtures)."""

import pytest
from fastapi.testclient import TestClient

from ariadne import api as api_mod
from ariadne.indexer import build_graph
from ariadne.ingest.fetch import FetchResult, Limits, walk_directory
from ariadne.meta_client import ChatResult, MetaAPIError
from ariadne.queries import NodeNotFound
from ariadne.scenario import analyze, change_notes, resolve, scenario_terms, validate_model_ranking
from tests.conftest import FULLSTACK_DIR
from tests.memory_queries import MemoryQueries

TO_CENTS = "backend/utils/money.py::to_cents"
PLACE = "backend/services/orders.py::place_order"
CREATE = "backend/app.py::create_order"
RECONCILE = "backend/services/orders.py::reconcile"


class FakeMeta:
    def __init__(self, reply="", error=None, configured=True):
        self.reply, self.error, self.configured, self.calls = reply, error, configured, []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return ChatResult(content=self.reply, model="muse-spark-1.3", usage={}, finish_reason="stop")


@pytest.fixture(scope="module")
def graph():
    return build_graph(walk_directory(str(FULLSTACK_DIR), Limits()))[0]


@pytest.fixture()
def fs(graph):
    return MemoryQueries(graph, "local__fullstack")


def sel(r):
    return [c["uid"] for c in r["resolution"]["candidates"] if c["selected"]]


# --- parsing the scenario -----------------------------------------------------------------------

def test_terms_drop_change_words_report_words_and_concepts():
    terms = [t["term"] for t in scenario_terms("If I change the retry behavior, what functions and tests should I inspect?")]
    assert terms == ["retry"]
    t = scenario_terms("What if `Order.total` or \"to_cents\" or api/index.py changes in the API?")
    assert [(x["term"], x["identifier"]) for x in t] == [("Order.total", True), ("to_cents", True), ("api/index.py", True)]


def test_change_notes_flag_what_static_analysis_cannot_see():
    notes = change_notes("If I rename this field in the user model and the response becomes nullable")
    assert any("Data shapes" in n for n in notes) and any("rename" in n for n in notes)
    assert change_notes("If I change to_cents") == []


# --- resolution: exact, ambiguous, weak, none ------------------------------------------------------

def test_exact_identifier_auto_analyzes(fs):
    r = analyze(fs, None, "If I change to_cents, what could be affected?")
    assert r["resolution"]["status"] == "analyzed" and sel(r) == [TO_CENTS]
    c = r["resolution"]["candidates"][0]
    assert (c["uid"], c["match"], c["confidence"]) == (TO_CENTS, "exact", "high")
    assert r["report"]["report"]["summary"]["targets"] == [TO_CENTS]
    assert r["report"]["report"]["summary"]["direct"] == 1


def test_file_path_target(fs):
    r = analyze(fs, None, "What could be affected if backend/utils/money.py changes?")
    assert sel(r) == ["file:backend/utils/money.py"] and r["report"]["report"]["summary"]["total"] >= 1


def test_ambiguous_name_is_never_selected_silently(graph):
    q = MemoryQueries(build_graph(walk_directory(str(FULLSTACK_DIR.parent / "sample_repo"), Limits()))[0], "local__sample")
    r = analyze(q, None, "What breaks if I change `validate`?")         # Base.validate and Order.validate
    res = r["resolution"]
    assert res["status"] == "needs_confirmation" and r["report"] is None and sel(r) == []
    amb = [c for c in res["candidates"] if c["ambiguous"]]
    assert {c["uid"] for c in amb} == {"shop/models.py::Base.validate", "shop/models.py::Order.validate"}
    assert all(c["confidence"] == "medium" and "2 symbols share this name" in c["reasons"][0] for c in amb)


def test_plain_word_match_needs_confirmation(fs):
    r = analyze(fs, None, "If I change reconcile, what should I inspect?")
    res = r["resolution"]
    assert res["status"] == "needs_confirmation" and r["report"] is None
    assert [c["uid"] for c in res["candidates"] if c["suggested"]] == [RECONCILE]          # pre-ticked, not run


def test_weak_matches_are_only_offered(fs):
    r = analyze(fs, None, "If I change the retry behavior, what functions and tests should I inspect?")
    res = r["resolution"]
    assert res["status"] == "needs_confirmation"
    assert [(c["uid"], c["confidence"], c["suggested"]) for c in res["candidates"]] == \
        [("backend/services/orders.py::retry_reconcile", "low", False)]
    assert any("Behavioural changes" in n for n in res["notes"])


def test_no_match(fs):
    r = analyze(fs, None, "What happens if authentication middleware changes?")
    assert r["resolution"]["status"] == "no_match" and r["report"] is None
    assert "No matching indexed symbol found" in r["resolution"]["message"]
    assert r["resolution"]["unmatched"] == ["authentication", "middleware"]


def test_concepts_offer_only_syntax_confirmed_nodes(fs):
    res = resolve(fs, "What could break if this API response becomes nullable?")
    concept = {c["uid"]: c for c in res["candidates"] if c["match"] == "concept"}
    assert set(concept) == {CREATE, "backend/app.py::delete_order", "backend/app.py::health"}   # the real routes
    assert all(c["confidence"] == "low" and "confirmed by syntax" in c["reasons"][0] for c in concept.values())
    assert res["concepts"] == ["HTTP route"]


# --- confirmation, multiple targets, depth and filters ------------------------------------------------

def test_confirmed_multiple_targets(fs):
    r = analyze(fs, None, "If I change reconcile or to_cents", targets=[RECONCILE, TO_CENTS])
    s = r["report"]["report"]["summary"]
    assert r["resolution"]["status"] == "analyzed" and s["targets"] == [RECONCILE, TO_CENTS]
    roots = {p["root"] for p in r["report"]["report"]["paths"]}
    assert {CREATE, "backend/app.py::delete_order"} <= roots                     # paths from both targets
    ends = {p["nodes"][-1] for p in r["report"]["report"]["paths"]}
    assert ends <= {RECONCILE, TO_CENTS}


def test_user_selected_target_outside_candidates_and_validation(fs):
    r = analyze(fs, None, "anything", targets=[PLACE])
    c = [x for x in r["resolution"]["candidates"] if x["uid"] == PLACE][0]
    assert c["match"] == "user" and c["selected"] and r["report"]["report"]["summary"]["targets"] == [PLACE]
    with pytest.raises(NodeNotFound):
        analyze(fs, None, "anything", targets=["backend/invented.py::nope"])
    with pytest.raises(ValueError):
        analyze(fs, None, "anything", targets=[])
    with pytest.raises(ValueError):
        analyze(fs, None, "   ")


def test_depth_and_relationship_filters(fs):
    shallow = analyze(fs, None, "If I change to_cents", depth=1)["report"]
    assert {n["uid"] for n in shallow["impacted"]} == {PLACE} and shallow["report"]["ui"]["flows"] == []
    imports = analyze(fs, None, "If I change to_cents", rels=["IMPORTS"])["report"]
    assert imports["rels"] == ["IMPORTS"] and all(e["type"] in ("IMPORTS", "MEMBER_OF")
                                                  for p in imports["report"]["paths"] for e in p["edges"])


def test_ui_impact_only_with_graph_evidence(fs, graph):
    full = analyze(fs, None, "If I change to_cents")["report"]["report"]["ui"]
    assert full["status"] == "established" and full["flows"][0]["nodes"][0] == "frontend/src/pages/CheckoutPage.tsx::CheckoutPage"
    fetched = walk_directory(str(FULLSTACK_DIR), Limits())
    backend = MemoryQueries(build_graph(FetchResult(files=[f for f in fetched.files if f[0].startswith("backend/")], source="d"))[0], "b")
    ui = analyze(backend, None, "If I change to_cents")["report"]["report"]["ui"]
    assert ui["status"] == "no_frontend" and ui["flows"] == []


def test_old_graph_and_missing_lines(graph):
    q = MemoryQueries(graph, "old").as_old_graph()
    q.edges = [(t, s, d, {k: v for k, v in p.items() if k not in ("line", "lines")}) for t, s, d, p in q.edges]
    r = analyze(q, None, "What if the API changes?")
    concept = {c["uid"] for c in r["resolution"]["candidates"] if c["match"] == "concept"}
    assert CREATE in concept                                                     # routes read from stored decorator text
    r = analyze(q, None, "If I change to_cents")
    assert r["report"]["report"]["coverage"]["graph_version"] == 1
    assert all("lines" not in e for p in r["report"]["report"]["paths"] for e in p["edges"])


# --- Meta ranking: only shown candidates, validated, never auto-run -----------------------------------

def test_model_ranking_is_validated_and_only_suggests(fs):
    meta = FakeMeta('{"targets": [{"ref": "C1", "why": "the retry loop"}, {"ref": "C9", "why": "invented"}, '
                    '{"ref": "backend/fake.py::x"}], "none_fit": false}')
    r = analyze(fs, meta, "If I change the retry behavior, what should I inspect?")
    res = r["resolution"]
    assert res["status"] == "needs_confirmation" and r["report"] is None           # still needs the user's OK
    assert res["model"]["picked"] == ["backend/services/orders.py::retry_reconcile"]
    assert res["model"]["rejected"] == ["C9", "backend/fake.py::x"]
    c = res["candidates"][0]
    assert c["model_suggested"] and c["suggested"] and not c["selected"] and c["model_reason"] == "the retry loop"
    prompt = meta.calls[0][1]["content"]
    assert "[C1] Function `retry_reconcile`" in prompt and "Never invent" in meta.calls[0][0]["content"]


def test_model_not_called_when_an_exact_match_exists_or_disabled(fs):
    meta = FakeMeta('{"targets": []}')
    analyze(fs, meta, "If I change to_cents")
    analyze(fs, meta, "If I change the retry behavior", use_model=False)
    assert meta.calls == []


def test_model_errors_and_bad_output_are_reported_not_trusted(fs):
    r = analyze(fs, FakeMeta(error=MetaAPIError("Meta Model API rate limit reached (429).", 429)), "If I change the retry behavior")
    assert "429" in r["resolution"]["model"]["error"] and r["resolution"]["status"] == "needs_confirmation"
    bad = validate_model_ranking("Sure! I think retry_reconcile is it.", {"C1": {"uid": "u"}})
    assert bad["picked"] == [] and "did not return the required JSON" in bad["error"]
    capped = validate_model_ranking('{"targets": ' + str([{"ref": f"C{i}"} for i in range(1, 9)]).replace("'", '"') + "}",
                                    {f"C{i}": {"uid": f"u{i}"} for i in range(1, 9)})
    assert len(capped["picked"]) == 5


# --- HTTP API -------------------------------------------------------------------------------------

@pytest.fixture()
def client(fs):
    api_mod.app.dependency_overrides[api_mod.repo_queries] = lambda repo_id: fs
    api_mod.app.dependency_overrides[api_mod.get_meta] = lambda: FakeMeta(configured=False)
    yield TestClient(api_mod.app)
    api_mod.app.dependency_overrides.clear()


def test_api_scenario_endpoint(client):
    r = client.post("/api/repos/local__fullstack/impact/scenario", json={"scenario": "If I change to_cents", "depth": 4})
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"]["status"] == "analyzed" and body["report"]["depth"] == 4
    assert body["repo"]["id"] == "local__fullstack"
    r = client.post("/api/repos/local__fullstack/impact/scenario",
                    json={"scenario": "If I change reconcile", "targets": [RECONCILE], "rels": ["CALLS"]})
    assert r.json()["report"]["rels"] == ["CALLS"]


@pytest.mark.parametrize("body,code", [({"scenario": ""}, 422), ({"scenario": "x" * 1001}, 422),
                                       ({"scenario": "x", "depth": 9}, 422), ({"scenario": "x", "targets": ["a"] * 11}, 422),
                                       ({"scenario": "x", "targets": ["nope"]}, 404), ({"scenario": "x", "rels": ["DEFINES"]}, 400)])
def test_api_scenario_validation(client, body, code):
    body = {**body}
    if code == 400:
        body["targets"] = [TO_CENTS]
    assert client.post("/api/repos/local__fullstack/impact/scenario", json=body).status_code == code


def test_candidates_keep_their_real_node_labels(fs):
    labels = {c["uid"]: c["label"] for c in resolve(fs, "If I change to_cents or the Order class or orders.ts")["candidates"]}
    assert labels[TO_CENTS] == "Function" and labels["backend/models.py::Order"] == "Class"
    assert "Entity" not in labels.values()

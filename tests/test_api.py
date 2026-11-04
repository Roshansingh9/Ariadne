"""HTTP API tests (FastAPI TestClient; FalkorDB and Meta are faked)."""

import json

import pytest
from fastapi.testclient import TestClient

from ariadne import api as api_mod
from ariadne.config import Settings
from ariadne.ingest.fetch import FetchError
from ariadne.store import FalkorStore
from tests.fake_falkor import FakeDB

SECRET_PW = "falkor-pw-should-never-leak"
SECRET_KEY = "LLM_999999999_metakeyshouldneverleak"


@pytest.fixture()
def settings():
    return Settings(falkordb_host="fake.cloud", falkordb_password=SECRET_PW, meta_api_key=None,
                    graph_prefix="ariadne_test")


@pytest.fixture()
def store(settings):
    s = FalkorStore(settings)
    s._db = FakeDB()
    return s


@pytest.fixture()
def client(settings, store, memory_queries):
    app = api_mod.app
    app.dependency_overrides[api_mod.get_settings] = lambda: settings
    app.dependency_overrides[api_mod.get_store] = lambda: store
    app.dependency_overrides[api_mod.repo_queries] = lambda repo_id: (
        memory_queries if repo_id == memory_queries.repo_id else (_ for _ in ()).throw(
            api_mod.RepoNotFound(repo_id)))
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_reports_presence_only(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["falkordb_configured"] is True
    assert body["config"]["meta_configured"] is False
    assert body["falkordb"]["reachable"] is True
    assert SECRET_PW not in r.text and "fake.cloud" not in r.text


def test_index_rejects_bad_urls(client):
    for url in ["http://github.com/a/b", "https://evil.com/a/b", "https://github.com/a/b;ls"]:
        r = client.post("/api/index", json={"url": url})
        assert r.status_code == 400, url


def test_index_requires_configured_db(client, settings):
    api_mod.app.dependency_overrides[api_mod.get_settings] = lambda: Settings()
    r = client.post("/api/index", json={"url": "https://github.com/acme/shop"})
    assert r.status_code == 503


def test_index_token_enforced(client):
    api_mod.app.dependency_overrides[api_mod.get_settings] = lambda: Settings(
        falkordb_host="fake", index_token="t0ken")
    assert client.post("/api/index", json={"url": "https://github.com/acme/shop"}).status_code == 401
    assert client.post("/api/index", json={"url": "https://github.com/acme/shop"},
                       headers={"X-Ariadne-Token": "wrong"}).status_code == 401
    assert client.delete("/api/repos/github__acme__shop").status_code == 401


WRITE_CALLS = [
    ("post", "/api/index", {"json": {"url": "https://github.com/acme/shop"}}),
    ("delete", "/api/repos/github__acme__shop", {}),
]


def _use_settings(**kw):
    api_mod.app.dependency_overrides[api_mod.get_settings] = lambda: Settings(falkordb_host="fake", **kw)


@pytest.mark.parametrize("on_vercel", [False, True])
@pytest.mark.parametrize("method,path,kw", WRITE_CALLS)
@pytest.mark.parametrize("header", [None, "", "wrong", "t0ke", "t0ken ", "T0KEN", "tökén", "t0ken "])
def test_write_endpoints_reject_missing_or_invalid_token(client, store, monkeypatch, on_vercel,
                                                         method, path, kw, header):
    _use_settings(index_token="t0ken", on_vercel=on_vercel)
    called = []
    monkeypatch.setattr("ariadne.api.run_index", lambda *a, **k: called.append(1) or iter(()))
    monkeypatch.setattr(store, "delete_repo", lambda rid: called.append(1) or True)
    # Raw UTF-8 bytes: the server sees non-ASCII header text (previously a TypeError → 500).
    headers = {} if header is None else {"X-Ariadne-Token": header.encode("utf-8")}
    r = getattr(client, method)(path, headers=headers, **kw)
    assert r.status_code == 401, r.text
    assert "t0ken" not in r.text
    assert called == []                         # the protected action never ran


@pytest.mark.parametrize("method,path,kw", WRITE_CALLS)
def test_write_endpoints_accept_valid_token(client, store, monkeypatch, method, path, kw):
    _use_settings(index_token="t0ken", on_vercel=True)
    monkeypatch.setattr("ariadne.api.run_index", lambda *a, **k: iter([{"stage": "done"}]))
    monkeypatch.setattr(store, "delete_repo", lambda rid: True)
    r = getattr(client, method)(path, headers={"X-Ariadne-Token": "t0ken"}, **kw)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("method,path,kw", WRITE_CALLS)
def test_write_endpoints_fail_closed_on_vercel_without_token(client, store, monkeypatch, method, path, kw):
    _use_settings(index_token=None, on_vercel=True)
    called = []
    monkeypatch.setattr("ariadne.api.run_index", lambda *a, **k: called.append(1) or iter(()))
    monkeypatch.setattr(store, "delete_repo", lambda rid: called.append(1) or True)
    for headers in ({}, {"X-Ariadne-Token": "anything"}):
        r = getattr(client, method)(path, headers=headers, **kw)
        assert r.status_code == 503 and "ARIADNE_INDEX_TOKEN" in r.text
    assert called == []


def test_writes_open_locally_without_token(client, store, monkeypatch):
    _use_settings(index_token=None, on_vercel=False)
    monkeypatch.setattr(store, "delete_repo", lambda rid: True)
    assert client.delete("/api/repos/github__acme__shop").status_code == 200


def test_token_checked_before_input_validation(client):
    _use_settings(index_token="t0ken")
    # A bad URL / bad repo id must not reveal anything to an unauthenticated caller.
    assert client.post("/api/index", json={"url": "https://evil.com/a/b"}).status_code == 401
    assert client.delete("/api/repos/BAD%20ID").status_code == 401


def test_read_endpoints_do_not_require_token(client, memory_queries):
    _use_settings(index_token="t0ken", on_vercel=True)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").json()["config"]["index_token_required"] is True


def _events(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_index_streams_progress_and_persists(client, store, fixture_fetch, monkeypatch):
    monkeypatch.setattr("ariadne.indexer.fetch_repository", lambda repo, limits, token: fixture_fetch)
    r = client.post("/api/index", json={"url": "https://github.com/acme/shop"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    ev = _events(r)
    stages = [e["stage"] for e in ev]
    assert stages[0] == "start" and stages[-1] == "done"
    assert {"fetch", "parse", "store"} <= set(stages)
    summary = ev[-1]["summary"]
    assert summary["repo_id"] == "github__acme__shop"
    assert summary["stats"]["files"] == 9 and summary["stats"]["functions"] == 18
    g = store._db.graphs["ariadne_test:github__acme__shop"]
    assert g["repo"]["status"] == "ready" and len(g["nodes"]) == 37

    # re-index: identical graph, no duplicates
    before = (dict(g["nodes"]), dict(g["edges"]))
    r2 = client.post("/api/index", json={"url": "https://github.com/acme/shop"})
    assert _events(r2)[-1]["stage"] == "done"
    g2 = store._db.graphs["ariadne_test:github__acme__shop"]
    assert (g2["nodes"], g2["edges"]) == before

    repos = client.get("/api/repos").json()["repos"]
    assert repos[0]["repo_id"] == "github__acme__shop" and repos[0]["stats"]["classes"] == 6


def test_index_fetch_error_is_reported_and_lock_released(client, store, monkeypatch):
    def boom(*a):
        raise FetchError("Repository acme/shop (or ref) was not found, or it is private.")
    monkeypatch.setattr("ariadne.indexer.fetch_repository", boom)
    ev = _events(client.post("/api/index", json={"url": "https://github.com/acme/shop"}))
    assert ev[-1]["stage"] == "error" and "not found" in ev[-1]["error"]
    assert store.list_repos() == []          # no stale "indexing" lock left behind


def test_unexpected_errors_do_not_leak_internals(client, monkeypatch):
    def boom(*a):
        raise RuntimeError(f"internal detail {SECRET_PW}")
    monkeypatch.setattr("ariadne.indexer.fetch_repository", boom)
    r = client.post("/api/index", json={"url": "https://github.com/acme/shop"})
    assert _events(r)[-1]["stage"] == "error" and SECRET_PW not in r.text


def test_read_endpoints(client):
    base = "/api/repos/github__acme__shop"
    ov = client.get(f"{base}/overview").json()
    assert ov["counts"]["Function"] == 18
    res = client.get(f"{base}/search", params={"q": "order", "kind": "class"}).json()["results"]
    assert res[0]["uid"] == "shop/models.py::Order"
    sym = client.get(f"{base}/symbol", params={"uid": "shop/models.py::Base.validate"}).json()
    assert {c["uid"] for c in sym["callers"]} == {"shop/models.py::Base.save", "shop/models.py::Order.validate"}
    assert sym["node"]["line_start"] == 11 and "def validate" in sym["node"]["source"]
    g = client.get(f"{base}/graph").json()
    assert g["nodes"] and g["edges"] and not g["truncated"]
    assert {e["type"] for e in client.get(f"{base}/graph", params={"rels": "EXTENDS"}).json()["edges"]} == {"EXTENDS"}
    assert len(client.get(f"{base}/graph", params={"limit": 2}).json()["nodes"]) == 2
    nb = client.get(f"{base}/neighbors", params={"uid": "shop/models.py::Order", "rels": "EXTENDS,DEFINES"}).json()
    assert {e["type"] for e in nb["edges"]} == {"EXTENDS", "DEFINES"}
    p = client.get(f"{base}/path", params={"src": "main.py::main", "dst": "shop/utils.py::fmt"}).json()
    assert p["found"]
    imp = client.get(f"{base}/impact", params={"uid": "shop/utils.py::fmt", "depth": 2}).json()
    assert "shop/utils.py::sum_items" in {n["uid"] for n in imp["impacted"]}


def test_ask_graph_only_without_key(client):
    r = client.post("/api/repos/github__acme__shop/ask", json={"question": "Who calls fmt?"})
    body = r.json()
    assert r.status_code == 200 and body["mode"] == "graph_only"
    assert body["evidence"][0]["path"] == "shop/utils.py"


def test_ask_uses_meta_when_configured(client, monkeypatch):
    from ariadne.meta_client import ChatResult

    class M:
        configured = True

        def chat(self, messages, **kw):
            return ChatResult("**Graph facts**\n- `sum_items` calls `fmt` [E2].", "muse-spark-1.3", {})
    api_mod.app.dependency_overrides[api_mod.get_meta] = lambda: M()
    body = client.post("/api/repos/github__acme__shop/ask", json={"question": "Who calls fmt?"}).json()
    assert body["mode"] == "model" and body["citations"] == ["E2"] and body["grounded"]


def test_errors(client):
    assert client.get("/api/repos/BAD ID!/overview").status_code in (400, 404)
    assert client.get("/api/repos/github__nope__x/overview").status_code == 404
    r = client.get("/api/repos/github__acme__shop/symbol", params={"uid": "missing"})
    assert r.status_code == 404
    r = client.get("/api/repos/github__acme__shop/neighbors", params={"uid": "main.py::main", "rels": "DROP"})
    assert r.status_code == 400
    r = client.get("/api/repos/github__acme__shop/graph", params={"rels": "DROP"})
    assert r.status_code == 400
    assert client.get("/api/repos/github__nope__x/graph").status_code == 404
    r = client.post("/api/repos/github__acme__shop/ask", json={"question": ""})
    assert r.status_code == 422


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Ariadne" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/../ariadne/config.py").status_code == 404
    assert client.get("/secret.txt").status_code == 404


def test_index_deadline_fails_cleanly(client, store, fixture_fetch, monkeypatch, settings):
    from dataclasses import replace
    api_mod.app.dependency_overrides[api_mod.get_settings] = lambda: replace(settings, index_deadline=0.0)
    monkeypatch.setattr("ariadne.indexer.fetch_repository", lambda repo, limits, token: fixture_fetch)
    ev = _events(client.post("/api/index", json={"url": "https://github.com/acme/shop"}))
    assert ev[-1]["stage"] == "error" and "time budget" in ev[-1]["error"]
    assert store.list_repos() == []          # nothing written; no stuck "indexing" lock


def test_root_redirects_when_ui_not_bundled(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api_mod, "PUBLIC_DIR", tmp_path)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/index.html"

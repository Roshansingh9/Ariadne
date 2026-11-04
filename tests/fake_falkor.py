"""Minimal fake of the FalkorDB client for persistence tests (mocked, not live).

It understands only Ariadne's write templates, and applies MERGE semantics
(upsert by uid / by (src, type, dst)) so re-index duplicate behavior can be
checked offline.
"""

from __future__ import annotations

import re

import redis.exceptions as rx

NODE_RE = re.compile(r"MERGE \(n:Entity:(\w+) \{uid: r\.uid\}\) SET n \+= r$")
EDGE_RE = re.compile(r"MERGE \(a\)-\[e:(\w+)\]->\(b\) SET e \+= r\.props$")


class _Result:
    def __init__(self, rows=None):
        self.result_set = rows or []


class FakeGraph:
    def __init__(self, db, name):
        self.db, self.name = db, name

    @property
    def data(self):
        return self.db.graphs.setdefault(self.name, {"nodes": {}, "edges": {}, "repo": {}, "log": []})

    def delete(self):
        if self.name not in self.db.graphs:
            raise rx.ResponseError("Invalid graph operation on empty key")
        del self.db.graphs[self.name]

    def query(self, cypher, params=None, timeout=None):
        if self.db.fail_on and self.db.fail_on in cypher:
            raise rx.ConnectionError("boom")
        params = params or {}
        d = self.data
        d["log"].append(cypher)
        if cypher.startswith("CREATE INDEX"):
            return _Result()
        if cypher.startswith("MERGE (r:Repository"):
            d["repo"].update(params["props"])
            d["repo"]["id"] = params["id"]
            return _Result()
        m = NODE_RE.search(cypher)
        if m:
            for row in params["rows"]:
                d["nodes"].setdefault(row["uid"], {"label": m.group(1)}).update(row)
            return _Result()
        m = EDGE_RE.search(cypher)
        if m:
            for row in params["rows"]:
                if row["src"] in d["nodes"] and row["dst"] in d["nodes"]:     # MATCH semantics
                    d["edges"].setdefault((row["src"], m.group(1), row["dst"]), {}).update(row["props"])
            return _Result()
        raise AssertionError(f"unexpected write: {cypher}")

    def ro_query(self, cypher, params=None, timeout=None):
        if cypher == "MATCH (r:Repository) RETURN r LIMIT 1":
            repo = self.db.graphs.get(self.name, {}).get("repo")
            return _Result([[{**repo, "labels": ["Repository"]}]] if repo else [])
        raise AssertionError(f"unexpected read: {cypher}")


class FakeConnection:
    def ping(self):
        return True


class FakeDB:
    def __init__(self):
        self.graphs: dict[str, dict] = {}
        self.connection = FakeConnection()
        self.fail_on: str | None = None

    def select_graph(self, name):
        return FakeGraph(self, name)

    def list_graphs(self):
        return list(self.graphs)

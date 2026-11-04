"""Test double: GraphQueries backed by an in-memory CodeGraph.

The database primitives (`node`, `nodes`, `_expand`, and the members query
used by impact) are reimplemented over Python data. `path()` and
`impact()` run the *production* BFS logic unchanged. `overview`, `search`,
`symbol` and `neighbors` are simple Python equivalents for API/QA tests.

The Cypher templates themselves are only exercised by the live FalkorDB
test (tests/test_live_falkordb.py).
"""

from __future__ import annotations

from collections import Counter

from ariadne.queries import GraphQueries, NodeNotFound, brief, validate_rels
from ariadne.store import node_rows


class MemoryQueries(GraphQueries):
    def __init__(self, graph, repo_id: str = "github__acme__shop"):
        super().__init__(store=None, repo_id=repo_id)
        self.graph = graph
        self.by_uid: dict[str, dict] = {}
        for label, rows in node_rows(graph).items():
            for r in rows:
                self.by_uid[r["uid"]] = {**r, "labels": [label]}
        self.edges = [(e.type, e.src, e.dst, dict(e.props)) for e in graph.edges]
        self.repo = {"id": repo_id, "url": "https://github.com/acme/shop", "name": "shop",
                     "owner": "acme", "status": "ready", "commit": "abc123"}

    # -- primitives ------------------------------------------------------
    def q(self, cypher, **params):
        if "-[:DEFINES*1..3]->(m) RETURN m.uid" in cypher:
            out, frontier = [], [params["uid"]]
            for _ in range(3):
                nxt = [d for t, s, d, _ in self.edges if t == "DEFINES" and s in frontier]
                out += nxt
                frontier = nxt
            return [[u] for u in out]
        raise AssertionError(f"MemoryQueries does not emulate: {cypher}")

    def ensure_exists(self):
        return dict(self.repo)

    def node(self, uid):
        if uid not in self.by_uid:
            raise NodeNotFound(f"No node with uid {uid!r}")
        return dict(self.by_uid[uid])

    def nodes(self, uids):
        return {u: dict(self.by_uid[u]) for u in uids if u in self.by_uid}

    def _edge_list(self, rels, cap):
        rows = [(s, t, p.get("confidence"), d) for t, s, d, p in self.edges if t in rels]
        return sorted(rows, key=lambda r: (r[0], r[1], r[3]))[:cap]

    def _expand(self, frontier, rels, reverse):
        fs = set(frontier)
        out = []
        for t, s, d, p in self.edges:
            if t not in rels:
                continue
            if not reverse and s in fs:
                out.append((s, t, p.get("confidence"), d))
            elif reverse and d in fs:
                out.append((d, t, p.get("confidence"), s))
        return sorted(out, key=lambda r: (r[0], r[1], r[3]))

    # -- simple Python equivalents ----------------------------------------
    def _rel(self, uid, types, outgoing):
        res = []
        for t, s, d, p in self.edges:
            if t in types and (s == uid if outgoing else d == uid):
                other = d if outgoing else s
                res.append({**brief(self.by_uid[other]), "edge": {"type": t, **p}})
        return sorted(res, key=lambda r: r["uid"])

    def overview(self):
        labels = Counter(n["labels"][0] for n in self.by_uid.values())
        edges: dict = {}
        for t, _, _, p in self.edges:
            d = edges.setdefault(t, {"total": 0})
            d["total"] += 1
            if p.get("confidence"):
                d[p["confidence"]] = d.get(p["confidence"], 0) + 1
        callers = Counter(d for t, s, d, _ in self.edges if t == "CALLS")
        hotspots = [{**brief(self.by_uid[u]), "callers": c} for u, c in callers.most_common(8)
                    if self.by_uid[u]["labels"] == ["Function"]]
        langs = Counter(n["language"] for n in self.by_uid.values() if n["labels"] == ["File"])
        return {"repo": dict(self.repo), "counts": dict(labels), "edges": edges, "languages": dict(langs),
                "hotspots": hotspots, "most_imported": [], "largest_files": [], "packages": [],
                "directories": []}

    def search(self, text, kind="all", limit=25):
        t = text.strip().lower()
        out = []
        for n in self.by_uid.values():
            lbl = n["labels"][0]
            if kind == "all" and lbl == "Package":
                continue
            if kind != "all" and lbl.lower() != kind:
                continue
            name, qual = n.get("name", "").lower(), (n.get("qualname") or "").lower()
            if t in name or t in qual or (lbl == "File" and t in n["path"].lower()):
                rank = 0 if t in (name, qual) else 1 if name.startswith(t) else 2
                out.append({**brief(n), "rank": rank})
        out.sort(key=lambda r: (r["rank"], len(r.get("name", "")), r["uid"]))
        return out[:limit]

    def symbol(self, uid, limit=60):
        n = self.node(uid)
        label = n["labels"][0]
        out = {
            "node": {k: v for k, v in n.items() if k != "labels"} | {"label": label},
            "defined_in": [brief(self.by_uid[s]) for t, s, d, _ in self.edges if t == "DEFINES" and d == uid],
            "children": [brief(self.by_uid[d]) for t, s, d, _ in self.edges if t == "DEFINES" and s == uid],
            "callers": self._rel(uid, {"CALLS"}, False)[:limit],
            "callees": self._rel(uid, {"CALLS"}, True)[:limit],
            "bases": self._rel(uid, {"EXTENDS", "IMPLEMENTS"}, True),
            "subclasses": self._rel(uid, {"EXTENDS", "IMPLEMENTS"}, False),
        }
        if label == "File":
            out["imports"] = self._rel(uid, {"IMPORTS"}, True)
            out["imported_by"] = self._rel(uid, {"IMPORTS"}, False)
            out["packages"] = self._rel(uid, {"DEPENDS_ON"}, True)
        return out

    def neighbors(self, uid, rels=None, direction="both", limit=80):
        rels = validate_rels(rels)
        center = self.node(uid)
        nodes = {uid: brief(center)}
        edges = []
        for t, s, d, p in self.edges:
            if t not in rels:
                continue
            if direction in ("out", "both") and s == uid:
                nodes.setdefault(d, brief(self.by_uid[d]))
                edges.append({"src": s, "dst": d, "type": t, **p})
            if direction in ("in", "both") and d == uid:
                nodes.setdefault(s, brief(self.by_uid[s]))
                edges.append({"src": s, "dst": d, "type": t, **p})
        return {"center": uid, "nodes": list(nodes.values()), "edges": edges[:limit]}

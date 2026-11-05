"""Read-side graph API.

Every query is a fixed template. User input enters only as parameters.
Labels and relationship types are interpolated **only** after allow-list
validation. Depths and limits are clamped. Path finding and impact analysis
are bounded breadth-first searches (one parameterized query per level), so
cycles or dense graphs can't blow up a variable-length match.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .model import EDGE_TYPES
from .store import RepoNotFound

SEARCH_KINDS = {"all": None, "file": "File", "class": "Class", "function": "Function",
                "package": "Package"}
TRAVERSAL_RELS = ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS", "DEFINES", "DEPENDS_ON")
IMPACT_RELS = ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS")
MAX_PATH_DEPTH = 8
MAX_IMPACT_DEPTH = 6
MAX_FRONTIER = 400
MAX_IMPACT_NODES = 300
MAX_IMPACT_TARGETS = 10
MAX_GRAPH_NODES = 400          # repository-graph view: nodes drawn at most
GRAPH_EDGE_SCAN = 20000        # repository-graph view: edges read at most to rank nodes
CONF_RANK = {"resolved": 3, "inferred": 2, "ambiguous": 1, None: 3}
RANK_CONF = {3: "resolved", 2: "inferred", 1: "ambiguous"}

class NodeNotFound(LookupError):
    pass


BRIEF_FIELDS = ("uid", "name", "qualname", "kind", "path", "line_start", "line_end",
                "signature", "language", "labels")


def clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def validate_rels(rels: Optional[Iterable[str]], allowed: tuple[str, ...] = TRAVERSAL_RELS) -> list[str]:
    if not rels:
        return list(allowed)
    out = []
    for r in rels:
        r = str(r).strip().upper()
        if not r:
            continue
        if r not in allowed or r not in EDGE_TYPES:
            raise ValueError(f"Unsupported relationship type: {r}")
        if r not in out:
            out.append(r)
    return out or list(allowed)


def label_of(node: dict) -> str:
    labels = node.get("labels") or []
    return labels[0] if labels else "Entity"


def brief(node: dict) -> dict:
    out = {k: node.get(k) for k in BRIEF_FIELDS if node.get(k) is not None}
    out["label"] = label_of(node)
    out.pop("labels", None)
    return out


def _edge_info(e: dict) -> dict:
    out = {"type": e.get("type")}
    for k in ("confidence", "resolution", "line", "lines"):
        if e.get(k) is not None:
            out[k] = e[k]
    return out


class GraphQueries:
    """Query helpers bound to one repository graph."""

    def __init__(self, store, repo_id: str):
        self.store = store
        self.repo_id = repo_id

    def q(self, cypher: str, **params) -> list[list]:
        return self.store.query(self.repo_id, cypher, params)

    def ensure_exists(self) -> dict:
        rows = self.q("MATCH (r:Repository) RETURN r LIMIT 1")
        if not rows:
            raise RepoNotFound(self.repo_id)
        meta = dict(rows[0][0])
        meta.pop("labels", None)
        return meta

    def node(self, uid: str) -> dict:
        rows = self.q("MATCH (n:Entity {uid: $uid}) RETURN n LIMIT 1", uid=uid)
        if not rows:
            raise NodeNotFound(f"No node with uid {uid!r}")
        return rows[0][0]

    def nodes(self, uids: list[str]) -> dict[str, dict]:
        if not uids:
            return {}
        rows = self.q("MATCH (n:Entity) WHERE n.uid IN $uids RETURN n", uids=list(uids))
        return {r[0]["uid"]: r[0] for r in rows}

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    def overview(self) -> dict:
        repo = self.ensure_exists()
        labels = Counter()
        for lbls, cnt in self.q("MATCH (n:Entity) RETURN labels(n), count(n)"):
            for lbl in lbls:
                if lbl != "Entity":
                    labels[lbl] += cnt
        edges: dict[str, dict] = {}
        for etype, conf, cnt in self.q("MATCH ()-[e]->() RETURN type(e), e.confidence, count(e)"):
            d = edges.setdefault(etype, {"total": 0})
            d["total"] += cnt
            if conf:
                d[conf] = d.get(conf, 0) + cnt
        languages = {lang: cnt for lang, cnt in
                     self.q("MATCH (f:File) RETURN f.language AS l, count(f) AS c ORDER BY c DESC")}
        hotspots = [
            {**brief(n), "callers": c} for n, c in self.q(
                "MATCH (c:Entity)-[:CALLS]->(fn:Function) WITH fn, count(DISTINCT c) AS callers "
                "RETURN fn, callers ORDER BY callers DESC, fn.uid LIMIT 8")]
        imported = [
            {"uid": u, "path": p, "importers": c} for u, p, c in self.q(
                "MATCH (a:File)-[:IMPORTS]->(f:File) WITH f, count(DISTINCT a) AS c "
                "RETURN f.uid, f.path, c ORDER BY c DESC, f.path LIMIT 8")]
        largest = [
            {"uid": u, "path": p, "symbols": c} for u, p, c in self.q(
                "MATCH (f:File)-[:DEFINES]->(s) WITH f, count(s) AS c "
                "RETURN f.uid, f.path, c ORDER BY c DESC, f.path LIMIT 8")]
        packages = [
            {"name": n, "language": lang, "files": c} for n, lang, c in self.q(
                "MATCH (f:File)-[:DEPENDS_ON]->(p:Package) WITH p, count(DISTINCT f) AS c "
                "RETURN p.name, p.language, c ORDER BY c DESC, p.name LIMIT 15")]
        dirs = Counter()
        for (path,) in self.q("MATCH (f:File) RETURN f.path"):
            dirs[path.rsplit("/", 1)[0] if "/" in path else "."] += 1
        return {
            "repo": repo,
            "counts": dict(labels),
            "edges": edges,
            "languages": languages,
            "hotspots": hotspots,
            "most_imported": imported,
            "largest_files": largest,
            "packages": packages,
            "directories": [{"dir": d, "files": c} for d, c in dirs.most_common(12)],
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, text: str, kind: str = "all", limit: int = 25) -> list[dict]:
        term = (text or "").strip().lower()
        if not term:
            return []
        if len(term) > 200:
            raise ValueError("Search term too long.")
        if kind not in SEARCH_KINDS:
            raise ValueError(f"kind must be one of {sorted(SEARCH_KINDS)}")
        label = SEARCH_KINDS[kind]
        label_filter = f"n:{label}" if label else "(n:File OR n:Class OR n:Function)"
        limit = clamp(limit, 1, 100, 25)
        rows = self.q(
            f"MATCH (n:Entity) WHERE {label_filter} AND (toLower(n.name) CONTAINS $t "
            "OR toLower(coalesce(n.qualname, '')) CONTAINS $t "
            "OR (n:File AND toLower(n.path) CONTAINS $t)) "
            "WITH n, CASE WHEN toLower(n.name) = $t OR toLower(coalesce(n.qualname, '')) = $t THEN 0 "
            "WHEN toLower(n.name) STARTS WITH $t THEN 1 ELSE 2 END AS rank "
            "RETURN n, rank ORDER BY rank, size(n.name), n.uid LIMIT $limit",
            t=term, limit=limit)
        return [{**brief(n), "rank": rank} for n, rank in rows]

    # ------------------------------------------------------------------
    # Symbol / file detail
    # ------------------------------------------------------------------
    def symbol(self, uid: str, limit: int = 60) -> dict:
        node = self.node(uid)
        limit = clamp(limit, 1, 200, 60)
        label = label_of(node)

        def rel(cypher: str):
            return [{**brief(n), "edge": _edge_info(e)} for n, e in self.q(cypher, uid=uid, limit=limit)]

        out = {
            "node": {k: v for k, v in node.items() if k != "labels"} | {"label": label},
            "defined_in": [brief(r[0]) for r in self.q(
                "MATCH (p:Entity)-[:DEFINES]->(n:Entity {uid: $uid}) RETURN p", uid=uid)],
            "children": [brief(r[0]) for r in self.q(
                "MATCH (n:Entity {uid: $uid})-[:DEFINES]->(c) RETURN c ORDER BY c.line_start LIMIT $limit",
                uid=uid, limit=limit)],
            "callers": rel("MATCH (c:Entity)-[e:CALLS]->(n:Entity {uid: $uid}) RETURN c, e "
                           "ORDER BY c.uid LIMIT $limit"),
            "callees": rel("MATCH (n:Entity {uid: $uid})-[e:CALLS]->(c) RETURN c, e "
                           "ORDER BY c.uid LIMIT $limit"),
            "bases": rel("MATCH (n:Entity {uid: $uid})-[e:EXTENDS|IMPLEMENTS]->(c) RETURN c, e LIMIT $limit"),
            "subclasses": rel("MATCH (c:Entity)-[e:EXTENDS|IMPLEMENTS]->(n:Entity {uid: $uid}) RETURN c, e "
                              "LIMIT $limit"),
        }
        if label == "File":
            out["imports"] = rel("MATCH (n:Entity {uid: $uid})-[e:IMPORTS]->(c) RETURN c, e "
                                 "ORDER BY c.path LIMIT $limit")
            out["imported_by"] = rel("MATCH (c:Entity)-[e:IMPORTS]->(n:Entity {uid: $uid}) RETURN c, e "
                                     "ORDER BY c.path LIMIT $limit")
            out["packages"] = rel("MATCH (n:Entity {uid: $uid})-[e:DEPENDS_ON]->(c) RETURN c, e "
                                  "ORDER BY c.name LIMIT $limit")
        if label == "Package":
            out["used_by"] = rel("MATCH (c:Entity)-[e:DEPENDS_ON]->(n:Entity {uid: $uid}) RETURN c, e "
                                 "ORDER BY c.path LIMIT $limit")
        return out

    def file_by_path(self, path: str) -> dict:
        return self.symbol(f"file:{path}")

    # ------------------------------------------------------------------
    # Repository graph (initial visualization)
    # ------------------------------------------------------------------
    def _edge_list(self, rels: list[str], cap: int) -> list[tuple]:
        """(src_uid, type, confidence, dst_uid) for edges of the given types, deterministic order."""
        rel_expr = "|".join(rels)
        return [tuple(r) for r in self.q(
            f"MATCH (a:Entity)-[e:{rel_expr}]->(b:Entity) "
            "RETURN a.uid, type(e), e.confidence, b.uid ORDER BY a.uid, type(e), b.uid LIMIT $cap",
            cap=cap)]

    def repo_graph(self, rels=None, limit: int = 150) -> dict:
        """The `limit` most-connected nodes over the selected relationship types,
        plus every selected edge between them. Bounded for large repositories."""
        rels = validate_rels(rels)
        limit = clamp(limit, 1, MAX_GRAPH_NODES, 150)
        raw = self._edge_list(rels, GRAPH_EDGE_SCAN + 1)
        scan_truncated = len(raw) > GRAPH_EDGE_SCAN
        raw = raw[:GRAPH_EDGE_SCAN]
        degree: Counter = Counter()
        for src, _, _, dst in raw:
            degree[src] += 1
            if dst != src:
                degree[dst] += 1
        keep = [u for u, _ in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
        found = self.nodes(keep)
        keep = [u for u in keep if u in found]
        kept = set(keep)
        edges = []
        for src, etype, conf, dst in raw:
            if src in kept and dst in kept:
                edges.append({"src": src, "dst": dst, "type": etype, **({"confidence": conf} if conf else {})})
        return {
            "nodes": [brief(found[u]) for u in keep],
            "edges": edges,
            "rels": rels,
            "total_nodes": len(degree),
            "total_edges": len(raw),
            "truncated": scan_truncated or len(degree) > len(keep),
        }

    # ------------------------------------------------------------------
    # Neighborhood (for visualization)
    # ------------------------------------------------------------------
    def neighbors(self, uid: str, rels=None, direction: str = "both", limit: int = 80) -> dict:
        rels = validate_rels(rels)
        if direction not in ("in", "out", "both"):
            raise ValueError("direction must be in, out or both")
        limit = clamp(limit, 1, 300, 80)
        center = self.node(uid)
        rel_expr = "|".join(rels)
        nodes = {center["uid"]: brief(center)}
        edges = []
        if direction in ("out", "both"):
            for e, m in self.q(f"MATCH (n:Entity {{uid: $uid}})-[e:{rel_expr}]->(m) RETURN e, m "
                               "ORDER BY m.uid LIMIT $limit", uid=uid, limit=limit):
                nodes.setdefault(m["uid"], brief(m))
                edges.append({"src": uid, "dst": m["uid"], **_edge_info(e)})
        if direction in ("in", "both"):
            for e, m in self.q(f"MATCH (m:Entity)-[e:{rel_expr}]->(n:Entity {{uid: $uid}}) RETURN e, m "
                               "ORDER BY m.uid LIMIT $limit", uid=uid, limit=limit):
                nodes.setdefault(m["uid"], brief(m))
                edges.append({"src": m["uid"], "dst": uid, **_edge_info(e)})
        return {"center": uid, "nodes": list(nodes.values()), "edges": edges}

    # ------------------------------------------------------------------
    # Bounded traversals
    # ------------------------------------------------------------------
    def _expand(self, frontier: list[str], rels: list[str], reverse: bool) -> list[tuple]:
        rel_expr = "|".join(rels)
        if reverse:
            cy = (f"UNWIND $frontier AS u MATCH (b:Entity {{uid: u}})<-[e:{rel_expr}]-(a:Entity) "
                  "RETURN b.uid, type(e), e.confidence, a.uid LIMIT $cap")
        else:
            cy = (f"UNWIND $frontier AS u MATCH (a:Entity {{uid: u}})-[e:{rel_expr}]->(b:Entity) "
                  "RETURN a.uid, type(e), e.confidence, b.uid LIMIT $cap")
        return [tuple(r) for r in self.q(cy, frontier=frontier, cap=MAX_FRONTIER * 10)]

    def path(self, src: str, dst: str, rels=None, max_depth: int = 6, undirected: bool = False) -> dict:
        rels = validate_rels(rels)
        max_depth = clamp(max_depth, 1, MAX_PATH_DEPTH, 6)
        ends = self.nodes([src, dst])
        if src not in ends or dst not in ends:
            raise NodeNotFound("Source or destination node not found.")

        def expand(frontier):
            out = [(a, t, c, b) for a, t, c, b in self._expand(frontier, rels, reverse=False)]
            if undirected:
                out += [(a, t, c, b) for a, t, c, b in self._expand(frontier, rels, reverse=True)]
            return out

        hops = bfs_path(src, dst, expand, max_depth)
        if hops is None:
            return {"found": False, "src": brief(ends[src]), "dst": brief(ends[dst]),
                    "max_depth": max_depth, "rels": rels}
        uids = [src] + [h["to"] for h in hops]
        info = self.nodes(uids)
        return {"found": True, "length": len(hops), "rels": rels,
                "nodes": [brief(info[u]) for u in uids if u in info], "hops": hops}

    def impact(self, uid: str, depth: int = 3, rels=None) -> dict:
        return self.impact_many([uid], depth, rels)

    def impact_many(self, uids: list[str], depth: int = 3, rels=None) -> dict:
        """Reverse impact from one or more targets. Read-only.

        Each Class/File target also seeds the members it defines; `seed_owner` maps every seed
        back to the target it belongs to, so paths can say "is defined in <target>"."""
        rels = validate_rels(rels, IMPACT_RELS)
        depth = clamp(depth, 1, MAX_IMPACT_DEPTH, 3)
        uids = list(dict.fromkeys(u for u in uids if u))
        if not uids:
            raise ValueError("No impact target given.")
        if len(uids) > MAX_IMPACT_TARGETS:
            raise ValueError(f"At most {MAX_IMPACT_TARGETS} impact targets at once.")
        found = self.nodes(uids)
        missing = [u for u in uids if u not in found]
        if missing:
            raise NodeNotFound(f"No node with uid {missing[0]!r}")
        targets = [found[u] for u in uids]
        seed_owner = {u: u for u in uids}
        for t in targets:
            if label_of(t) in ("Class", "File"):
                # A change to a class/file also affects the members it defines.
                for (m,) in self.q("MATCH (n:Entity {uid: $uid})-[:DEFINES*1..3]->(m) RETURN m.uid LIMIT 400", uid=t["uid"]):
                    seed_owner.setdefault(m, t["uid"])
        seeds = list(seed_owner)

        result, parents = bfs_impact_paths(seeds, lambda fr: self._expand_edges(fr, rels), depth)
        info = self.nodes(list(result.keys()) + seeds)
        for t in targets:
            info.setdefault(t["uid"], t)
        impacted = []
        for u, meta in sorted(result.items(), key=lambda kv: (kv[1]["depth"], kv[0])):
            if u not in info:
                continue
            impacted.append({**brief(info[u]), **meta, "parents": parents.get(u, [])})
        files = Counter(n.get("path") for n in impacted if n.get("path"))
        conf = Counter(n["confidence"] for n in impacted)
        from .impact import build_report
        report = build_report(self, targets, seed_owner, depth, rels,
                              {u: m for u, m in result.items() if u in info}, parents, info, self.ensure_exists())
        return {
            "target": brief(targets[0]),
            "targets": [brief(t) for t in targets],
            "depth": depth,
            "rels": rels,
            "seed_count": len(seeds),
            "impacted": impacted,
            "affected_files": [{"path": p, "symbols": c} for p, c in files.most_common()],
            "by_confidence": dict(conf),
            "truncated": len(result) >= MAX_IMPACT_NODES,
            "report": report,
        }

    # -- read-only lookups used by the impact report (overridden by the in-memory test double) --
    def _expand_edges(self, frontier: list[str], rels: list[str]) -> list[tuple]:
        """Reverse expansion with edge props: (to, type, confidence, from, {lines, syntax, event})."""
        rel_expr = "|".join(rels)
        rows = self.q(f"UNWIND $frontier AS u MATCH (b:Entity {{uid: u}})<-[e:{rel_expr}]-(a:Entity) "
                      "RETURN b.uid, type(e), e.confidence, a.uid, e.lines, e.line, e.syntax, e.event LIMIT $cap",
                      frontier=frontier, cap=MAX_FRONTIER * 10)
        return [(b, t, c, a, {"lines": lines or ([line] if line else None), "syntax": syn, "event": ev})
                for b, t, c, a, lines, line, syn, ev in rows]

    def repo_files(self) -> list[tuple]:
        return [(p, lang) for p, lang in self.q("MATCH (f:File) RETURN f.path, f.language")]

    def http_client_nodes(self, indexed_facts: bool) -> list[dict]:
        """Functions that may send HTTP requests to this app's routes.

        Graphs with indexed facts are queried on `http_calls`; older graphs are narrowed by
        source text and parsed in Python (frameworks.*_from_source)."""
        if indexed_facts:
            rows = self.q("MATCH (n:Function) WHERE n.http_calls IS NOT NULL RETURN n LIMIT 500")
        else:
            rows = self.q("MATCH (n:Function) WHERE n.source CONTAINS 'fetch(' OR n.source CONTAINS 'axios.' "
                          "OR n.source CONTAINS 'client.' RETURN n LIMIT 500")
        return [r[0] for r in rows]

    def layer_nodes(self, layer: str) -> list[dict]:
        """Nodes whose membership of `layer` is confirmed by syntax (route decorator, JSX, model base,
        external call…). Used to match concept words in impact scenarios. Read-only."""
        from . import frameworks as F
        from .impact import Facts
        version = int(self.ensure_exists().get("graph_version") or 1)
        facts = Facts(version)
        out = []
        for n in self._layer_candidates(layer, version >= 2):
            if any(l["layer"] == layer and l["basis"] == "syntax" for l in F.classify(n, facts.of(n, [], None))):
                out.append(n)
        return sorted(out, key=lambda n: n["uid"])

    def _layer_candidates(self, layer: str, indexed_facts: bool) -> list[dict]:
        decorated = ("MATCH (n:Function) WHERE n.decorators IS NOT NULL RETURN n LIMIT 300" if indexed_facts else
                     "MATCH (n:Function) WHERE n.language = 'python' AND n.source STARTS WITH '@' RETURN n LIMIT 300")
        cypher = {
            "HTTP route": decorated,
            "Background job / event handler": decorated,
            # Older graphs have no JSX facts: no component is *confirmed*, so none is offered.
            "UI component": "MATCH (n:Entity) WHERE n.renders_jsx = true RETURN n LIMIT 300" if indexed_facts else None,
            "Data model / schema": "MATCH (n:Class) RETURN n LIMIT 400",
            "External service / database": "MATCH (n:Function) WHERE size(n.external_calls) > 0 RETURN n LIMIT 400",
        }.get(layer)
        if layer == "API client":
            return self.http_client_nodes(indexed_facts)
        return [r[0] for r in self.q(cypher)] if cypher else []

    def upstream(self, uids: list[str], rels: list[str], depth: int) -> tuple[dict, dict, dict]:
        """Dependents of `uids` (e.g. matched API clients), with the same bounds as impact."""
        result, parents = bfs_impact_paths(uids, lambda fr: self._expand_edges(fr, rels), depth)
        return result, parents, self.nodes(list(result) + list(uids))


# --------------------------------------------------------------------------
# Pure BFS helpers (unit-tested without a database)
# --------------------------------------------------------------------------

ExpandFn = Callable[[list[str]], list[tuple]]


def bfs_path(src: str, dst: str, expand: ExpandFn, max_depth: int) -> Optional[list[dict]]:
    """Shortest path by hop count. `expand(frontier)` yields (from, rel, confidence, to)."""
    if src == dst:
        return []
    parent: dict[str, tuple] = {src: None}
    frontier = [src]
    for _ in range(max_depth):
        nxt = []
        for a, rel, conf, b in expand(frontier[:MAX_FRONTIER]):
            if b in parent:
                continue
            parent[b] = (a, rel, conf)
            if b == dst:
                hops = []
                cur = dst
                while parent[cur] is not None:
                    pa, pr, pc = parent[cur]
                    hops.append({"from": pa, "to": cur, "type": pr, "confidence": pc or "resolved"})
                    cur = pa
                return list(reversed(hops))
            nxt.append(b)
        if not nxt:
            return None
        frontier = sorted(set(nxt))
    return None


def bfs_impact(seeds: list[str], expand_reverse: ExpandFn, max_depth: int) -> dict[str, dict]:
    """Reverse reachability. Each impacted node gets its minimum depth, the edge
    that reached it, and the weakest confidence along the best path found."""
    return bfs_impact_paths(seeds, expand_reverse, max_depth)[0]


def bfs_impact_paths(seeds: list[str], expand_reverse: ExpandFn, max_depth: int) -> tuple[dict[str, dict], dict[str, list]]:
    """Like `bfs_impact`, and also every parent edge on the shortest-path DAG.

    `parents[u]` lists each edge `u -[type]-> to` where `to` sits one level closer to the
    seeds, so all callers and all shortest paths are kept. Each node is expanded once,
    which bounds the work and makes cycles harmless. Rows may carry a 5th element with
    edge props (lines, syntax, event)."""
    seed_set = set(seeds)
    best_rank = {s: 3 for s in seeds}
    out: dict[str, dict] = {}
    parents: dict[str, list] = {}
    frontier = sorted(seed_set)
    for depth in range(1, max_depth + 1):
        level: dict[str, dict] = {}
        level_parents: dict[str, list] = {}
        for row in expand_reverse(frontier[:MAX_FRONTIER]):
            target, rel, conf, src = row[:4]
            props = row[4] if len(row) > 4 and row[4] else {}
            if src in seed_set:
                continue
            edge = {"to": target, "type": rel, "confidence": conf or "resolved",
                    **{k: v for k, v in props.items() if v not in (None, [], "")}}
            if src in out:
                # Already reached (at this depth or shallower): keep the edge as an alternative
                # path (e.g. a route that calls the target directly *and* through a helper).
                alt = parents.setdefault(src, [])
                if not any(e["to"] == target and e["type"] == rel for e in alt):
                    alt.append({**edge, "alt": True})
                continue
            bucket = level_parents.setdefault(src, [])
            if not any(e["to"] == target and e["type"] == rel for e in bucket):
                bucket.append(edge)
            rank = min(best_rank.get(target, 3), CONF_RANK.get(conf, 1))
            cur = level.get(src)
            if cur is None or rank > cur["_rank"]:
                level[src] = {"depth": depth, "via": rel, "from": target, "_rank": rank}
        for u, meta in level.items():
            best_rank[u] = meta.pop("_rank")
            meta["confidence"] = RANK_CONF[best_rank[u]]
            out[u] = meta
            parents[u] = sorted(level_parents[u], key=lambda e: (e["to"], e["type"])) + parents.get(u, [])
            if len(out) >= MAX_IMPACT_NODES:
                return out, parents
        if not level:
            break
        frontier = sorted(level)
    return out, parents

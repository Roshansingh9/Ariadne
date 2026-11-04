"""FalkorDB Cloud persistence.

* One graph per repository: ``{prefix}:{repo_id}``.
* Writes are a deterministic *plan* of batched ``UNWIND … MERGE`` statements,
  built by `build_write_plan` (pure, unit-testable). `FalkorStore` runs it.
* Labels and relationship types in Cypher come only from fixed allow-lists;
  all values are parameters.
* Reads go through ``ro_query``, so the database itself rejects writes from
  the query layer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol

from .config import Settings
from .model import EDGE_TYPES, NODE_LABELS, CodeGraph

BATCH_SIZE = 500
REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,150}$")
INDEX_LOCK_SECONDS = 360


class StoreError(RuntimeError):
    """Database failure with a message that is safe to show users."""


class RepoNotFound(LookupError):
    pass


def check_repo_id(repo_id: str) -> str:
    if not isinstance(repo_id, str) or not REPO_ID_RE.match(repo_id):
        raise ValueError("Invalid repository id.")
    return repo_id


def graph_name(prefix: str, repo_id: str) -> str:
    if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", prefix):
        raise ValueError("FALKORDB_GRAPH_NAME must match [A-Za-z0-9_.-]{1,40}.")
    return f"{prefix}:{check_repo_id(repo_id)}"


# --------------------------------------------------------------------------
# Write plan
# --------------------------------------------------------------------------

@dataclass
class Statement:
    cypher: str
    params: dict
    kind: str          # "node:<Label>" | "edge:<TYPE>" | "repo"
    rows: int = 0


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i: i + size]


def _node_cypher(label: str) -> str:
    assert label in NODE_LABELS
    return f"UNWIND $rows AS r MERGE (n:Entity:{label} {{uid: r.uid}}) SET n += r"


def _edge_cypher(etype: str) -> str:
    assert etype in EDGE_TYPES
    return (f"UNWIND $rows AS r MATCH (a:Entity {{uid: r.src}}) MATCH (b:Entity {{uid: r.dst}}) "
            f"MERGE (a)-[e:{etype}]->(b) SET e += r.props")


REPO_UPSERT = "MERGE (r:Repository {id: $id}) SET r += $props"


def node_rows(graph: CodeGraph) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {lbl: [] for lbl in NODE_LABELS}
    for f in graph.files:
        rows["File"].append({
            "uid": f.uid, "path": f.path, "name": f.path.rsplit("/", 1)[-1],
            "ext": "." + f.path.rsplit(".", 1)[-1] if "." in f.path else "",
            "language": f.language, "loc": f.loc, "has_errors": f.has_errors,
            "unresolved_calls": f.unresolved_calls, "external_calls": f.external_calls,
        })
        for s in f.symbols:
            rows[s.label].append({
                "uid": s.uid, "name": s.name, "qualname": s.qualname, "kind": s.kind,
                "path": s.path, "language": s.language, "line_start": s.line_start,
                "line_end": s.line_end, "signature": s.signature, "docstring": s.docstring,
                "source": s.source, "unresolved_calls": s.unresolved_calls,
                "external_calls": s.external_calls,
            })
    for uid in sorted(graph.packages):
        p = graph.packages[uid]
        rows["Package"].append({"uid": uid, "name": p["name"], "language": p["language"]})
    return rows


def edge_rows(graph: CodeGraph) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {t: [] for t in EDGE_TYPES}
    for e in graph.edges:
        rows[e.type].append({"src": e.src, "dst": e.dst, "props": dict(e.props)})
    return rows


def build_write_plan(graph: CodeGraph, batch_size: int = BATCH_SIZE) -> list[Statement]:
    plan: list[Statement] = []
    for label, rows in node_rows(graph).items():
        for chunk in _chunks(rows, batch_size):
            plan.append(Statement(_node_cypher(label), {"rows": chunk}, f"node:{label}", len(chunk)))
    for etype, rows in edge_rows(graph).items():
        for chunk in _chunks(rows, batch_size):
            plan.append(Statement(_edge_cypher(etype), {"rows": chunk}, f"edge:{etype}", len(chunk)))
    return plan


# --------------------------------------------------------------------------
# Result conversion
# --------------------------------------------------------------------------

def to_python(value: Any) -> Any:
    """Convert FalkorDB result values (Node/Edge/Path) into plain dicts/lists."""
    try:
        from falkordb import Edge, Node, Path
    except ImportError:  # pragma: no cover
        Node = Edge = Path = ()  # type: ignore
    if isinstance(value, Node):
        d = dict(value.properties or {})
        d["labels"] = [lbl for lbl in (value.labels or []) if lbl != "Entity"]
        return d
    if isinstance(value, Edge):
        d = dict(value.properties or {})
        d["type"] = value.relation
        return d
    if isinstance(value, Path):
        return {"nodes": [to_python(n) for n in value.nodes()],
                "edges": [to_python(e) for e in value.edges()]}
    if isinstance(value, list):
        return [to_python(v) for v in value]
    if isinstance(value, dict):
        return {k: to_python(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------
# Store interface + FalkorDB implementation
# --------------------------------------------------------------------------

class Store(Protocol):
    def query(self, repo_id: str, cypher: str, params: Optional[dict] = None) -> list[list]: ...
    def list_repos(self) -> list[dict]: ...
    def repo_exists(self, repo_id: str) -> bool: ...


ProgressFn = Callable[[int, int, str], None]


class FalkorStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._db = None

    # -- connection ------------------------------------------------------
    def _client(self):
        if not self.settings.falkordb_configured:
            raise StoreError("FalkorDB is not configured (set FALKORDB_HOST and credentials).")
        if self._db is None:
            from falkordb import FalkorDB
            s = self.settings
            self._db = FalkorDB(
                host=s.falkordb_host, port=s.falkordb_port, username=s.falkordb_username,
                password=s.falkordb_password, ssl=s.falkordb_ssl,
                socket_timeout=s.falkordb_timeout, socket_connect_timeout=min(10.0, s.falkordb_timeout),
                health_check_interval=30,
            )
        return self._db

    def _graph(self, repo_id: str):
        return self._client().select_graph(graph_name(self.settings.graph_prefix, repo_id))

    def _wrap(self, fn: Callable[[], Any]) -> Any:
        import redis.exceptions as rx
        try:
            return fn()
        except StoreError:
            raise
        except rx.AuthenticationError as exc:
            raise StoreError("FalkorDB authentication failed. Check FALKORDB_USERNAME/PASSWORD.") from exc
        except rx.TimeoutError as exc:
            raise StoreError("FalkorDB request timed out.") from exc
        except rx.ConnectionError as exc:
            self._db = None
            raise StoreError("Cannot reach FalkorDB. Check FALKORDB_HOST, FALKORDB_PORT and FALKORDB_SSL.") from exc
        except rx.ResponseError as exc:
            raise StoreError(f"FalkorDB error: {str(exc)[:300]}") from exc

    def ping(self) -> bool:
        return bool(self._wrap(lambda: self._client().connection.ping()))

    # -- reads -----------------------------------------------------------
    def query(self, repo_id: str, cypher: str, params: Optional[dict] = None) -> list[list]:
        def run():
            res = self._graph(repo_id).ro_query(cypher, params or {}, timeout=int(self.settings.falkordb_timeout * 1000))
            return [to_python(row) for row in res.result_set]
        return self._wrap(run)

    def _graph_names(self) -> list[str]:
        return self._wrap(lambda: list(self._client().list_graphs()))

    def repo_exists(self, repo_id: str) -> bool:
        return graph_name(self.settings.graph_prefix, repo_id) in self._graph_names()

    def list_repos(self) -> list[dict]:
        prefix = self.settings.graph_prefix + ":"
        out = []
        for name in sorted(self._graph_names()):
            if not name.startswith(prefix):
                continue
            rid = name[len(prefix):]
            if not REPO_ID_RE.match(rid):
                continue
            rows = self.query(rid, "MATCH (r:Repository) RETURN r LIMIT 1")
            meta = rows[0][0] if rows else {}
            meta.pop("labels", None)
            out.append({"repo_id": rid, **meta})
        return out

    def get_repo(self, repo_id: str) -> Optional[dict]:
        if not self.repo_exists(repo_id):
            return None
        rows = self.query(repo_id, "MATCH (r:Repository) RETURN r LIMIT 1")
        if not rows:
            return None
        meta = rows[0][0]
        meta.pop("labels", None)
        return meta

    # -- writes ----------------------------------------------------------
    def _write(self, repo_id: str, cypher: str, params: dict):
        return self._wrap(lambda: self._graph(repo_id).query(cypher, params))

    def mark_indexing(self, repo_id: str, props: dict) -> Optional[dict]:
        """Take a soft per-repo lock. Returns the previous Repository props (or None).

        Raises StoreError if another indexing run appears to be in progress.
        """
        prev = self.get_repo(repo_id)
        if prev and prev.get("status") == "indexing" and \
                time.time() - float(prev.get("indexing_started") or 0) < INDEX_LOCK_SECONDS:
            raise StoreError("This repository is already being indexed. Try again in a few minutes.")
        self._write(repo_id, REPO_UPSERT, {"id": repo_id, "props": {
            **props, "status": "indexing", "indexing_started": time.time()}})
        return prev

    def restore_status(self, repo_id: str, prev: Optional[dict], error: str) -> None:
        """Called when indexing fails *before* the graph was replaced."""
        try:
            if prev:
                self._write(repo_id, REPO_UPSERT, {"id": repo_id, "props": {
                    "status": prev.get("status") or "ready", "last_error": error[:300]}})
            else:
                self.delete_repo(repo_id)
        except StoreError:
            pass

    def mark_failed(self, repo_id: str, error: str) -> None:
        try:
            self._write(repo_id, REPO_UPSERT, {"id": repo_id, "props": {
                "status": "failed", "last_error": error[:300]}})
        except StoreError:
            pass

    def delete_repo(self, repo_id: str) -> bool:
        if not self.repo_exists(repo_id):
            return False
        self._wrap(lambda: self._graph(repo_id).delete())
        return True

    def replace_graph(self, repo_id: str, repo_props: dict, graph: CodeGraph,
                      progress: Optional[ProgressFn] = None) -> dict:
        info: dict = {}
        for step in self.replace_graph_steps(repo_id, repo_props, graph):
            if step.get("final"):
                info = {k: v for k, v in step.items() if k != "final"}
            elif progress:
                progress(step["done"], step["total"], step["kind"])
        return info

    def replace_graph_steps(self, repo_id: str, repo_props: dict, graph: CodeGraph):
        """Drop the repo graph and write `graph` fresh (no stale or duplicate data).

        Yields progress dicts after each batch; the last one has ``final: True``.
        """
        plan = build_write_plan(graph)
        g = self._graph(repo_id)

        def drop():
            try:
                g.delete()
            except Exception as exc:  # graph may not exist yet
                if "empty key" not in str(exc).lower() and "invalid graph" not in str(exc).lower() \
                        and "not exist" not in str(exc).lower():
                    raise
        self._wrap(drop)
        self._wrap(lambda: g.query("CREATE INDEX FOR (n:Entity) ON (n.uid)"))
        self._write(repo_id, REPO_UPSERT, {"id": repo_id, "props": {
            **repo_props, "status": "indexing", "indexing_started": time.time()}})

        written = 0
        total = sum(s.rows for s in plan)
        for st in plan:
            self._write(repo_id, st.cypher, st.params)
            written += st.rows
            yield {"done": written, "total": total, "kind": st.kind}

        self._write(repo_id, REPO_UPSERT, {"id": repo_id, "props": {
            "status": "ready", "indexed_at": time.time(), "last_error": ""}})
        yield {"final": True, "statements": len(plan), "rows": total}


def stats_json(stats: dict) -> str:
    return json.dumps(stats, sort_keys=True)

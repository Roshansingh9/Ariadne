"""Impact report: paths, layers, flows and an explanation built only from graph facts.

Input is what `GraphQueries.impact` already has: the reverse-BFS result, every parent
edge on the shortest-path DAG, and the nodes involved. Extra read-only lookups (repo
files, HTTP-client candidates, an upstream BFS from matched API clients) go through the
`gq` object, so this module stays testable without a database.

Every statement carries its basis:
  * "graph"     — an edge in the code graph (with its own resolved/inferred/ambiguous confidence)
  * "syntax"    — an explicit construct (route decorator, fetch("/x"), JSX) read from code
  * "heuristic" — a naming/path convention or a URL match between client and route: *inferred*
Graph connectivity means *potential* impact. Nothing here claims that a change will break.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from . import frameworks as F

MAX_PATHS = 120
MAX_PATHS_PER_ROOT = 3
MAX_FLOWS = 20
MAX_EXPLAINED = 6
BRIEF_KEYS = ("uid", "name", "qualname", "kind", "path", "line_start", "line_end", "language", "signature")
VERBS = {"CALLS": "calls", "IMPORTS": "imports", "EXTENDS": "extends", "IMPLEMENTS": "implements",
         "MEMBER_OF": "is defined in", "HTTP": "sends a matching HTTP request to"}
ENTRY_LAYERS = ("HTTP route", "Background job / event handler", "UI page", "UI component",
                "UI event handler", "Script entry point", "Test", "API client (test)")


def _label(n: dict) -> str:
    labels = n.get("labels") or []
    return n.get("label") or (labels[0] if labels else "Entity")


def _name(n: dict) -> str:
    return n.get("qualname") or n.get("name") or n.get("path") or n.get("uid", "?")


def _loc(n: dict) -> str:
    if not n.get("path"):
        return ""
    if n.get("line_start"):
        end = f"-{n['line_end']}" if n.get("line_end") and n["line_end"] != n["line_start"] else ""
        return f"{n['path']}:{n['line_start']}{end}"
    return n["path"]


def _bases(n: dict) -> list[str]:
    m = re.match(r"\s*class\s+\w+\s*(?:\(([^)]*)\)|extends\s+([\w.]+))", n.get("signature") or "")
    if not m:
        return []
    raw = m.group(1) or m.group(2) or ""
    return [b.strip() for b in raw.split(",") if b.strip() and "=" not in b]


class Facts:
    """Architectural facts per node: from indexed properties (graph_version >= 2) or,
    for older graphs, derived from the stored source snippet (reported in coverage)."""

    def __init__(self, graph_version: int):
        self.v2 = graph_version >= 2
        self.derived = 0                       # nodes whose facts came from stored source

    def of(self, n: dict, event_wiring: list[str], module_entry: Optional[str]) -> dict:
        lang, src = n.get("language") or "", n.get("source") or ""
        if self.v2:
            decorators = list(n.get("decorators") or [])
            http_calls = list(n.get("http_calls") or [])
            jsx, jsx_known = bool(n.get("renders_jsx")), True
        else:
            decorators = F.decorators_from_source(src) if lang == "python" else []
            if lang == "python":
                http_calls = F.python_http_calls_from_source(src) if F.is_test_path(n.get("path", "")) else []
            else:
                http_calls = F.js_http_calls_from_source(src)
            jsx, jsx_known = False, False       # JSX cannot be told from TS generics in a text snippet
            if decorators or http_calls:
                self.derived += 1
        return {"decorators": decorators, "routes": F.python_routes(decorators), "jobs": F.python_jobs(decorators),
                "http_calls": http_calls, "jsx": jsx, "jsx_known": jsx_known,
                "event_handler_for": event_wiring, "module_entry": module_entry, "bases": _bases(n)}


def _paths(seed_owner: dict, parents: dict, info: dict, roots: list[str], max_len: int = 99) -> list[dict]:
    """Paths root → … → selected. Shortest-path edges first, then alternative edges found during
    the traversal; no node repeats (cycles), no path longer than the depth limit. Bounded."""
    out: list[dict] = []
    covered: set = set()
    seeds = set(seed_owner)

    def walk(u, nodes, edges, budget):
        if len(out) >= MAX_PATHS or budget[0] <= 0:
            return
        if u in seeds:
            n2, e2 = list(nodes), list(edges)
            owner = seed_owner.get(u, u)
            if owner != u:                         # a member of a selected class/file
                n2.append(owner)
                e2.append({"type": "MEMBER_OF", "confidence": "resolved"})
            out.append({"nodes": n2, "edges": e2})
            covered.update(n2)
            budget[0] -= 1
            return
        if len(edges) >= max_len:
            return
        for p in sorted(parents.get(u, []), key=lambda e: bool(e.get("alt"))):
            if p["to"] in nodes:                   # would revisit a node: a cycle
                continue
            walk(p["to"], nodes + [p["to"]], edges + [{k: v for k, v in p.items() if k not in ("to", "alt")}], budget)

    for r in roots:
        walk(r, [r], [], [MAX_PATHS_PER_ROOT])
    for u in sorted(set(parents) - covered, key=lambda x: (len(parents.get(x, [])), x)):  # every dependent on >= 1 path
        if u not in covered and len(out) < MAX_PATHS:
            walk(u, [u], [], [1])
    for p in out:
        p["root"] = p["nodes"][0]
        p["length"] = len(p["edges"])
        p["kind"] = "direct" if p["length"] == 1 or (p["length"] == 2 and p["edges"][-1]["type"] == "MEMBER_OF") else "transitive"
        p["confidence"] = "resolved" if all(e.get("confidence", "resolved") == "resolved" for e in p["edges"]) else \
            ("ambiguous" if any(e.get("confidence") == "ambiguous" for e in p["edges"]) else "inferred")
    return out


def _edge_phrase(edge: dict, nxt: str) -> str:
    if edge.get("syntax") == "jsx-render":
        return f"renders `{nxt}`"
    if edge.get("syntax") == "jsx-event":
        return f"wires `{nxt}` to {edge.get('event') or 'an event'}"
    return f"{VERBS.get(edge['type'], edge['type'].lower())} `{nxt}`"


def _chain(path: dict, names: dict, targets: set) -> str:
    parts = [f"`{names[path['nodes'][0]]}`"]
    for i, e in enumerate(path["edges"]):
        nxt = path["nodes"][i + 1]
        single = nxt in targets and len(targets) == 1
        phrase = _edge_phrase(e, "the selected symbol" if single else names.get(nxt, nxt))
        phrase = phrase.replace("`the selected symbol`", "the selected symbol")
        if nxt in targets and not single:
            phrase += " (a selected target)"
        parts.append(phrase if i == 0 else f"which {phrase}")
    return parts[0] + " " + ", ".join(parts[1:]) + "."


def build_report(gq, targets, seed_owner, depth: int, rels: list[str],
                 result: dict, parents: dict, info: dict, repo: dict) -> dict:
    if isinstance(targets, dict):                  # single target (older call style)
        targets = [targets]
    if not isinstance(seed_owner, dict):           # a list of seeds owned by the single target
        seed_owner = {u: targets[0]["uid"] for u in seed_owner} | {targets[0]["uid"]: targets[0]["uid"]}
    target = targets[0]
    target_uid = target["uid"]
    target_uids = [t["uid"] for t in targets]
    target_set = set(target_uids)
    seeds = list(seed_owner)
    seed_set = set(seeds)
    facts = Facts(int(repo.get("graph_version") or 1))
    files = gq.repo_files()                                   # [(path, language)]
    languages = Counter(lang for _, lang in files)
    frontend_files = [p for p, _ in files if F.is_frontend_path(p)]

    # --- per-node facts and layers ------------------------------------------------------
    wiring: dict[str, list[str]] = {}
    callers_of_file: dict[str, str] = {}
    for u, edges in parents.items():
        for e in edges:
            if e.get("syntax") == "jsx-event":
                wiring.setdefault(e["to"], []).append(f"{e.get('event', 'on?')} in `{_name(info.get(u, {'uid': u}))}`")
        if _label(info.get(u, {})) == "File" and any(e["type"] == "CALLS" for e in edges):
            callers_of_file[u] = info[u].get("path", u)

    node_facts, layers_of = {}, {}

    def describe(uid: str) -> dict:
        if uid not in node_facts:
            n = info.get(uid, {"uid": uid})
            fx = facts.of(n, wiring.get(uid, []), callers_of_file.get(uid))
            node_facts[uid] = fx
            layers_of[uid] = sorted(F.classify(n, fx), key=lambda l: F.layer_order(l["layer"]))
        return node_facts[uid]

    for u in [*target_uids, *seeds, *result]:
        describe(u)

    # --- dependency paths --------------------------------------------------------------
    depended_on = {e["to"] for es in parents.values() for e in es}
    roots = sorted((u for u in result if u not in depended_on),
                   key=lambda u: (-result[u]["depth"], F.layer_order((layers_of[u] or [{"layer": "~"}])[0]["layer"]), u))
    paths = _paths(seed_owner, parents, info, roots, max_len=depth)
    names = {u: _name(info.get(u, {"uid": u})) for u in {*result, *seeds, *target_uids}}

    direct = sorted(u for u, m in result.items() if m["depth"] == 1)
    transitive = sorted(u for u, m in result.items() if m["depth"] > 1)
    affected_files = Counter(info[u].get("path") for u in result if u in info and info[u].get("path"))

    # --- UI / API flows -------------------------------------------------------------------
    flows, links, ui_status, ui_message = [], [], "", ""
    route_nodes = [u for u in [*target_uids, *result] if node_facts[u]["routes"]
                   and not F.is_test_path(info.get(u, {}).get("path", ""))]     # a test app's routes are not UI targets
    graph_ui_paths = [p for p in paths if any(l["layer"] in ("UI page", "UI component", "UI event handler")
                                              for n in p["nodes"] for l in layers_of.get(n, []))]
    http_tests = []
    has_js = any(lang in ("javascript", "typescript", "tsx") for _, lang in files)
    clients = gq.http_client_nodes(facts.v2) if (route_nodes or has_js) else []
    js_clients = [c for c in clients if c.get("language") in ("javascript", "typescript", "tsx")
                  and not F.is_test_path(c.get("path", "")) and facts.of(c, [], None)["http_calls"]]
    if route_nodes:
        for c in clients:
            cf = facts.of(c, [], None)
            for call in cf["http_calls"]:
                for r in route_nodes:
                    for route in node_facts[r]["routes"]:
                        if F.route_matches(call, route):
                            link = {"client": c["uid"], "route_node": r, "call": call, "route": route,
                                    "basis": "heuristic", "rule": "client URL and method match the route path"}
                            info.setdefault(c["uid"], c)
                            describe(c["uid"])
                            names[c["uid"]] = _name(c)
                            (http_tests if F.is_test_path(c.get("path", "")) else links).append(link)
        # Upstream of each matched (non-test) API client: what UI reaches it, through graph edges.
        client_uids = sorted({l["client"] for l in links})
        up_result, up_parents, up_info = gq.upstream(client_uids, rels, depth) if client_uids else ({}, {}, {})
        for u, n in up_info.items():
            info.setdefault(u, n)
            names[u] = _name(n)
        for u, edges in up_parents.items():
            for e in edges:
                if e.get("syntax") == "jsx-event":
                    w = wiring.setdefault(e["to"], [])
                    msg = f"{e.get('event', 'on?')} in `{_name(info.get(u, {'uid': u}))}`"
                    if msg not in w:
                        w.append(msg)
        for u in {*up_result, *client_uids}:
            node_facts.pop(u, None)
            describe(u)
        backend_tail = {}
        for r in route_nodes:                   # route → … → selected, along the main DAG
            tail = next((p for p in paths if r in p["nodes"]), None)
            backend_tail[r] = ([r], []) if r in target_set else \
                (tail["nodes"][tail["nodes"].index(r):], tail["edges"][tail["nodes"].index(r):]) if tail else ([r], [])
        for link in links:
            c = link["client"]
            up_depended = {e["to"] for es in up_parents.values() for e in es}
            ups = [u for u in up_result if u not in up_depended] or [c]
            front = _paths({c: c}, up_parents, info, sorted(ups))[:3] if ups != [c] else [{"nodes": [c], "edges": []}]
            tail_nodes, tail_edges = backend_tail[link["route_node"]]
            for fp in front:
                if len(flows) >= MAX_FLOWS:
                    break
                flows.append({
                    "nodes": fp["nodes"] + tail_nodes,
                    "edges": fp["edges"] + [{"type": "HTTP", "basis": "heuristic", "confidence": "inferred",
                                             "detail": f"{link['call']} ⇢ {link['route']}"}] + tail_edges,
                    "link": link,
                })
    if flows or graph_ui_paths:
        ui_status = "established"
        ui_message = ("UI → API path(s) found. Graph edges are confirmed; the client→route hop is inferred from a URL match."
                      if flows else "UI code reaches the selected symbol through graph edges.")
    elif not frontend_files and not js_clients:
        ui_status, ui_message = "no_frontend", "UI impact not established — no frontend code found in this repository."
    else:
        ui_status, ui_message = "not_established", "UI/API relationship not established from indexed code."
    if not facts.v2 and ui_status != "no_frontend":
        ui_message += (" This graph was indexed before JSX render/event links existed, so UI components beyond "
                       "API clients and their direct callers are not traced; a normal re-index adds them.")

    # --- layers ---------------------------------------------------------------------------
    layer_groups: dict[str, list] = {}
    for u in sorted({*result, *(n for f in flows for n in f["nodes"])} - target_set):
        reach = "graph" if u in result else "via inferred HTTP link"
        for l in layers_of.get(u, []):
            layer_groups.setdefault(l["layer"], []).append({"uid": u, "basis": l["basis"], "evidence": l["evidence"],
                                                            "reach": reach})
    layers = [{"layer": k, "members": v, "confirmed": sum(1 for m in v if m["basis"] == "syntax"),
               "inferred": sum(1 for m in v if m["basis"] == "heuristic")}
              for k, v in sorted(layer_groups.items(), key=lambda kv: F.layer_order(kv[0]))]

    # --- behaviour / entry points ---------------------------------------------------------
    behaviors = []
    for r in roots + [u for u in direct if u not in roots]:
        for l in layers_of.get(r, []):
            if l["layer"] in ENTRY_LAYERS and not any(b["uid"] == r for b in behaviors):
                fx = node_facts[r]
                what = {"HTTP route": f"The HTTP route {', '.join(fx['routes'])} (`{names[r]}`)",
                        "Background job / event handler": f"The background job/event `{names[r]}` ({', '.join(fx['jobs'])})",
                        "Script entry point": f"Module-level code in {fx['module_entry']}",
                        "Test": f"The test `{names[r]}`", "API client (test)": f"The test `{names[r]}`",
                        }.get(l["layer"], f"The {l['layer'].lower()} `{names[r]}`")
                behaviors.append({"uid": r, "layer": l["layer"], "basis": l["basis"], "evidence": l["evidence"],
                                  "text": f"{what} reaches the selected symbol through {result[r]['depth']} graph hop(s)."})
    for f in flows[:5]:
        ui = f["nodes"][0]
        behaviors.append({"uid": ui, "layer": "UI flow", "basis": "heuristic", "evidence": f["link"]["rule"],
                          "text": f"`{names[ui]}` leads to `{names[f['link']['client']]}`, which sends {f['link']['call']}; "
                                  f"that matches the route {f['link']['route']} (inferred), which reaches the selected symbol."})

    # --- inspection -----------------------------------------------------------------------
    graph_tests = [u for u in result if any(l["layer"] == "Test" for l in layers_of.get(u, []))]
    tests = [{"uid": u, "basis": "graph", "evidence": f"a graph path reaches the selected symbol ({result[u]['depth']} hop(s))"}
             for u in sorted(graph_tests)]
    for t in http_tests:
        if t["client"] not in graph_tests and all(x["uid"] != t["client"] for x in tests):
            tests.append({"uid": t["client"], "basis": "heuristic",
                          "evidence": f"sends {t['call']}, matching route {t['route']} (URL match)"})
    inspection = {
        "callers": direct,
        "files": [{"path": p, "symbols": c} for p, c in affected_files.most_common(12)],
        "tests": tests,
        "tests_note": None if tests else
        f"No indexed test reaches the selected symbol through {', '.join(rels)} within depth {depth}. "
        "This does not mean the code is untested: tests may call it dynamically or through unanalysed paths.",
    }

    # --- explanation ----------------------------------------------------------------------
    tname = _name(target) if len(targets) == 1 else f"{len(targets)} targets"
    sentences = []
    if result:
        sentences.append({"basis": "graph", "text":
                          f"`{tname}` has {len(direct)} direct and {len(transitive)} transitive dependent(s) in "
                          f"{len(affected_files)} file(s), following {', '.join(rels)} up to depth {depth}."})
    else:
        sentences.append({"basis": "graph", "text":
                          f"No dependents of `{tname}` were found following {', '.join(rels)} up to depth {depth}."})
    best_per_root = {}
    for p in paths:                      # one sentence per entry point: its shortest, best-resolved path
        if p["root"] in roots:
            key = (p["confidence"] != "resolved", p["length"])
            if p["root"] not in best_per_root or key < best_per_root[p["root"]][0]:
                best_per_root[p["root"]] = (key, p)
    for p in [best_per_root[r][1] for r in roots if r in best_per_root][:MAX_EXPLAINED]:
        root_layers = [l for l in layers_of.get(p["root"], []) if l["layer"] in ENTRY_LAYERS]
        why = ""
        if root_layers:
            l0 = root_layers[0]["layer"]
            why = {"HTTP route": f" A change may affect the {'/'.join(node_facts[p['root']]['routes'])} request path.",
                   "Background job / event handler": " A change may affect this background job.",
                   "UI page": " A change may affect this page.", "UI component": " A change may affect this UI component.",
                   "UI event handler": " A change may affect this UI interaction.",
                   "Test": " This test exercises the path.", "Script entry point": " A change may affect this script."}.get(l0, "")
        caveat = "" if p["confidence"] == "resolved" else f" (includes {p['confidence']} call resolution)"
        sentences.append({"basis": "graph" if p["confidence"] == "resolved" else "graph-inferred",
                          "text": _chain(p, names, target_set) + why + caveat, "path": p["nodes"]})
    for f in flows[:3]:
        l = f["link"]
        sentences.append({"basis": "heuristic", "text":
                          f"`{names[l['client']]}` sends {l['call']}, which matches the route {l['route']} on "
                          f"`{names[l['route_node']]}` (inferred from the URL; not a graph edge)."})
    sentences.append({"basis": "caveat", "text":
                      "Graph connectivity shows potential impact, not verified breakage: signatures, behaviour and "
                      "runtime conditions are not analysed."})

    # --- coverage ---------------------------------------------------------------------------
    edge_conf = Counter(e.get("confidence", "resolved") for es in parents.values() for e in es)
    unresolved = sum(len(info.get(u, {}).get("unresolved_calls") or []) for u in {*target_uids, *result})
    coverage = {
        "graph_version": 2 if facts.v2 else 1,
        "facts_source": ("indexed syntax facts (decorators, HTTP calls, JSX)" if facts.v2 else
                         "derived from stored source snippets (first 60 lines of each symbol); JSX render/event edges "
                         "need a normal re-index of this repository"),
        "derived_nodes": facts.derived,
        "languages": dict(languages),
        "files_in_repo": len(files),
        "frontend_files": len(frontend_files),
        "api_clients_found": len(js_clients),
        "relationships": rels,
        "edge_confidence": dict(edge_conf),
        "unresolved_calls_in_scope": unresolved,
        "depth": depth,
        "truncated": len(result) >= 300,
        "not_analysed": F.UNSUPPORTED,
    }

    node_map = {}
    for u in {*target_uids, *seeds, *result, *(n for f in flows for n in f["nodes"])}:
        n = info.get(u, {"uid": u})
        fx = node_facts.get(u) or describe(u)
        node_map[u] = {**{k: n.get(k) for k in BRIEF_KEYS if n.get(k) is not None}, "label": _label(n),
                       "layers": layers_of.get(u, []),
                       **({"routes": fx["routes"]} if fx["routes"] else {}),
                       **({"http_calls": fx["http_calls"]} if fx["http_calls"] else {}),
                       **({"jobs": fx["jobs"]} if fx["jobs"] else {}),
                       "depth": result.get(u, {}).get("depth", 0)}

    return {
        "summary": {"target": target_uid, "targets": target_uids, "location": _loc(target),
                    "target_layers": layers_of.get(target_uid, []),
                    "direct": len(direct), "transitive": len(transitive), "total": len(result),
                    "files": len(affected_files), "depth": depth, "rels": rels, "seeds": len(seeds)},
        "nodes": node_map,
        "paths": paths,
        "layers": layers,
        "ui": {"status": ui_status, "message": ui_message, "flows": flows, "graph_paths": len(graph_ui_paths),
               "links": links},
        "behaviors": behaviors,
        "inspection": inspection,
        "explanation": sentences,
        "coverage": coverage,
    }

"""Static cross-file resolution: parsed files → graph edges.

Every CALLS / EXTENDS / IMPLEMENTS edge carries:
  * ``resolution`` — the strategy that produced it
  * ``confidence`` — ``resolved`` | ``inferred`` | ``ambiguous``

Anything that can't be linked is kept on the caller as ``unresolved_calls``
(or ``external_calls`` when it targets a known external package), so
nothing is silently dropped.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Union

from .model import CodeGraph, Edge, ImportBinding, ParsedFile, Symbol, file_uid, package_uid

MAX_AMBIGUOUS = 3
MAX_UNRESOLVED_PER_SCOPE = 40
_SELF_NAMES = {"self", "this", "cls"}
_SUPER_NAMES = {"super", "super()"}
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_CONF_RANK = {"resolved": 3, "inferred": 2, "ambiguous": 1}

# Method names too generic to link by name alone (builtins / stdlib / DOM / promises).
COMMON_METHOD_NAMES = {
    "get", "set", "put", "post", "add", "append", "extend", "insert", "remove", "pop", "push",
    "shift", "unshift", "update", "keys", "values", "items", "join", "split", "strip", "lstrip",
    "rstrip", "lower", "upper", "replace", "format", "startswith", "endswith", "find", "index",
    "count", "copy", "clear", "sort", "reverse", "map", "filter", "reduce", "forEach", "some",
    "every", "includes", "indexOf", "slice", "splice", "concat", "then", "catch", "finally",
    "toString", "valueOf", "call", "apply", "bind", "log", "info", "debug", "warn", "warning",
    "error", "exception", "critical", "read", "write", "close", "open", "send", "emit", "on",
    "off", "once", "next", "has", "delete", "setdefault", "encode", "decode", "json", "text",
    "exec", "execute", "query", "run", "start", "stop", "init", "load", "save", "dump", "dumps",
    "loads", "parse", "match", "search", "test", "flush", "len", "length", "size", "trim",
    "fetch", "resolve", "reject", "all", "create", "render", "cancel", "wait", "sleep",
    "assert", "assertEqual", "assertTrue", "assertFalse", "fail", "mock", "patch", "spy",
    "describe", "it", "expect", "toBe", "toEqual", "addEventListener", "querySelector",
    "getElementById", "createElement", "appendChild", "preventDefault", "stopPropagation",
    "setState", "useState", "useEffect", "default", "main", "handle", "handler", "process",
    "subscribe", "unsubscribe", "listen", "connect", "disconnect", "commit", "rollback",
}


# Language/runtime globals: calls on these are recorded as external "builtin:" calls.
BUILTIN_RECEIVERS = {
    "Math", "JSON", "console", "Object", "Array", "Promise", "Number", "String", "Date",
    "Reflect", "Symbol", "window", "document", "process", "globalThis", "navigator",
    "localStorage", "sessionStorage", "URL", "Intl", "Buffer", "crypto", "performance",
}


@dataclass(frozen=True)
class _FileTarget:
    path: str


@dataclass(frozen=True)
class _PkgTarget:
    name: str
    language: str


Target = Union[Symbol, _FileTarget, _PkgTarget, None]


def _py_module_parts(path: str) -> list[str]:
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _js_package_name(spec: str) -> str:
    if spec.startswith("node:"):
        return spec
    parts = spec.split("/")
    if spec.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


class Resolver:
    def __init__(self, files: list[ParsedFile]):
        self.files = sorted(files, key=lambda f: f.path)
        self.by_path = {f.path: f for f in self.files}
        self.sym_by_uid: dict[str, Symbol] = {}
        self.children: dict[Optional[str], dict[str, list[Symbol]]] = defaultdict(lambda: defaultdict(list))
        self.top_level: dict[str, dict[str, list[Symbol]]] = defaultdict(lambda: defaultdict(list))
        self.global_top: dict[str, list[Symbol]] = defaultdict(list)
        self.global_methods: dict[str, list[Symbol]] = defaultdict(list)
        self.methods: dict[str, dict[str, Symbol]] = defaultdict(dict)
        self.class_bases: dict[str, list[Symbol]] = {}

        for f in self.files:
            for s in f.symbols:
                self.sym_by_uid[s.uid] = s
                if s.parent_uid is None:
                    self.top_level[f.path][s.name].append(s)
                    self.global_top[s.name].append(s)
                else:
                    self.children[s.parent_uid][s.name].append(s)
                if s.class_uid:
                    self.methods[s.class_uid].setdefault(s.name, s)
                    self.global_methods[s.name].append(s)

        # Python dotted-module index (exact + suffix, first path wins deterministically).
        self.py_exact: dict[str, str] = {}
        self.py_suffix: dict[str, str] = {}
        for f in self.files:
            if f.language != "python":
                continue
            parts = _py_module_parts(f.path)
            if not parts:
                continue
            self.py_exact.setdefault(".".join(parts), f.path)
            for i in range(len(parts)):
                self.py_suffix.setdefault(".".join(parts[i:]), f.path)

        self.bindings: dict[str, dict[str, tuple[ImportBinding, Target]]] = {}
        self.graph = CodeGraph(files=self.files)
        self._edges: dict[tuple, Edge] = {}

    # ------------------------------------------------------------------
    # Module / import resolution
    # ------------------------------------------------------------------
    def _py_lookup(self, dotted: str) -> Optional[str]:
        if not dotted:
            return None
        return self.py_exact.get(dotted) or self.py_suffix.get(dotted)

    def _py_absolute(self, f: ParsedFile, b: ImportBinding) -> Optional[str]:
        if not b.level:
            return b.module
        parts = _py_module_parts(f.path)
        package = parts if f.path.endswith("__init__.py") else parts[:-1]
        if b.level - 1 > len(package):
            return None
        base = package[: len(package) - (b.level - 1)]
        return ".".join([*base, *([b.module] if b.module else [])])

    def _resolve_py_binding(self, f: ParsedFile, b: ImportBinding) -> Target:
        full = self._py_absolute(f, b)
        if full is None:
            return None
        if b.imported is None:                       # import a.b / from x import *
            path = self._py_lookup(full)
            if path:
                return _FileTarget(path)
            return None if b.level else _PkgTarget(full.split(".")[0], "python")
        # from M import N: N may be a submodule or a symbol in M.
        sub = self._py_lookup(f"{full}.{b.imported}" if full else b.imported)
        if sub and (b.level or full):
            return _FileTarget(sub)
        mod_path = self._py_lookup(full) if full else None
        if mod_path:
            syms = self.top_level[mod_path].get(b.imported)
            if syms:
                return syms[0]
            return _FileTarget(mod_path)
        if b.level:
            return None
        return _PkgTarget(full.split(".")[0], "python")

    def _js_file(self, base: str) -> Optional[str]:
        base = posixpath.normpath(base)
        if base.startswith("../") or base == "..":
            return None
        cands = [base]
        stem, ext = posixpath.splitext(base)
        if ext in _JS_EXTS:
            cands += [stem + e for e in _JS_EXTS]       # "./x.js" written for x.ts sources
        cands += [base + e for e in _JS_EXTS] + [f"{base}/index{e}" for e in _JS_EXTS]
        for c in cands:
            if c in self.by_path:
                return c
        return None

    def _resolve_js_module(self, f: ParsedFile, spec: str) -> Target:
        if spec.startswith("."):
            path = self._js_file(posixpath.join(posixpath.dirname(f.path), spec))
            return _FileTarget(path) if path else None
        if spec.startswith(("@/", "~/")):            # common tsconfig path aliases
            rest = spec[2:]
            path = self._js_file(f"src/{rest}") or self._js_file(rest)
            return _FileTarget(path) if path else None
        if spec.startswith("/"):
            return None
        return _PkgTarget(_js_package_name(spec), "javascript")

    def _resolve_js_binding(self, f: ParsedFile, b: ImportBinding) -> Target:
        target = self._resolve_js_module(f, b.module)
        if not isinstance(target, _FileTarget) or b.imported is None:
            return target
        tops = self.top_level[target.path]
        if b.imported == "default":
            syms = tops.get(b.local) or tops.get("default")
            if syms:
                return syms[0]
            return target
        syms = tops.get(b.imported)
        return syms[0] if syms else target

    def _build_bindings(self) -> None:
        for f in self.files:
            table: dict[str, tuple[ImportBinding, Target]] = {}
            for b in f.imports:
                if f.language == "python":
                    target = self._resolve_py_binding(f, b)
                else:
                    target = self._resolve_js_binding(f, b)
                # file-level dependency edges
                if isinstance(target, Symbol):
                    if target.path != f.path:
                        self._edge("IMPORTS", f.uid, file_uid(target.path), {"line": b.line})
                elif isinstance(target, _FileTarget):
                    if target.path != f.path:
                        self._edge("IMPORTS", f.uid, file_uid(target.path), {"line": b.line})
                elif isinstance(target, _PkgTarget):
                    puid = package_uid(target.language, target.name)
                    self.graph.packages.setdefault(puid, {"uid": puid, "name": target.name,
                                                          "language": target.language})
                    self._edge("DEPENDS_ON", f.uid, puid, {"line": b.line})
                if b.local:
                    table.setdefault(b.local, (b, target))
                # `import os.path` also binds `os`.
                if (f.language == "python" and b.is_namespace and "." in b.local
                        and b.local == b.module):
                    head = b.local.split(".")[0]
                    head_b = ImportBinding(local=head, module=head, imported=None, line=b.line,
                                           is_namespace=True)
                    table.setdefault(head, (head_b, self._resolve_py_binding(f, head_b)))
            self.bindings[f.path] = table

    # ------------------------------------------------------------------
    # Symbol lookup
    # ------------------------------------------------------------------
    def _scope_lookup(self, f: ParsedFile, scope: Optional[Symbol], name: str) -> list[Symbol]:
        cur = scope
        while cur is not None:
            if cur.label != "Class":
                found = self.children[cur.uid].get(name)
                if found:
                    return found
            cur = self.sym_by_uid.get(cur.parent_uid) if cur.parent_uid else None
        return self.top_level[f.path].get(name, [])

    def _enclosing_class(self, scope: Optional[Symbol]) -> Optional[Symbol]:
        cur = scope
        while cur is not None:
            if cur.label == "Class":
                return cur
            if cur.class_uid:
                return self.sym_by_uid.get(cur.class_uid)
            cur = self.sym_by_uid.get(cur.parent_uid) if cur.parent_uid else None
        return None

    def _class_chain(self, cls: Symbol, include_self: bool = True) -> list[Symbol]:
        out, seen, queue = [], {cls.uid}, [cls] if include_self else list(self.class_bases.get(cls.uid, []))
        if not include_self:
            seen.update(b.uid for b in queue)
        depth = 0
        while queue and depth < 6:
            nxt = []
            for c in queue:
                out.append(c)
                for b in self.class_bases.get(c.uid, []):
                    if b.uid not in seen:
                        seen.add(b.uid)
                        nxt.append(b)
            queue, depth = nxt, depth + 1
        return out

    def _method_in_chain(self, cls: Symbol, name: str, include_self: bool = True) -> Optional[Symbol]:
        for c in self._class_chain(cls, include_self):
            m = self.methods[c.uid].get(name)
            if m is not None:
                return m
        return None

    def _global(self, name: str, exclude_path: str, classes_only: bool = False):
        cands = [s for s in self.global_top.get(name, [])
                 if s.path != exclude_path and (not classes_only or s.label == "Class")]
        if len(cands) == 1:
            return cands, "inferred"
        if 1 < len(cands) <= MAX_AMBIGUOUS:
            return cands, "ambiguous"
        return [], None

    def _resolve_name(self, f: ParsedFile, scope: Optional[Symbol], name: str,
                      classes_only: bool = False):
        """Returns (targets, resolution, confidence) or ([], 'external:<pkg>' | None, None)."""
        local = [s for s in self._scope_lookup(f, scope, name)
                 if not classes_only or s.label == "Class"]
        if local:
            return local[:1], "same_file", "resolved"
        bound = self.bindings.get(f.path, {}).get(name)
        if bound:
            target = bound[1]
            if isinstance(target, Symbol):
                return [target], "import", "resolved"
            if isinstance(target, _PkgTarget):
                return [], f"external:{target.name}", None
            if isinstance(target, _FileTarget) or target is None:
                return [], None, None
        cands, conf = self._global(name, f.path, classes_only)
        if cands:
            return cands, "name_match", conf
        return [], None, None

    def _resolve_class_expr(self, f: ParsedFile, scope: Optional[Symbol], expr: str):
        expr = expr.split("<", 1)[0].strip()
        if "." in expr:
            recv, _, name = expr.rpartition(".")
            bound = self.bindings.get(f.path, {}).get(recv)
            if bound and isinstance(bound[1], _FileTarget):
                syms = [s for s in self.top_level[bound[1].path].get(name, []) if s.label == "Class"]
                if syms:
                    return syms[:1], "module_attr", "resolved"
            cands, conf = self._global(name, "", classes_only=True)
            return (cands, "name_match", conf) if cands else ([], None, None)
        return self._resolve_name(f, scope, expr, classes_only=True)

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------
    def _edge(self, etype: str, src: str, dst: str, props: dict) -> None:
        if src == dst and etype != "CALLS":
            return
        key = (etype, src, dst)
        existing = self._edges.get(key)
        if existing is None:
            props = dict(props)
            if "line" in props and etype == "CALLS":
                props["lines"] = [props["line"]]
            self._edges[key] = Edge(etype, src, dst, props)
            return
        p = existing.props
        if etype == "CALLS" and "line" in props:
            if props["line"] not in p.setdefault("lines", []) and len(p["lines"]) < 20:
                p["lines"].append(props["line"])
        new_c, old_c = props.get("confidence"), p.get("confidence")
        if new_c and old_c and _CONF_RANK[new_c] > _CONF_RANK[old_c]:
            p["confidence"], p["resolution"] = new_c, props.get("resolution")

    def _resolve_bases(self) -> None:
        for f in self.files:
            for s in f.symbols:
                if s.label != "Class":
                    continue
                resolved: list[Symbol] = []
                for etype, exprs in (("EXTENDS", s.bases), ("IMPLEMENTS", s.implements)):
                    for expr in exprs:
                        scope = self.sym_by_uid.get(s.parent_uid) if s.parent_uid else None
                        targets, how, conf = self._resolve_class_expr(f, scope, expr)
                        for t in targets:
                            if t.uid == s.uid:
                                continue
                            self._edge(etype, s.uid, t.uid, {"resolution": how, "confidence": conf})
                            if etype == "EXTENDS" and (conf == "resolved" or len(targets) == 1):
                                resolved.append(t)
                self.class_bases[s.uid] = resolved

    def _var_type(self, scope: Optional[Symbol], var: str) -> Optional[str]:
        cur = scope
        while cur is not None and cur.label != "Class":
            if var in cur.var_types:
                return cur.var_types[var]
            cur = self.sym_by_uid.get(cur.parent_uid) if cur.parent_uid else None
        return None

    def _resolve_call(self, f: ParsedFile, scope: Optional[Symbol], site) -> tuple[list, Optional[str], Optional[str], Optional[str]]:
        """Return (targets, resolution, confidence, external_label)."""
        recv, name = site.receiver, site.name
        if recv is None:
            targets, how, conf = self._resolve_name(f, scope, name, classes_only=site.is_new)
            if how and how.startswith("external:"):
                return [], None, None, f"{how[9:]}:{name}"
            if not targets and (name.endswith(("Error", "Exception", "Warning"))
                                or name in BUILTIN_RECEIVERS):
                return [], None, None, f"builtin:{name}"
            return targets, how, conf, None

        if recv in _SELF_NAMES:
            cls = self._enclosing_class(scope)
            if cls is not None:
                m = self._method_in_chain(cls, name)
                if m is not None:
                    return [m], "self_method", "resolved", None
            return [], None, None, None

        if recv in _SUPER_NAMES:
            cls = self._enclosing_class(scope)
            if cls is not None:
                m = self._method_in_chain(cls, name, include_self=False)
                if m is not None:
                    return [m], "super_method", "resolved", None
            return [], None, None, None

        bound = self.bindings.get(f.path, {}).get(recv)
        if bound is not None:
            target = bound[1]
            if isinstance(target, _FileTarget):
                syms = self.top_level[target.path].get(name)
                if syms:
                    return syms[:1], "module_attr", "resolved", None
                return [], None, None, None
            if isinstance(target, Symbol) and target.label == "Class":
                m = self._method_in_chain(target, name)
                if m is not None:
                    return [m], "class_method", "resolved", None
                return [], None, None, None
            if isinstance(target, _PkgTarget):
                return [], None, None, f"{target.name}:{recv}.{name}"

        if recv.isidentifier():
            local = [s for s in self._scope_lookup(f, scope, recv) if s.label == "Class"]
            if local:
                m = self._method_in_chain(local[0], name)
                if m is not None:
                    return [m], "class_method", "resolved", None
            # Local variable assigned from a constructor: `x = Foo()` / `const x = new Foo()`.
            type_expr = self._var_type(scope, recv)
            if type_expr:
                classes, _, _ = self._resolve_class_expr(f, scope, type_expr)
                if len(classes) == 1:
                    m = self._method_in_chain(classes[0], name)
                    if m is not None:
                        return [m], "local_type", "inferred", None
            if recv in BUILTIN_RECEIVERS:
                return [], None, None, f"builtin:{recv}.{name}"

        if name not in COMMON_METHOD_NAMES and len(name) >= 3 and not name.startswith("__"):
            cands = self.global_methods.get(name, [])
            if len(cands) == 1:
                return cands, "name_match", "inferred", None
        return [], None, None, None

    def _resolve_calls(self) -> None:
        for f in self.files:
            scopes: list[tuple[Optional[Symbol], list]] = [(None, f.module_calls)]
            scopes += [(s, s.calls) for s in f.symbols]
            for scope, calls in scopes:
                src = scope.uid if scope else f.uid
                unresolved: list[str] = []
                external: list[str] = []
                for site in calls:
                    targets, how, conf, ext = self._resolve_call(f, scope, site)
                    if ext:
                        if ext not in external:
                            external.append(ext)
                        continue
                    if not targets:
                        label = f"{site.receiver}.{site.name}" if site.receiver else site.name
                        label = label[-60:]
                        if label not in unresolved:
                            unresolved.append(label)
                        continue
                    for t in targets:
                        self._edge("CALLS", src, t.uid,
                                   {"line": site.line, "resolution": how, "confidence": conf})
                holder = scope if scope is not None else f
                holder.unresolved_calls = unresolved[:MAX_UNRESOLVED_PER_SCOPE]
                holder.external_calls = external[:MAX_UNRESOLVED_PER_SCOPE]

    def _definitions(self) -> None:
        for f in self.files:
            for s in f.symbols:
                parent = s.parent_uid or f.uid
                self._edge("DEFINES", parent, s.uid, {})

    def run(self) -> CodeGraph:
        self._definitions()
        self._build_bindings()
        self._resolve_bases()
        self._resolve_calls()
        self.graph.edges = sorted(self._edges.values(), key=lambda e: (e.type, e.src, e.dst))
        return self.graph


def resolve(files: list[ParsedFile]) -> CodeGraph:
    return Resolver(files).run()

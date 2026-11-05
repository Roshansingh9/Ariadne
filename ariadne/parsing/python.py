"""Python extraction: classes, functions/methods, calls, imports, bases."""

from __future__ import annotations

import re
from typing import Optional

from tree_sitter import Node

from .. import frameworks
from ..model import CallSite, ImportBinding, ParsedFile, Symbol
from .base import SymbolBuilder, get_parser, one_line, text

_PY_STRING = re.compile(r"""^[rRbBuUfF]{0,2}(['"]{1,3})(.*)\1$""", re.S)

_SKIP_CALLS = {"super", "print", "len", "isinstance", "range", "str", "int", "dict", "list",
               "set", "tuple", "bool", "float", "repr", "type", "getattr", "setattr",
               "hasattr", "enumerate", "zip", "sorted", "reversed", "min", "max", "sum",
               "any", "all", "open", "iter", "next", "map", "filter", "id", "hash",
               "callable", "vars", "issubclass", "abs", "round", "format", "object"}


def _docstring(body: Optional[Node]) -> str:
    if body is None or body.named_child_count == 0:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_child_count:
        s = first.named_children[0]
        if s.type == "string":
            raw = text(s)
            for q in ('"""', "'''", '"', "'"):
                if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                    raw = raw[len(q):-len(q)]
                    break
            return raw.strip()
    return ""


def _signature(node: Node) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        return text(node).split("\n", 1)[0]
    head = node.text[: body.start_byte - node.start_byte].decode("utf-8", "replace")
    return head.rstrip().rstrip(":")


class _PyVisitor:
    def __init__(self, parsed: ParsedFile, lines: list[str]):
        self.p = parsed
        self.b = SymbolBuilder(parsed, lines)

    def visit(self, node: Node, scope: Optional[Symbol], cls: Optional[Symbol]) -> None:
        t = node.type
        if t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                decorators = [one_line(text(c), 160) for c in node.named_children if c.type == "decorator"]
                sym = self._definition(inner, node, scope, cls)
                if sym is not None:
                    sym.decorators = decorators[:8]
                # decorators may contain calls
                for c in node.named_children:
                    if c.type == "decorator":
                        self.visit(c, scope, cls)
            return
        if t in ("function_definition", "class_definition"):
            self._definition(node, node, scope, cls)
            return
        if t == "call":
            self._call(node, scope)
        elif t == "assignment" and scope is not None:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if (left is not None and left.type == "identifier" and right is not None
                    and right.type == "call"):
                fn = right.child_by_field_name("function")
                if fn is not None and fn.type in ("identifier", "attribute"):
                    scope.var_types.setdefault(text(left), text(fn))
        elif t == "import_statement":
            self._import(node)
        elif t == "import_from_statement":
            self._import_from(node)
        for c in node.named_children:
            self.visit(c, scope, cls)

    def _definition(self, node: Node, outer: Node, scope: Optional[Symbol], cls: Optional[Symbol]) -> Optional[Symbol]:
        name = text(node.child_by_field_name("name"))
        if not name:
            return None
        qual = f"{scope.qualname}.{name}" if scope else name
        body = node.child_by_field_name("body")
        if node.type == "class_definition":
            sym = self.b.make(label="Class", kind="class", name=name, qualname=qual, node=outer,
                              signature=_signature(node), docstring=_docstring(body), parent=scope)
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                for arg in supers.named_children:
                    if arg.type in ("identifier", "attribute"):
                        sym.bases.append(text(arg))
            if body is not None:
                for c in body.named_children:
                    self.visit(c, sym, sym)
            return sym
        is_method = scope is not None and scope.label == "Class"
        sym = self.b.make(label="Function", kind="method" if is_method else "function", name=name,
                          qualname=qual, node=outer, signature=_signature(node),
                          docstring=_docstring(body), parent=scope,
                          class_sym=scope if is_method else None)
        for field in ("parameters", "return_type"):
            sub = node.child_by_field_name(field)
            if sub is not None:
                self.visit(sub, sym, cls)       # default-value calls
        if body is not None:
            self.visit(body, sym, cls)
        return sym

    def _call(self, node: Node, scope: Optional[Symbol]):
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point.row + 1
        if fn.type == "identifier":
            name = text(fn)
            if name in _SKIP_CALLS:
                return
            site = CallSite(name=name, receiver=None, line=line)
        elif fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            attr = fn.child_by_field_name("attribute")
            if attr is None:
                return
            receiver = one_line(text(obj), 80) if obj is not None else None
            if obj is not None and obj.type == "call" and text(obj.child_by_field_name("function")) == "super":
                receiver = "super()"
            site = CallSite(name=text(attr), receiver=receiver, line=line)
            self._http_call(node, receiver, text(attr), scope)
        else:
            return
        (scope.calls if scope else self.p.module_calls).append(site)

    def _http_call(self, node: Node, receiver: Optional[str], method: str, scope: Optional[Symbol]) -> None:
        """`client.get("/api/x")`, `requests.post(...)`: an HTTP call with a literal URL."""
        if scope is None or method not in frameworks.HTTP_METHODS or not receiver:
            return
        if not re.search(r"(client|requests|httpx|session)$", receiver):
            return
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if first is None or first.type not in ("string", "concatenated_string"):
            return
        m = _PY_STRING.match(text(first))
        call = frameworks.http_call(method, m.group(2)) if m else None
        if call and call not in scope.http_calls:
            scope.http_calls.append(call)

    def _import(self, node: Node):
        line = node.start_point.row + 1
        for child in node.named_children:
            if child.type == "dotted_name":
                mod = text(child)
                self.p.imports.append(ImportBinding(local=mod, module=mod, imported=None,
                                                    line=line, is_namespace=True))
            elif child.type == "aliased_import":
                mod = text(child.child_by_field_name("name"))
                alias = text(child.child_by_field_name("alias"))
                self.p.imports.append(ImportBinding(local=alias or mod, module=mod, imported=None,
                                                    line=line, is_namespace=True))

    def _import_from(self, node: Node):
        line = node.start_point.row + 1
        mod_node = node.child_by_field_name("module_name")
        level, base = 0, ""
        if mod_node is not None:
            if mod_node.type == "relative_import":
                prefix = next((c for c in mod_node.children if c.type == "import_prefix"), None)
                level = len(text(prefix)) if prefix is not None else 1
                dn = next((c for c in mod_node.named_children if c.type == "dotted_name"), None)
                base = text(dn)
            else:
                base = text(mod_node)
        names = node.children_by_field_name("name")
        if not names:
            # `from x import *` — record the module dependency only.
            self.p.imports.append(ImportBinding(local="", module=base, imported=None,
                                                line=line, level=level))
            return
        for n in names:
            if n.type == "aliased_import":
                imported = text(n.child_by_field_name("name"))
                local = text(n.child_by_field_name("alias")) or imported
            else:
                imported = text(n)
                local = imported
            self.p.imports.append(ImportBinding(local=local, module=base, imported=imported,
                                                line=line, level=level))


def parse_python(path: str, data: bytes) -> ParsedFile:
    tree = get_parser("python").parse(data)
    src = data.decode("utf-8", errors="replace")
    lines = src.splitlines()
    parsed = ParsedFile(path=path, language="python", loc=len(lines),
                        has_errors=tree.root_node.has_error)
    _PyVisitor(parsed, lines).visit(tree.root_node, None, None)
    return parsed

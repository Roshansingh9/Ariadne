"""JavaScript / TypeScript extraction (shared visitor; grammars differ slightly)."""

from __future__ import annotations

import re

from typing import Optional

from tree_sitter import Node

from .. import frameworks
from ..model import CallSite, ImportBinding, ParsedFile, Symbol
from .base import SymbolBuilder, get_parser, one_line, text

_FUNC_DECL = {"function_declaration", "generator_function_declaration"}
_CLASS_DECL = {"class_declaration", "abstract_class_declaration"}
_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_FIELD_DEFS = {"field_definition", "public_field_definition"}
_SKIP_CALLS = {"require", "import", "parseInt", "parseFloat", "String", "Number", "Boolean",
               "Array", "Object", "Symbol", "setTimeout", "setInterval", "clearTimeout",
               "clearInterval", "Promise", "Error", "isNaN"}


def _string_value(node: Optional[Node]) -> Optional[str]:
    if node is None or node.type not in ("string", "template_string"):
        return None
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    if frag is not None:
        return text(frag)
    raw = text(node)
    return raw[1:-1] if len(raw) >= 2 else None


def _leading_comment(node: Node) -> str:
    target = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
    prev = target.prev_named_sibling
    if prev is not None and prev.type == "comment" and prev.end_point.row >= target.start_point.row - 1:
        raw = text(prev).strip()
        if raw.startswith("/**") or raw.startswith("/*"):
            raw = raw.lstrip("/*").rstrip("*/")
            raw = "\n".join(line.strip().lstrip("*").strip() for line in raw.splitlines())
        elif raw.startswith("//"):
            raw = raw[2:]
        return raw.strip()
    return ""


def _signature(node: Node) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        return text(node).split("\n", 1)[0]
    return node.text[: body.start_byte - node.start_byte].decode("utf-8", "replace").rstrip()


def _signature_between(outer: Node, fn_node: Node) -> str:
    """Text from the start of the declaration up to the function body."""
    body = fn_node.child_by_field_name("body")
    if body is None or body.start_byte < outer.start_byte:
        return text(outer).split("\n", 1)[0]
    head = outer.text[: body.start_byte - outer.start_byte].decode("utf-8", "replace").rstrip()
    return head.removesuffix("=>").rstrip() if body.type != "statement_block" else head


class _JsVisitor:
    def __init__(self, parsed: ParsedFile, lines: list[str]):
        self.p = parsed
        self.b = SymbolBuilder(parsed, lines)

    # -- traversal -------------------------------------------------------
    def visit(self, node: Node, scope: Optional[Symbol], cls: Optional[Symbol]) -> None:
        t = node.type
        if t in _FUNC_DECL:
            self._function(node, text(node.child_by_field_name("name")), node, scope, cls, "function")
            return
        if t in _CLASS_DECL or (t == "class" and node.parent is not None
                                and node.parent.type == "export_statement"):
            self._class(node, scope)
            return
        if t == "interface_declaration":
            self._interface(node, scope)
            return
        if t == "method_definition":
            name = text(node.child_by_field_name("name"))
            self._function(node, name, node, scope, cls, "method")
            return
        if t in _FIELD_DEFS:
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNC_VALUES:
                name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
                self._function(value, text(name_node), node, scope, cls, "method")
                return
        if t == "variable_declarator":
            if self._declarator(node, scope, cls):
                return
        if t in ("jsx_element", "jsx_self_closing_element", "jsx_fragment") and scope is not None:
            scope.renders_jsx = True
        if t in ("jsx_opening_element", "jsx_self_closing_element"):
            self._jsx(node, scope)
        if t == "call_expression":
            self._call(node, scope)
        elif t == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                self._add_call(scope, CallSite(name=text(ctor), receiver=None,
                                               line=node.start_point.row + 1, is_new=True))
            elif ctor is not None and ctor.type == "member_expression":
                self._add_call(scope, CallSite(
                    name=text(ctor.child_by_field_name("property")),
                    receiver=one_line(text(ctor.child_by_field_name("object")), 80),
                    line=node.start_point.row + 1, is_new=True))
        elif t == "import_statement":
            self._import(node)
        elif t == "export_statement":
            src = _string_value(node.child_by_field_name("source"))
            if src:
                self.p.imports.append(ImportBinding(local="", module=src, imported=None,
                                                    line=node.start_point.row + 1))
        for c in node.named_children:
            self.visit(c, scope, cls)

    # -- definitions -----------------------------------------------------
    def _function(self, fn_node: Node, name: str, outer: Node, scope: Optional[Symbol],
                  cls: Optional[Symbol], kind: str) -> None:
        if not name:
            for c in fn_node.named_children:
                self.visit(c, scope, cls)
            return
        qual = f"{scope.qualname}.{name}" if scope else name
        is_method = kind == "method" and scope is not None and scope.label == "Class"
        sym = self.b.make(label="Function", kind="method" if is_method else kind, name=name,
                          qualname=qual, node=outer, signature=_signature_between(outer, fn_node),
                          docstring=_leading_comment(outer), parent=scope,
                          class_sym=scope if is_method else None)
        for c in fn_node.named_children:
            if c.type != "identifier" or c != fn_node.child_by_field_name("name"):
                self.visit(c, sym, cls)

    def _class(self, node: Node, scope: Optional[Symbol]) -> None:
        name = text(node.child_by_field_name("name")) or "default"
        qual = f"{scope.qualname}.{name}" if scope else name
        sym = self.b.make(label="Class", kind="class", name=name, qualname=qual, node=node,
                          signature=_signature(node), docstring=_leading_comment(node), parent=scope)
        for c in node.named_children:
            if c.type == "class_heritage":
                for h in c.named_children:
                    if h.type == "extends_clause":                    # TypeScript
                        v = h.child_by_field_name("value")
                        if v is not None:
                            sym.bases.append(text(v))
                    elif h.type == "implements_clause":
                        for it in h.named_children:
                            if it.type in ("type_identifier", "identifier", "nested_type_identifier"):
                                sym.implements.append(text(it))
                            elif it.type == "generic_type":
                                sym.implements.append(text(it.named_children[0]))
                    elif h.type in ("identifier", "member_expression"):  # JavaScript
                        sym.bases.append(text(h))
                    elif h.type == "call_expression":   # mixins: extends mix(A)
                        self.visit(h, scope, None)
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.named_children:
                self.visit(c, sym, sym)

    def _interface(self, node: Node, scope: Optional[Symbol]) -> None:
        name = text(node.child_by_field_name("name"))
        if not name:
            return
        qual = f"{scope.qualname}.{name}" if scope else name
        sym = self.b.make(label="Class", kind="interface", name=name, qualname=qual, node=node,
                          signature=_signature(node), docstring=_leading_comment(node), parent=scope)
        for c in node.named_children:
            if c.type == "extends_type_clause":
                for it in c.named_children:
                    if it.type == "generic_type" and it.named_child_count:
                        sym.bases.append(text(it.named_children[0]))
                    else:
                        sym.bases.append(text(it))

    def _declarator(self, node: Node, scope: Optional[Symbol], cls: Optional[Symbol]) -> bool:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if value is None:
            return False
        if (value.type == "new_expression" and scope is not None and name_node is not None
                and name_node.type == "identifier"):
            ctor = value.child_by_field_name("constructor")
            if ctor is not None and ctor.type in ("identifier", "member_expression"):
                scope.var_types.setdefault(text(name_node), text(ctor))
        # const x = require("mod") / const {a, b: c} = require("mod")
        if value.type == "call_expression" and text(value.child_by_field_name("function")) == "require":
            spec = self._first_string_arg(value)
            if spec is not None:
                line = node.start_point.row + 1
                if name_node is not None and name_node.type == "identifier":
                    self.p.imports.append(ImportBinding(local=text(name_node), module=spec,
                                                        imported=None, line=line, is_namespace=True))
                elif name_node is not None and name_node.type == "object_pattern":
                    for prop in name_node.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            n = text(prop)
                            self.p.imports.append(ImportBinding(local=n, module=spec, imported=n, line=line))
                        elif prop.type == "pair_pattern":
                            k = text(prop.child_by_field_name("key"))
                            v = text(prop.child_by_field_name("value"))
                            self.p.imports.append(ImportBinding(local=v or k, module=spec, imported=k, line=line))
                else:
                    self.p.imports.append(ImportBinding(local="", module=spec, imported=None, line=line))
                return True
        if value.type in _FUNC_VALUES and name_node is not None and name_node.type == "identifier":
            self._function(value, text(name_node), node.parent if node.parent is not None and
                           node.parent.type in ("lexical_declaration", "variable_declaration") else node,
                           scope, cls, "arrow" if value.type == "arrow_function" else "function")
            return True
        return False

    # -- calls & imports -------------------------------------------------
    @staticmethod
    def _first_string_arg(call: Node) -> Optional[str]:
        args = call.child_by_field_name("arguments")
        if args is None or args.named_child_count == 0:
            return None
        return _string_value(args.named_children[0])

    def _add_call(self, scope: Optional[Symbol], site: CallSite) -> None:
        if site.name:
            (scope.calls if scope else self.p.module_calls).append(site)

    def _jsx(self, node: Node, scope: Optional[Symbol]) -> None:
        """`<OrderForm/>` renders a component; `onClick={handleSubmit}` wires an event handler.

        Recorded as JSX references (CallSite.jsx), so the resolver can link them like calls.
        Lowercase tags (`<div>`) are DOM elements, not components, and are skipped."""
        line = node.start_point.row + 1
        name = node.child_by_field_name("name")
        if name is not None:
            if name.type == "identifier" and text(name)[:1].isupper():
                self._add_call(scope, CallSite(name=text(name), receiver=None, line=line, jsx="render"))
            elif name.type in ("member_expression", "nested_identifier"):
                obj, _, prop = text(name).rpartition(".")
                if prop[:1].isupper():
                    self._add_call(scope, CallSite(name=prop, receiver=obj, line=line, jsx="render"))
        for attr in node.named_children:
            if attr.type != "jsx_attribute" or not attr.named_children:
                continue
            attr_name = text(attr.named_children[0])
            if not re.match(r"^on[A-Z]", attr_name) or len(attr.named_children) < 2:
                continue
            value = attr.named_children[1]
            if value.type != "jsx_expression" or not value.named_children:
                continue
            ref = value.named_children[0]
            if ref.type == "identifier":
                self._add_call(scope, CallSite(name=text(ref), receiver=None, line=line, jsx="event", event=attr_name))
            elif ref.type == "member_expression":
                prop = ref.child_by_field_name("property")
                obj = ref.child_by_field_name("object")
                if prop is not None:
                    self._add_call(scope, CallSite(name=text(prop), receiver=one_line(text(obj), 80) if obj is not None else None,
                                                   line=line, jsx="event", event=attr_name))

    def _http_call(self, node: Node, method: str, scope: Optional[Symbol]) -> None:
        """fetch("/api/x", {method: "POST"}) / axios.post("/api/x"): an HTTP call with a literal URL."""
        if scope is None:
            return
        args = node.child_by_field_name("arguments")
        if args is None or not args.named_children:
            return
        first = args.named_children[0]
        if first.type not in ("string", "template_string"):
            return
        url = text(first)[1:-1]
        if method == "fetch":
            opt = re.search(r"""method\s*:\s*['"`](\w+)['"`]""", text(args.named_children[1])) \
                if len(args.named_children) > 1 else None
            method = opt.group(1) if opt else "GET"
        call = frameworks.http_call(method, url)
        if call and call not in scope.http_calls:
            scope.http_calls.append(call)

    def _call(self, node: Node, scope: Optional[Symbol]) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point.row + 1
        if fn.type == "identifier":
            name = text(fn)
            if name == "require":
                spec = self._first_string_arg(node)
                if spec is not None and not (node.parent is not None and node.parent.type == "variable_declarator"):
                    self.p.imports.append(ImportBinding(local="", module=spec, imported=None, line=line))
                return
            if name == "fetch":
                self._http_call(node, "fetch", scope)
            if name in _SKIP_CALLS:
                return
            self._add_call(scope, CallSite(name=name, receiver=None, line=line))
        elif fn.type == "import":   # dynamic import("x")
            spec = self._first_string_arg(node)
            if spec is not None:
                self.p.imports.append(ImportBinding(local="", module=spec, imported=None, line=line))
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            obj = fn.child_by_field_name("object")
            if prop is None:
                return
            receiver = one_line(text(obj), 80) if obj is not None else None
            if receiver == "axios" and text(prop) in frameworks.HTTP_METHODS:
                self._http_call(node, text(prop), scope)
            self._add_call(scope, CallSite(name=text(prop), receiver=receiver, line=line))

    def _import(self, node: Node) -> None:
        spec = _string_value(node.child_by_field_name("source"))
        if spec is None:
            return
        line = node.start_point.row + 1
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            self.p.imports.append(ImportBinding(local="", module=spec, imported=None, line=line))
            return
        for c in clause.named_children:
            if c.type == "identifier":
                self.p.imports.append(ImportBinding(local=text(c), module=spec, imported="default", line=line))
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    self.p.imports.append(ImportBinding(local=text(ident), module=spec, imported=None,
                                                        line=line, is_namespace=True))
            elif c.type == "named_imports":
                for spec_node in c.named_children:
                    if spec_node.type != "import_specifier":
                        continue
                    name = text(spec_node.child_by_field_name("name"))
                    alias = text(spec_node.child_by_field_name("alias"))
                    self.p.imports.append(ImportBinding(local=alias or name, module=spec,
                                                        imported=name, line=line))


def parse_javascript(path: str, data: bytes, language: str = "javascript") -> ParsedFile:
    tree = get_parser(language).parse(data)
    src = data.decode("utf-8", errors="replace")
    lines = src.splitlines()
    parsed = ParsedFile(path=path, language=language if language != "tsx" else "typescript",
                        loc=len(lines), has_errors=tree.root_node.has_error)
    _JsVisitor(parsed, lines).visit(tree.root_node, None, None)
    return parsed

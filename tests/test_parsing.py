from ariadne.parsing import parse_file


def syms(parsed):
    return {s.uid: s for s in parsed.symbols}


def test_python_entities_lines_and_metadata():
    src = b'''"""mod doc"""
import os
import pkg.sub as ps
from .models import Order as O, Base
from .. import helpers
from x import *


@decorator(arg())
def top(a: int, b=default()) -> str:
    """Top-level doc."""
    def inner():
        helper()
    return os.path.join(a)


class Box(Base, mixins.Mixin, metaclass=Meta):
    def put(self, item):
        self.items.append(item)
        o = O()
        o.save()

    def put(self, item, extra):
        pass
'''
    p = parse_file("pkg/mod.py", src)
    s = syms(p)
    assert set(s) == {"pkg/mod.py::top", "pkg/mod.py::top.inner", "pkg/mod.py::Box",
                      "pkg/mod.py::Box.put", "pkg/mod.py::Box.put#L23"}
    top = s["pkg/mod.py::top"]
    assert (top.line_start, top.line_end) == (9, 14)          # 1-based, includes decorator
    assert top.signature == "def top(a: int, b=default()) -> str"
    assert top.docstring == "Top-level doc."
    assert top.kind == "function" and top.parent_uid is None
    assert {c.name for c in top.calls} >= {"default", "join"}
    # decorators execute in the enclosing (module) scope
    assert {"decorator", "arg"} <= {c.name for c in p.module_calls}
    assert "helper" not in {c.name for c in top.calls}         # attributed to inner
    assert [c.name for c in s["pkg/mod.py::top.inner"].calls] == ["helper"]
    join = next(c for c in top.calls if c.name == "join")
    assert join.receiver == "os.path" and join.line == 14

    box = s["pkg/mod.py::Box"]
    assert box.bases == ["Base", "mixins.Mixin"]               # keyword args ignored
    put = s["pkg/mod.py::Box.put"]
    assert put.kind == "method" and put.class_uid == box.uid
    assert put.var_types == {"o": "O"}
    assert ("save", "o") in {(c.name, c.receiver) for c in put.calls}
    assert "def put(self, item)" in put.source

    imps = {(b.local, b.module, b.imported, b.level, b.is_namespace) for b in p.imports}
    assert ("os", "os", None, 0, True) in imps
    assert ("ps", "pkg.sub", None, 0, True) in imps
    assert ("O", "models", "Order", 1, False) in imps
    assert ("Base", "models", "Base", 1, False) in imps
    assert ("helpers", "", "helpers", 2, False) in imps
    assert ("", "x", None, 0, False) in imps                   # star import: dependency only


def test_python_tolerates_syntax_errors():
    p = parse_file("bad.py", b"def ok():\n    return 1\n\ndef broken(:\n    pass\n")
    assert p.has_errors
    assert "bad.py::ok" in syms(p)


def test_javascript_entities_imports_and_calls():
    src = b'''import def, {a as b, c} from "./x";
import * as ns from "../y/index.js";
import "./side.css";
const r = require("./z");
const {p, q: qq} = require("lib");
export { k } from "./kk";

// Adds things.
export function add(x) { return helper(x) + ns.run(); }
const arrow = async (y) => { b(); };
const fexpr = function named() { new Thing(1); };
class A extends mod.Base {
  f = () => { this.g(); };
  static s() { super.s(); console.log(1); }
  g() { const t = new Thing(); t.go(); }
}
'''
    p = parse_file("src/m.js", src)
    s = syms(p)
    assert set(s) == {"src/m.js::add", "src/m.js::arrow", "src/m.js::fexpr", "src/m.js::A",
                      "src/m.js::A.f", "src/m.js::A.s", "src/m.js::A.g"}
    assert s["src/m.js::add"].docstring == "Adds things."
    assert s["src/m.js::arrow"].kind == "arrow"
    assert s["src/m.js::arrow"].signature == "const arrow = async (y) =>"
    assert s["src/m.js::A.f"].kind == "method" and s["src/m.js::A.f"].class_uid == "src/m.js::A"
    assert s["src/m.js::A"].bases == ["mod.Base"]
    assert {(c.name, c.receiver) for c in s["src/m.js::add"].calls} == {("helper", None), ("run", "ns")}
    assert [(c.name, c.is_new) for c in s["src/m.js::fexpr"].calls] == [("Thing", True)]
    assert ("g", "this") in {(c.name, c.receiver) for c in s["src/m.js::A.f"].calls}
    assert s["src/m.js::A.g"].var_types == {"t": "Thing"}

    imps = {(b.local, b.module, b.imported, b.is_namespace) for b in p.imports}
    assert {("def", "./x", "default", False), ("b", "./x", "a", False), ("c", "./x", "c", False),
            ("ns", "../y/index.js", None, True), ("", "./side.css", None, False),
            ("r", "./z", None, True), ("p", "lib", "p", False), ("qq", "lib", "q", False),
            ("", "./kk", None, False)} <= imps
    assert "require" not in {c.name for sym in p.symbols for c in sym.calls}


def test_typescript_classes_interfaces_and_implements():
    src = b'''export class Client extends Base<T> implements Fetcher, Other<U> {
  public handler = (e: Event) => this.handle(e);
  constructor(private y: number) { super(); }
  handle(e: Event): void {}
}
export interface Fetcher extends Base2 { request(id: string): Promise<unknown>; }
abstract class Q {}
export default class {}
'''
    p = parse_file("web/c.ts", src)
    s = syms(p)
    assert s["web/c.ts::Client"].bases == ["Base"]
    assert s["web/c.ts::Client"].implements == ["Fetcher", "Other"]
    assert s["web/c.ts::Fetcher"].kind == "interface" and s["web/c.ts::Fetcher"].bases == ["Base2"]
    assert "web/c.ts::Q" in s and "web/c.ts::default" in s
    assert s["web/c.ts::Client.handler"].kind == "method"
    assert p.language == "typescript"


def test_tsx_parses():
    p = parse_file("ui/App.tsx", b"export function App() { return <div onClick={() => go()}>hi</div>; }\n")
    assert [x.uid for x in p.symbols] == ["ui/App.tsx::App"]
    assert [c.name for c in p.symbols[0].calls] == ["go"]

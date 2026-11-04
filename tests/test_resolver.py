from ariadne.parsing import parse_file
from ariadne.resolver import resolve
from tests.conftest import edge_set


def test_fixture_calls_with_confidence(fixture_graph):
    calls = edge_set(fixture_graph, "CALLS")
    expected_resolved = {
        ("file:main.py", "main.py::main"),                                       # module-level call
        ("main.py::main", "shop/service.py::OrderService"),                      # import
        ("shop/models.py::Base.save", "shop/models.py::Base.validate"),          # self method
        ("shop/models.py::Order.validate", "shop/models.py::Base.validate"),     # super()
        ("shop/models.py::Order.total", "shop/utils.py::sum_items"),             # relative import
        ("shop/service.py::OrderService.place", "shop/models.py::Order"),        # instantiation
        ("shop/service.py::OrderService.place", "shop/utils.py::dump"),          # module attribute
        ("shop/service.py::OrderService.place", "shop/service.py::OrderService.notify"),
        ("shop/utils.py::sum_items", "shop/utils.py::fmt"),                      # same file
        ("web/api.ts::Client.getOrder", "web/api.ts::Client.request"),          # this.method
        ("web/api.ts::showPrice", "web/format.js::formatPrice"),                 # TS → JS import
        ("web/format.js::formatPrice", "web/format.js::round"),
        ("web/index.js::boot", "web/api.ts::Client"),                            # require + new
    }
    for src, dst in expected_resolved:
        assert ("CALLS", src, dst, "resolved") in calls, (src, dst)
    # heuristics are labeled as such
    assert ("CALLS", "shop/service.py::OrderService.place", "shop/models.py::Base.save", "inferred") in calls
    assert ("CALLS", "web/index.js::boot", "web/api.ts::Client.getOrder", "inferred") in calls
    assert ("CALLS", "main.py::main", "shop/service.py::OrderService.place", "inferred") in calls
    assert len(calls) == 17


def test_fixture_structure_edges(fixture_graph):
    es = edge_set(fixture_graph)
    assert ("EXTENDS", "shop/models.py::Order", "shop/models.py::Base", "resolved") in es
    assert ("EXTENDS", "web/api.ts::Client", "web/base.ts::BaseClient", "resolved") in es
    assert ("IMPLEMENTS", "web/api.ts::Client", "web/base.ts::Fetcher", "resolved") in es
    imports = {(s, d) for t, s, d, _ in es if t == "IMPORTS"}
    assert imports == {
        ("file:main.py", "file:shop/service.py"), ("file:shop/__init__.py", "file:shop/models.py"),
        ("file:shop/models.py", "file:shop/utils.py"), ("file:shop/service.py", "file:shop/models.py"),
        ("file:shop/service.py", "file:shop/utils.py"), ("file:web/api.ts", "file:web/base.ts"),
        ("file:web/api.ts", "file:web/format.js"), ("file:web/index.js", "file:web/api.ts")}
    deps = {(s, d) for t, s, d, _ in es if t == "DEPENDS_ON"}
    assert ("file:shop/service.py", "pkg:python:requests") in deps
    assert ("file:web/api.ts", "pkg:javascript:axios") in deps
    assert ("DEFINES", "shop/models.py::Order", "shop/models.py::Order.total", None) in es
    assert ("DEFINES", "file:shop/models.py", "shop/models.py::Order", None) in es


def test_unresolved_and_external_are_recorded_not_dropped(fixture_graph):
    s = {x.uid: x for x in fixture_graph.symbols()}
    assert s["shop/service.py::OrderService.notify"].unresolved_calls == ["log_event"]
    assert "requests:requests.post" in s["shop/service.py::OrderService.place"].external_calls
    assert "json:json.dumps" in s["shop/utils.py::dump"].external_calls
    assert "os:os.path.join" in s["shop/utils.py::dump"].external_calls     # `import os.path` binds os
    assert "axios:axios.get" in s["web/api.ts::Client.request"].external_calls
    assert "builtin:Math.round" in s["web/format.js::round"].external_calls


def _resolve(files: dict[str, bytes]):
    return resolve([parse_file(p, d) for p, d in files.items()])


def test_ambiguous_and_unique_name_matches():
    g = _resolve({
        "a.py": b"def dup():\n    pass\n\ndef solo():\n    pass\n",
        "b.py": b"def dup():\n    pass\n",
        "c.py": b"def caller():\n    dup()\n    solo()\n    nowhere()\n",
    })
    calls = edge_set(g, "CALLS")
    assert ("CALLS", "c.py::caller", "a.py::dup", "ambiguous") in calls
    assert ("CALLS", "c.py::caller", "b.py::dup", "ambiguous") in calls
    assert ("CALLS", "c.py::caller", "a.py::solo", "inferred") in calls
    caller = next(s for s in g.symbols() if s.uid == "c.py::caller")
    assert caller.unresolved_calls == ["nowhere"]


def test_common_method_names_are_not_linked_by_name():
    g = _resolve({
        "a.py": b"class Store:\n    def get(self):\n        pass\n    def rebuild_index(self):\n        pass\n",
        "b.py": b"def f(obj):\n    obj.get()\n    obj.rebuild_index()\n",
    })
    calls = edge_set(g, "CALLS")
    assert ("CALLS", "b.py::f", "a.py::Store.rebuild_index", "inferred") in calls
    assert not any(d == "a.py::Store.get" for _, _, d, _ in calls)


def test_inherited_method_resolution_through_chain():
    g = _resolve({
        "base.py": b"class A:\n    def ping(self):\n        pass\n",
        "mid.py": b"from base import A\nclass B(A):\n    pass\n",
        "leaf.py": b"from mid import B\nclass C(B):\n    def run(self):\n        self.ping()\n",
    })
    assert ("CALLS", "leaf.py::C.run", "base.py::A.ping", "resolved") in edge_set(g, "CALLS")


def test_python_src_layout_and_package_imports():
    g = _resolve({
        "src/app/core.py": b"def work():\n    pass\n",
        "src/app/__init__.py": b"",
        "tests/test_core.py": b"from app.core import work\nimport app.core as c\n\ndef test():\n    work()\n    c.work()\n",
    })
    calls = edge_set(g, "CALLS")
    assert ("CALLS", "tests/test_core.py::test", "src/app/core.py::work", "resolved") in calls
    assert ("IMPORTS", "file:tests/test_core.py", "file:src/app/core.py", None) in edge_set(g, "IMPORTS")


def test_js_index_resolution_and_ts_js_extension_mapping():
    g = _resolve({
        "lib/index.ts": b"export function boot() {}\n",
        "lib/util.ts": b"export const u = () => 1;\n",
        "main.ts": b'import { boot } from "./lib";\nimport { u } from "./lib/util.js";\nboot(); u();\n',
    })
    calls = edge_set(g, "CALLS")
    assert ("CALLS", "file:main.ts", "lib/index.ts::boot", "resolved") in calls
    assert ("CALLS", "file:main.ts", "lib/util.ts::u", "resolved") in calls


def test_resolution_is_deterministic(fixture_fetch):
    from ariadne.indexer import build_graph
    a, _ = build_graph(fixture_fetch)
    b, _ = build_graph(fixture_fetch)
    assert [(e.type, e.src, e.dst, e.props) for e in a.edges] == [(e.type, e.src, e.dst, e.props) for e in b.edges]

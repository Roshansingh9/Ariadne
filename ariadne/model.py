"""In-memory code-graph model produced by parsing + resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SOURCE_MAX_LINES = 60
SOURCE_MAX_CHARS = 6000

NODE_LABELS = ("File", "Class", "Function", "Package")
EDGE_TYPES = ("DEFINES", "CALLS", "IMPORTS", "DEPENDS_ON", "EXTENDS", "IMPLEMENTS")
CONFIDENCE_LEVELS = ("resolved", "inferred", "ambiguous")


@dataclass
class CallSite:
    name: str                      # callee simple name (e.g. "save")
    receiver: Optional[str]        # receiver text for member calls ("self", "os.path", "repo") or None
    line: int                      # 1-based
    is_new: bool = False           # JS `new X()`
    jsx: Optional[str] = None      # "render" for <X/>, "event" for onClick={x}: a JSX reference, not a call
    event: Optional[str] = None    # JSX attribute name for jsx == "event" (e.g. "onClick")


@dataclass
class ImportBinding:
    """One imported name made available in a file."""
    local: str                     # local binding name ("np", "save", "utils")
    module: str                    # module spec as written ("numpy", "./utils", ".models")
    imported: Optional[str]        # imported member ("save") or None for module/namespace imports
    line: int
    level: int = 0                 # python relative import level
    is_namespace: bool = False     # binding refers to a whole module (import x / import * as x)


@dataclass
class Symbol:
    uid: str
    label: str                     # "Class" | "Function"
    kind: str                      # class | interface | function | method | arrow
    name: str
    qualname: str
    path: str
    language: str
    line_start: int
    line_end: int
    signature: str = ""
    docstring: str = ""
    source: str = ""
    parent_uid: Optional[str] = None       # enclosing Class/Function uid, None → file
    class_uid: Optional[str] = None        # for methods: owning class uid
    bases: list[str] = field(default_factory=list)       # raw base-class expressions
    implements: list[str] = field(default_factory=list)  # raw TS implements
    calls: list[CallSite] = field(default_factory=list)
    var_types: dict[str, str] = field(default_factory=dict)   # local var -> constructor expr
    unresolved_calls: list[str] = field(default_factory=list)
    external_calls: list[str] = field(default_factory=list)
    # Architectural facts read from syntax at index time (see ariadne/frameworks.py).
    decorators: list[str] = field(default_factory=list)   # Python decorator texts
    http_calls: list[str] = field(default_factory=list)   # "POST /api/orders" (fetch/axios/test clients)
    renders_jsx: bool = False                             # body contains JSX


@dataclass
class ParsedFile:
    path: str                      # repo-relative, forward slashes
    language: str
    loc: int
    has_errors: bool = False
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportBinding] = field(default_factory=list)
    module_calls: list[CallSite] = field(default_factory=list)   # top-level calls
    unresolved_calls: list[str] = field(default_factory=list)
    external_calls: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        return file_uid(self.path)


@dataclass
class Edge:
    type: str
    src: str
    dst: str
    props: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.type, self.src, self.dst, self.props.get("line"))


@dataclass
class CodeGraph:
    files: list[ParsedFile] = field(default_factory=list)
    packages: dict[str, dict] = field(default_factory=dict)   # uid -> props
    edges: list[Edge] = field(default_factory=list)

    def symbols(self):
        for f in self.files:
            yield from f.symbols

    def stats(self) -> dict:
        by_edge: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        for e in self.edges:
            by_edge[e.type] = by_edge.get(e.type, 0) + 1
            if "confidence" in e.props:
                c = e.props["confidence"]
                by_conf[c] = by_conf.get(c, 0) + 1
        classes = sum(1 for s in self.symbols() if s.label == "Class")
        functions = sum(1 for s in self.symbols() if s.label == "Function")
        unresolved = sum(len(s.unresolved_calls) for s in self.symbols()) + sum(
            len(f.unresolved_calls) for f in self.files)
        langs: dict[str, int] = {}
        for f in self.files:
            langs[f.language] = langs.get(f.language, 0) + 1
        return {
            "files": len(self.files),
            "classes": classes,
            "functions": functions,
            "packages": len(self.packages),
            "edges": by_edge,
            "edge_confidence": by_conf,
            "unresolved_call_names": unresolved,
            "languages": langs,
        }


def file_uid(path: str) -> str:
    return f"file:{path}"


def package_uid(language: str, name: str) -> str:
    return f"pkg:{language}:{name}"


def truncate_source(text: str) -> str:
    lines = text.splitlines()
    clipped = len(lines) > SOURCE_MAX_LINES
    out = "\n".join(lines[:SOURCE_MAX_LINES])
    if len(out) > SOURCE_MAX_CHARS:
        out = out[:SOURCE_MAX_CHARS]
        clipped = True
    if clipped:
        out += "\n… (truncated)"
    return out

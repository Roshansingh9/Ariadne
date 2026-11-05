"""Deterministic architectural facts from syntax and explicit framework patterns.

Everything here is a pure function over text that the parsers (index time) or the
stored node `source` (query-time fallback for graphs indexed before these facts
existed) provide. Two kinds of result, never mixed:

* **syntax**: an explicit construct says so. `@app.post("/x")` *is* a route
  declaration; `fetch("/x")` *is* an HTTP call; a JSX `onClick={h}` *does* wire `h`.
* **heuristic**: a naming or path convention suggests it (`services/`, `utils.py`,
  `test_*.py`, `pages/`). Reported as *inferred*, always with the rule that fired.

Not supported (documented, never guessed): Django `urls.py` route tables, routes
registered with `app.add_url_rule()`/`add_api_route()`, Express `app.get(...)`
routes, URLs built at runtime (string concatenation, variables), API clients other
than `fetch`/`axios`, GraphQL, dependency injection, reflection and dynamic dispatch.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# --------------------------------------------------------------------------
# Python decorators: routes and background jobs
# --------------------------------------------------------------------------

_STR = r"""(?P<q>['"])(?P<path>[^'"]*)(?P=q)"""
_ROUTE_METHOD = re.compile(rf"^@\s*(?P<obj>[A-Za-z_][\w.]*)\.(?P<m>{'|'.join(HTTP_METHODS)}|websocket)\s*\(\s*{_STR}")
_ROUTE_GENERIC = re.compile(rf"^@\s*(?P<obj>[A-Za-z_][\w.]*)\.(?:route|api_route)\s*\(\s*{_STR}(?P<rest>.*)$", re.S)
_METHODS_KW = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
_JOB = re.compile(r"^@\s*(?:(?P<obj>[A-Za-z_][\w.]*)\.)?(?P<kind>task|shared_task|periodic_task|scheduled_job|on_event|"
                  r"cron|job|consumer|subscribe|on_message|listener)\b\s*(?:\((?P<args>[^)]*)\))?")


def python_routes(decorators: list[str]) -> list[str]:
    """`["POST /api/orders"]` from decorator texts. Paths must start with "/"."""
    out: list[str] = []
    for d in decorators:
        d = d.strip()
        m = _ROUTE_METHOD.match(d)
        if m and m.group("path").startswith("/"):
            method = "WS" if m.group("m") == "websocket" else m.group("m").upper()
            out.append(f"{method} {m.group('path')}")
            continue
        m = _ROUTE_GENERIC.match(d)
        if m and m.group("path").startswith("/"):
            kw = _METHODS_KW.search(m.group("rest") or "")
            methods = [x.strip(" '\"").upper() for x in kw.group(1).split(",") if x.strip(" '\"")] if kw else ["GET"]
            out += [f"{x} {m.group('path')}" for x in methods]
    return list(dict.fromkeys(out))


def python_jobs(decorators: list[str]) -> list[str]:
    """`["celery.task"]`-style labels for background-job / event decorators."""
    out = []
    for d in decorators:
        m = _JOB.match(d.strip())
        if m:
            kind = m.group("kind")
            label = f"{m.group('obj')}.{kind}" if m.group("obj") else kind
            if kind == "on_event" and m.group("args"):
                label += f"({m.group('args').strip()})"
            out.append(label)
    return list(dict.fromkeys(out))


def decorators_from_source(source: str) -> list[str]:
    """Decorator lines at the top of a stored Python `source` (the node starts at its first decorator)."""
    out: list[str] = []
    buf = ""
    for line in (source or "").splitlines():
        s = line.strip()
        if buf:                                   # continuation of a multi-line decorator call
            buf += " " + s
            if buf.count("(") <= buf.count(")"):
                out.append(buf); buf = ""
            continue
        if s.startswith("@"):
            if s.count("(") > s.count(")"):
                buf = s
            else:
                out.append(s)
            continue
        if s.startswith(("def ", "async def ", "class ")) or (s and not s.startswith("#")):
            break
    return out[:8]


# --------------------------------------------------------------------------
# HTTP client calls (JS fetch/axios; Python test clients and requests/httpx)
# --------------------------------------------------------------------------

_FETCH_SRC = re.compile(r"""\bfetch\s*\(\s*(?P<q>[`'"])(?P<url>.*?)(?P=q)(?P<rest>[^;]{0,300})""", re.S)
_AXIOS_SRC = re.compile(rf"""\baxios\s*\.\s*(?P<m>{'|'.join(HTTP_METHODS)})\s*\(\s*(?P<q>[`'"])(?P<url>.*?)(?P=q)""", re.S)
_METHOD_OPT = re.compile(r"""method\s*:\s*['"`](\w+)['"`]""", re.I)
_PY_CLIENT_SRC = re.compile(rf"""\b(?P<recv>(?:[A-Za-z_][\w.]*?)?(?:client|requests|httpx|session))\s*\.\s*(?P<m>{'|'.join(HTTP_METHODS)})\s*\(\s*f?(?P<q>['"])(?P<url>[^'"]*)(?P=q)""")


LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "testserver", "[::1]")


def http_call(method: str, url: str) -> Optional[str]:
    """`"POST /api/x"` for a call into *this* application, else None.

    Absolute URLs to other hosts (third-party APIs) are not calls into this repo's routes,
    so they are never matched against them."""
    host = re.match(r"^\s*[a-z][a-z0-9+.-]*://([^/:?#]*)", url or "", re.I)
    if host and host.group(1).lower() not in LOCAL_HOSTS:
        return None
    path = normalize_path(url)
    return f"{method.upper()} {path}" if path else None


def js_http_calls_from_source(source: str) -> list[str]:
    """`["GET /api/graph_entities"]` from fetch/axios calls with a literal URL in stored source."""
    out = []
    for m in _FETCH_SRC.finditer(source or ""):
        opt = _METHOD_OPT.search(m.group("rest") or "")
        call = http_call(opt.group(1) if opt else "GET", m.group("url"))
        if call:
            out.append(call)
    for m in _AXIOS_SRC.finditer(source or ""):
        call = http_call(m.group("m"), m.group("url"))
        if call:
            out.append(call)
    return list(dict.fromkeys(out))


def python_http_calls_from_source(source: str) -> list[str]:
    """Test clients (`client.get("/api/x")`) and requests/httpx calls with a literal URL."""
    out = []
    for m in _PY_CLIENT_SRC.finditer(source or ""):
        call = http_call(m.group("m"), m.group("url"))
        if call:
            out.append(call)
    return list(dict.fromkeys(out))


_TEMPLATE_EXPR = re.compile(r"\$\{[^}]*\}")


def normalize_path(url: str) -> Optional[str]:
    """Comparable route path: no scheme/host/query/fragment; every parameter becomes `{}`.

    `${id}` (JS template), `{id}` (FastAPI), `<int:id>` (Flask) and `:id` (Express) all map
    to `{}`. Returns None for anything that is not a path (e.g. a bare variable).
    """
    u = _TEMPLATE_EXPR.sub("{}", (url or "").strip())
    u = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", u, flags=re.I)     # scheme + host
    u = re.split(r"[?#]", u, maxsplit=1)[0]
    if not u.startswith("/"):
        return None
    segs = []
    for s in u.split("/")[1:]:
        if re.fullmatch(r"\{[^}]*\}|<[^>]*>|:[A-Za-z_]\w*", s) or "{}" in s:
            s = "{}"
        segs.append(s)
    path = "/" + "/".join(segs)
    return path.rstrip("/") or "/"


def route_matches(call: str, route: str) -> bool:
    """Same method and the same path, segment by segment, with `{}` as a wildcard."""
    cm, _, cp = call.partition(" ")
    rm, _, rp = route.partition(" ")
    if cm != rm and not (rm == "GET" and cm == "HEAD"):
        return False
    a, b = (normalize_path(cp) or "").split("/"), (normalize_path(rp) or "").split("/")
    return len(a) == len(b) and all(x == y or "{}" in (x, y) for x, y in zip(a, b))


# --------------------------------------------------------------------------
# Layer classification
# --------------------------------------------------------------------------

EXTERNAL_SERVICES = {
    # HTTP clients and SDKs
    "requests": "HTTP client", "httpx": "HTTP client", "aiohttp": "HTTP client", "urllib3": "HTTP client",
    "axios": "HTTP client", "boto3": "AWS SDK", "openai": "OpenAI SDK", "anthropic": "Anthropic SDK",
    "twilio": "Twilio SDK", "stripe": "Stripe SDK", "smtplib": "SMTP",
    # databases and stores
    "sqlalchemy": "SQL database", "psycopg2": "PostgreSQL", "psycopg": "PostgreSQL", "asyncpg": "PostgreSQL",
    "pymysql": "MySQL", "sqlite3": "SQLite", "pymongo": "MongoDB", "motor": "MongoDB", "redis": "Redis",
    "falkordb": "FalkorDB", "neo4j": "Neo4j", "elasticsearch": "Elasticsearch", "faiss": "FAISS vector index",
    "chromadb": "Chroma vector DB", "pinecone": "Pinecone",
}
MODEL_BASES = {"BaseModel": "Pydantic model", "SQLModel": "SQLModel", "Model": "ORM model",
               "DeclarativeBase": "SQLAlchemy model", "Base": None, "TypedDict": "TypedDict schema",
               "Schema": "schema"}
FRONTEND_EXTS = (".tsx", ".jsx")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|e2e|spec)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
_SERVICE_PATH = re.compile(r"(^|/)services?(/|\.py$|\.[jt]s$)|_service\.py$|Service\.[jt]sx?$")
_UTIL_PATH = re.compile(r"(^|/)(utils?|helpers?|common|shared|lib)(/|\.py$|\.[jt]sx?$)")
_MODEL_PATH = re.compile(r"(^|/)(models?|schemas?|entities)(/|\.py$|\.[jt]s$)")
_PAGE_PATH = re.compile(r"(^|/)(pages|views|screens)/|(^|/)app/(.*/)?page\.[jt]sx$")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path or ""))


def is_frontend_path(path: str) -> bool:
    return (path or "").endswith(FRONTEND_EXTS)


def classify(node: dict, facts: dict) -> list[dict]:
    """Layers for one node. `facts` holds what is already known about it:
    routes, jobs, http_calls, jsx (renders JSX), event_handler_for, module_entry, decorators.
    Returns [{layer, basis: "syntax"|"heuristic", evidence}], strongest first."""
    out: list[dict] = []
    path, name = node.get("path") or "", node.get("name") or ""
    label = (node.get("labels") or [node.get("label")])[0] if (node.get("labels") or node.get("label")) else ""

    def add(layer, basis, evidence):
        if not any(o["layer"] == layer for o in out):
            out.append({"layer": layer, "basis": basis, "evidence": evidence})

    for r in facts.get("routes") or []:
        add("HTTP route", "syntax", f"route decorator: {r}")
    for j in facts.get("jobs") or []:
        add("Background job / event handler", "syntax", f"decorator @{j}")
    if facts.get("event_handler_for"):
        add("UI event handler", "syntax", "wired in JSX: " + ", ".join(facts["event_handler_for"][:3]))
    if facts.get("jsx"):
        add("UI component", "syntax", "renders JSX")
    for c in facts.get("http_calls") or []:
        layer = "Test" if is_test_path(path) else "API client"
        add(layer if layer == "API client" else "API client (test)", "syntax", f"HTTP call: {c}")
    ext = sorted({EXTERNAL_SERVICES[x.split(":", 1)[0]] for x in node.get("external_calls") or []
                  if x.split(":", 1)[0] in EXTERNAL_SERVICES})
    if ext:
        add("External service / database", "syntax", "calls " + ", ".join(ext))
    if facts.get("module_entry"):
        add("Script entry point", "syntax", "called at module level of " + facts["module_entry"])
    for d in facts.get("decorators") or []:
        if re.match(r"@\s*(dataclasses\.)?dataclass\b", d):
            add("Data model / schema", "syntax", f"decorator {d[:40]}")
    for b in facts.get("bases") or []:
        tail = b.split(".")[-1]
        if MODEL_BASES.get(tail):
            add("Data model / schema", "syntax", f"extends {b} ({MODEL_BASES[tail]})")

    # --- naming / path conventions: always "heuristic" ------------------------------
    if is_test_path(path):
        add("Test", "heuristic", "test file naming convention (tests/, test_*.py, *.test.ts, e2e/)")
    if _PAGE_PATH.search(path) and is_frontend_path(path):
        add("UI page", "heuristic", "path convention (pages/, views/, app/**/page.tsx)")
    if (is_frontend_path(path) and label in ("Function", "Class") and name[:1].isupper()
            and not facts.get("jsx") and not facts.get("jsx_known")):
        add("UI component", "heuristic", "capitalized symbol in a .tsx/.jsx file")
    if _MODEL_PATH.search(path) and label == "Class":
        add("Data model / schema", "heuristic", "defined in a models/schemas module")
    if _SERVICE_PATH.search(path) or (label == "Class" and name.endswith("Service")):
        add("Service / business logic", "heuristic", "service naming convention")
    if _UTIL_PATH.search(path):
        add("Shared utility", "heuristic", "utils/helpers/common module")
    return out


UNSUPPORTED = [
    "Routes declared outside decorators (Django urls.py, add_url_rule/add_api_route, Express app.get) are not detected.",
    "HTTP calls are detected only for fetch/axios (JS/TS) and test clients/requests/httpx (Python) with a literal URL; "
    "URLs assembled at runtime are not linked.",
    "UI→API links match client URL and method against route paths; they are inferred, never graph edges.",
    "Dependency injection, reflection, dynamic dispatch, event buses and cross-repository calls are not followed.",
    "Edges marked inferred/ambiguous come from heuristic call resolution; unresolved calls are not linked.",
]


def layer_order(layer: str) -> int:
    order = ["UI page", "UI component", "UI event handler", "API client", "HTTP route",
             "Background job / event handler", "Script entry point", "Service / business logic",
             "Data model / schema", "External service / database", "Shared utility", "API client (test)", "Test"]
    return order.index(layer) if layer in order else len(order)


def module_name(path: str) -> str:
    return PurePosixPath(path or "").name

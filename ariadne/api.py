"""Ariadne HTTP API (FastAPI)."""

from __future__ import annotations

import hmac
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .indexer import run_index
from .ingest.urls import InvalidRepoURL, parse_repo_url
from .meta_client import MetaClient
from .qa import answer_question
from .queries import GraphQueries, NodeNotFound
from .store import FalkorStore, RepoNotFound, StoreError, check_repo_id

log = logging.getLogger("ariadne")
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

app = FastAPI(title="Ariadne", version=__version__,
              description="Repository intelligence on FalkorDB Cloud + Meta Model API.")


# --------------------------------------------------------------------------
# Dependencies (overridable in tests)
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def _store() -> FalkorStore:
    return FalkorStore(_settings())


def get_settings() -> Settings:
    return _settings()


def get_store():
    return _store()


def get_meta(settings: Settings = Depends(get_settings)) -> MetaClient:
    return MetaClient(settings)


def require_index_token(x_ariadne_token: Optional[str] = Header(default=None),
                        settings: Settings = Depends(get_settings)) -> None:
    """Guard for write endpoints (index, delete).

    * Token configured: the X-Ariadne-Token header must match it (constant-time).
    * No token on Vercel: fail closed, since a deployment is reachable by anyone.
    * No token locally: allowed, for development.
    """
    if not settings.index_token:
        if settings.on_vercel:
            raise HTTPException(status_code=503, detail="Indexing and deletion are disabled: "
                                "ARIADNE_INDEX_TOKEN is not configured on the server.")
        return
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (would be a 500).
    supplied = (x_ariadne_token or "").encode("utf-8", "surrogateescape")
    if not supplied or not hmac.compare_digest(supplied, settings.index_token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Ariadne-Token.")


def repo_queries(repo_id: str, store=Depends(get_store)) -> GraphQueries:
    try:
        check_repo_id(repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GraphQueries(store, repo_id)


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------

@app.exception_handler(StoreError)
async def _store_error(_: Request, exc: StoreError):
    return JSONResponse({"detail": str(exc), "error": "database"}, status_code=503)


@app.exception_handler(RepoNotFound)
async def _repo_missing(_: Request, exc: RepoNotFound):
    return JSONResponse({"detail": f"Repository '{exc}' is not indexed.", "error": "not_found"},
                        status_code=404)


@app.exception_handler(NodeNotFound)
async def _lookup_error(_: Request, exc: NodeNotFound):
    msg = exc.args[0] if exc.args else "Not found."
    return JSONResponse({"detail": str(msg), "error": "not_found"}, status_code=404)


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc), "error": "bad_request"}, status_code=400)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class IndexRequest(BaseModel):
    url: str = Field(..., max_length=300)
    ref: Optional[str] = Field(default=None, max_length=200)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)


def _split(rels: Optional[str]) -> Optional[list[str]]:
    return [r for r in rels.split(",") if r.strip()] if rels else None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/api/health")
def health(settings: Settings = Depends(get_settings), store=Depends(get_store)):
    db = {"configured": settings.falkordb_configured, "reachable": False}
    if settings.falkordb_configured:
        try:
            db["reachable"] = bool(store.ping())
        except StoreError as exc:
            db["error"] = str(exc)
    return {"status": "ok", "version": __version__, "config": settings.public_status(), "falkordb": db}


@app.post("/api/index", dependencies=[Depends(require_index_token)])
def index_repo(body: IndexRequest, settings: Settings = Depends(get_settings), store=Depends(get_store)):
    try:
        parse_repo_url(body.url, body.ref)
    except InvalidRepoURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not settings.falkordb_configured:
        raise HTTPException(status_code=503, detail="FalkorDB is not configured on the server.")

    def stream():
        for event in run_index(body.url, body.ref, settings, store):
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/repos")
def list_repos(store=Depends(get_store)):
    repos = store.list_repos()
    for r in repos:
        if isinstance(r.get("stats_json"), str):
            try:
                r["stats"] = json.loads(r.pop("stats_json"))
            except ValueError:
                r.pop("stats_json", None)
        r.pop("skipped_json", None)
    return {"repos": repos}


@app.delete("/api/repos/{repo_id}", dependencies=[Depends(require_index_token)])
def delete_repo(repo_id: str, store=Depends(get_store)):
    check_repo_id(repo_id)
    if not store.delete_repo(repo_id):
        raise RepoNotFound(repo_id)
    return {"deleted": repo_id}


@app.get("/api/repos/{repo_id}/overview")
def overview(gq: GraphQueries = Depends(repo_queries)):
    ov = gq.overview()
    repo = ov["repo"]
    for key in ("stats_json", "skipped_json"):
        if isinstance(repo.get(key), str):
            try:
                repo[key.removesuffix("_json")] = json.loads(repo.pop(key))
            except ValueError:
                repo.pop(key, None)
    return ov


@app.get("/api/repos/{repo_id}/search")
def search(q: str = Query(..., min_length=1, max_length=200), kind: str = "all",
           limit: int = 25, gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return {"query": q, "kind": kind, "results": gq.search(q, kind, limit)}


@app.get("/api/repos/{repo_id}/symbol")
def symbol(uid: str = Query(..., max_length=600), gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return gq.symbol(uid)


@app.get("/api/repos/{repo_id}/file")
def file_detail(path: str = Query(..., max_length=500), gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return gq.file_by_path(path)


@app.get("/api/repos/{repo_id}/graph")
def repo_graph(rels: Optional[str] = None, limit: int = 150, gq: GraphQueries = Depends(repo_queries)):
    """Initial repository graph: most-connected nodes + edges among them (bounded)."""
    gq.ensure_exists()
    return gq.repo_graph(_split(rels), limit)


@app.get("/api/repos/{repo_id}/neighbors")
def neighbors(uid: str = Query(..., max_length=600), rels: Optional[str] = None,
              direction: str = "both", limit: int = 80, gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return gq.neighbors(uid, _split(rels), direction, limit)


@app.get("/api/repos/{repo_id}/path")
def path(src: str = Query(..., max_length=600), dst: str = Query(..., max_length=600),
         rels: Optional[str] = None, max_depth: int = 6, undirected: bool = False,
         gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return gq.path(src, dst, _split(rels), max_depth, undirected)


@app.get("/api/repos/{repo_id}/impact")
def impact(uid: str = Query(..., max_length=600), depth: int = 3, rels: Optional[str] = None,
           gq: GraphQueries = Depends(repo_queries)):
    gq.ensure_exists()
    return gq.impact(uid, depth, _split(rels))


class ScenarioRequest(BaseModel):
    scenario: str = Field(..., min_length=1, max_length=1000)
    targets: Optional[list[str]] = Field(default=None, max_length=10)
    depth: int = Field(default=3, ge=1, le=6)
    rels: Optional[list[str]] = Field(default=None, max_length=4)
    use_model: bool = True


@app.post("/api/repos/{repo_id}/impact/scenario")
def impact_scenario(body: ScenarioRequest, gq: GraphQueries = Depends(repo_queries),
                    meta: MetaClient = Depends(get_meta)):
    """Natural-language change scenario → matched indexed targets → impact report. Read-only."""
    from .scenario import analyze
    return analyze(gq, meta, body.scenario, body.targets, body.depth, body.rels, body.use_model)


@app.post("/api/repos/{repo_id}/ask")
def ask(body: AskRequest, gq: GraphQueries = Depends(repo_queries), meta: MetaClient = Depends(get_meta)):
    return answer_question(gq, meta, body.question)


# --------------------------------------------------------------------------
# Frontend. On Vercel, public/ is served by the CDN; locally we serve it here.
# --------------------------------------------------------------------------

# Without Cache-Control, browsers may heuristically reuse a stale copy of one asset next to a
# fresh copy of another (e.g. new app.js with an old styles.css → zero-height graph canvas).
# "no-cache" means "revalidate every time"; the ETag keeps that a cheap 304.
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", include_in_schema=False)
def index_html():
    page = PUBLIC_DIR / "index.html"
    if page.is_file():
        return FileResponse(page, headers=NO_CACHE)
    # On Vercel, public/ is served by the CDN and may not be inside the function bundle.
    return RedirectResponse("/index.html", status_code=307)


_ASSETS = {"app.js": "text/javascript", "styles.css": "text/css", "favicon.svg": "image/svg+xml"}

if not os.getenv("VERCEL"):
    @app.get("/{asset}", include_in_schema=False)
    def local_asset(asset: str):
        if asset not in _ASSETS:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(PUBLIC_DIR / asset, media_type=_ASSETS[asset], headers=NO_CACHE)

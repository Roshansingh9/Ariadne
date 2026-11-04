"""Indexing pipeline: fetch → filter → parse → resolve → persist.

`run_index` is a generator of progress events, so the HTTP layer can
stream them as NDJSON. Parsing and resolution finish entirely in memory
before the database is touched, so a parse failure never leaves a
half-written graph.
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

from .config import Settings
from .ingest.fetch import FetchError, FetchResult, Limits, fetch_repository, walk_directory
from .ingest.urls import RepoRef, parse_repo_url
from .model import CodeGraph, ParsedFile
from .parsing import parse_file
from .resolver import resolve
from .store import StoreError, stats_json


class IndexingError(RuntimeError):
    pass


def limits_from(settings: Settings, deadline: Optional[float] = None) -> Limits:
    return Limits(deadline=deadline, max_files=settings.max_files, max_file_bytes=settings.max_file_kb * 1024,
                  max_archive_bytes=settings.max_archive_mb * 1024 * 1024,
                  max_source_bytes=settings.max_source_mb * 1024 * 1024,
                  timeout=settings.fetch_timeout)


def parse_all(fetched: FetchResult) -> tuple[list[ParsedFile], list[dict]]:
    parsed, errors = [], []
    for path, data in fetched.files:
        try:
            parsed.append(parse_file(path, data))
        except Exception as exc:  # parser bugs must not abort the whole index
            errors.append({"path": path, "error": type(exc).__name__})
    return parsed, errors


def build_graph(fetched: FetchResult) -> tuple[CodeGraph, list[dict]]:
    parsed, errors = parse_all(fetched)
    return resolve(parsed), errors


def _event(stage: str, message: str, **extra) -> dict:
    return {"stage": stage, "message": message, "t": round(time.time(), 3), **extra}


def run_index(url: str, ref: Optional[str], settings: Settings, store) -> Iterator[dict]:
    started = time.time()
    deadline = time.monotonic() + settings.index_deadline

    def check_deadline(stage: str) -> None:
        if time.monotonic() >= deadline:
            raise IndexingError(f"Indexing exceeded the {int(settings.index_deadline)}s time budget "
                                f"during {stage}. Try a smaller repository.")
    repo: RepoRef = parse_repo_url(url, ref)            # raises InvalidRepoURL (caller maps to 400)
    rid = repo.repo_id
    yield _event("start", f"Indexing {repo.web_url}", repo_id=rid)

    base_props = {"url": repo.web_url, "name": repo.name, "owner": repo.owner, "host": repo.host}
    try:
        prev = store.mark_indexing(rid, base_props)
    except StoreError as exc:
        yield _event("error", str(exc), error=str(exc), repo_id=rid)
        return

    replaced = False
    try:
        yield _event("fetch", f"Downloading {repo.slug}…")
        t0 = time.time()
        fetched = fetch_repository(repo, limits_from(settings, deadline), settings.github_token)
        if not fetched.files:
            raise IndexingError("No supported source files (Python, JavaScript, TypeScript) were found.")
        yield _event("fetch", f"Fetched {len(fetched.files)} source files", files=len(fetched.files),
                     skipped=fetched.skipped, truncated=fetched.truncated,
                     seconds=round(time.time() - t0, 2))

        t0 = time.time()
        yield _event("parse", "Parsing and resolving symbols…")
        graph, parse_errors = build_graph(fetched)
        check_deadline("parsing")
        stats = graph.stats()
        yield _event("parse", "Built code graph", stats=stats, parse_errors=len(parse_errors),
                     seconds=round(time.time() - t0, 2))

        t0 = time.time()
        repo_props = {
            **base_props, "ref": fetched.ref or "", "commit": fetched.commit or "",
            "source": fetched.source, "truncated": fetched.truncated,
            "stats_json": stats_json(stats),
            "skipped_json": stats_json({"counts": fetched.skipped, "samples": fetched.skipped_samples,
                                        "parse_errors": parse_errors[:20]}),
        }
        yield _event("store", "Writing graph to FalkorDB…",
                     total=sum(1 for _ in graph.symbols()) + len(graph.files) + len(graph.edges))
        replaced = True
        write_info: dict = {}
        last_kind = None
        for step in store.replace_graph_steps(rid, repo_props, graph):
            check_deadline("graph writes")
            if step.get("final"):
                write_info = {k: v for k, v in step.items() if k != "final"}
            elif step["kind"] != last_kind or step["done"] == step["total"]:
                last_kind = step["kind"]
                yield _event("store", f"Writing {step['kind'].split(':', 1)[1]}",
                             done=step["done"], total=step["total"])
        summary = {
            "repo_id": rid, "url": repo.web_url, "ref": fetched.ref, "commit": fetched.commit,
            "stats": stats, "skipped": fetched.skipped, "skipped_samples": fetched.skipped_samples[:15],
            "truncated": fetched.truncated, "parse_errors": parse_errors[:20],
            "write": write_info, "seconds": round(time.time() - started, 2),
            "store_seconds": round(time.time() - t0, 2),
        }
        yield _event("done", "Indexing complete", summary=summary)
    except (FetchError, IndexingError, StoreError) as exc:
        msg = str(exc)
        if replaced:
            store.mark_failed(rid, msg)
        else:
            store.restore_status(rid, prev, msg)
        yield _event("error", msg, error=msg, repo_id=rid)
    except Exception as exc:  # unexpected: report type only, never internals
        msg = f"Unexpected indexing failure ({type(exc).__name__})."
        if replaced:
            store.mark_failed(rid, msg)
        else:
            store.restore_status(rid, prev, msg)
        yield _event("error", msg, error=msg, repo_id=rid)


def index_directory(path: str, repo_id: str, store, settings: Optional[Settings] = None,
                    url: str = "") -> dict:
    """Index a local directory (tests / local CLI). Not exposed over HTTP."""
    settings = settings or Settings()
    fetched = walk_directory(path, limits_from(settings))
    graph, errors = build_graph(fetched)
    stats = graph.stats()
    props = {"url": url or f"local:{repo_id}", "name": repo_id, "owner": "", "host": "local",
             "ref": "", "commit": "", "source": "directory", "truncated": fetched.truncated,
             "stats_json": stats_json(stats),
             "skipped_json": stats_json({"counts": fetched.skipped, "samples": fetched.skipped_samples})}
    store.replace_graph(repo_id, props, graph)
    return {"stats": stats, "skipped": fetched.skipped, "parse_errors": errors, "graph": graph}

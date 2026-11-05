# Ariadne

**Repository intelligence on a code graph.** Paste a public Git URL. Ariadne parses the source with tree-sitter and stores a code graph in **FalkorDB Cloud**. You can then search, explore callers, callees, imports and inheritance, find paths, run impact analysis, and ask questions answered by the **Meta Model API** from graph evidence, with file and line citations.

- Languages: **Python, JavaScript, TypeScript (incl. TSX)**
- Graph: `File`, `Class`, `Function`, `Package` nodes; `DEFINES`, `CALLS`, `IMPORTS`, `DEPENDS_ON`, `EXTENDS`, `IMPLEMENTS` edges
- Every inferred link is labeled: `resolved` / `inferred` / `ambiguous`. Unresolved and external calls are kept, not dropped.
- No MCP server. The web app is the product.

Design docs: [`docs/FALKORDB_CODEGRAPH_ANALYSIS.md`](docs/FALKORDB_CODEGRAPH_ANALYSIS.md) (upstream study) and [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## Quick start (local)

```bash
python -m pip install -e ".[dev]"
cp .env.example .env          # fill in FalkorDB Cloud + Meta credentials
python -m uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

## Configuration

| Variable | Purpose |
|---|---|
| `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD` | FalkorDB Cloud connection |
| `FALKORDB_SSL` | `true` if the endpoint supports TLS (default `false`) |
| `FALKORDB_TIMEOUT` | socket/query timeout in seconds, default 30 |
| `FALKORDB_GRAPH_NAME` | graph-name prefix (default `ariadne`) → `ariadne:github__owner__repo` |
| `META_API_KEY` (alias `MODEL_API_KEY`) | Meta Model API key (server-side only) |
| `META_API_BASE_URL` (alias `META_BASE_URL`) | default `https://api.meta.ai/v1` |
| `META_MODEL` | default `muse-spark-1.3` |
| `META_API_TIMEOUT` | seconds, default 45 |
| `ARIADNE_INDEX_TOKEN` | if set, `POST /api/index` and `DELETE /api/repos/…` require a matching `X-Ariadne-Token` header (401 otherwise). **Required on Vercel**: without it those endpoints return 503 there. Optional locally |
| `GITHUB_TOKEN` | optional. Raises the GitHub API rate limit for metadata lookups |
| `ARIADNE_INDEX_DEADLINE_SECONDS` | whole-index time budget, default 270 (below the 300 s function limit) |
| `ARIADNE_MAX_FILES` / `_MAX_FILE_KB` / `_MAX_ARCHIVE_MB` / `_MAX_SOURCE_MB` | indexing bounds (defaults 1500 / 400 / 60 / 25) |
| `ARIADNE_FETCH_TIMEOUT` | GitHub download timeout in seconds, default 60 |

Blank values count as unset. If a canonical name and its alias are both set to different values, the canonical name wins and a warning is logged. Invalid numbers fall back to the default with a warning. Warnings name variables, never values.

`GET /api/health` reports only whether each setting is **present**. Values are never returned or logged.

## API

| Method & path | Description |
|---|---|
| `GET /api/health` | config presence + FalkorDB reachability |
| `POST /api/index` `{url, ref?}` | index a repo. Streams NDJSON progress events |
| `GET /api/repos` · `DELETE /api/repos/{id}` | list / delete indexed repos |
| `GET /api/repos/{id}/overview` | counts, languages, hotspots, most-imported files, packages |
| `GET /api/repos/{id}/search?q=&kind=all\|file\|class\|function\|package` | ranked search |
| `GET /api/repos/{id}/symbol?uid=` · `/file?path=` | definition, source, callers, callees, hierarchy, imports |
| `GET /api/repos/{id}/neighbors?uid=&rels=CALLS,IMPORTS&direction=both` | subgraph for visualization |
| `GET /api/repos/{id}/path?src=&dst=&rels=&max_depth=6&undirected=false` | bounded shortest path |
| `GET /api/repos/{id}/impact?uid=&depth=3` | transitive dependents with depth + confidence, affected files |
| `POST /api/repos/{id}/ask` `{question}` | graph-grounded answer with `[E#]` citations |

Node ids (`uid`) are `file:<path>`, `<path>::<Qualified.name>` or `pkg:<lang>:<name>`. Lines are 1-based.

## How questions are answered

1. Identifiers in the question are matched against the graph by deterministic search.
2. Ariadne collects evidence: definitions with source, callers and callees with call lines, impact (for "what breaks if…" questions), and a repo overview (for broad questions).
3. Only that evidence is sent to the Meta Model API. The system prompt requires `[E#]` citations and separate **Graph facts** / **Interpretation** sections, and the model must say when the evidence is insufficient.
4. Citations are validated. References to unknown evidence ids are removed and reported.
5. If nothing matches, no model call is made. If the Meta API is unavailable, the response is a deterministic `graph_only` summary with the error. **No other model provider is ever used, and model output is never executed as Cypher.**

## Impact analysis

`GET /api/repos/{id}/impact?uid=…&depth=1-6&rels=CALLS,IMPORTS,EXTENDS,IMPLEMENTS` walks dependents in reverse and returns a `report` built **deterministically** from graph facts (no model is used). The Impact tab follows the relationship filters ticked above the graph.

- **Summary:** direct and transitive dependents, affected files, depth, relationships. A class or file also includes its members.
- **Dependency paths:** every dependent on at least one path to the selected symbol, with multiple callers and alternative paths (e.g. a route that calls it directly *and* through a helper). Cycles never repeat a node, and no path is longer than the depth.
- **Architecture & layers**, **user flows / UI impact**, **behaviour**, **affected files**, **explanation**, **suggested inspection** and **evidence & coverage**.

Every item says how it is known:

| Basis | Examples |
|---|---|
| **confirmed · graph** | a `CALLS`/`IMPORTS`/`EXTENDS`/`IMPLEMENTS` edge (with its own resolved / inferred / ambiguous confidence) |
| **confirmed · syntax** | route decorators (`@app.post("/x")`, `@bp.route(..., methods=[...])`), job decorators (`@celery.task`, `@shared_task`, `@app.on_event`), `fetch`/`axios` calls with a literal URL, JSX `<Component/>` renders and `onClick={handler}` wiring, model base classes (`BaseModel`, `SQLModel`, …), external-service calls (`requests`, `boto3`, `redis`, …) |
| **inferred** | path/name conventions (`services/`, `utils.py`, `tests/`, `pages/`), and **UI → API links**: a client's URL and method matched to a route path. These are never graph edges. |

"UI impact not established — no frontend code found in this repository." is shown when there are no `.tsx`/`.jsx` files and no JS/TS API clients. "UI/API relationship not established from indexed code." is shown when frontend code exists but nothing links it to the selected symbol. Graph connectivity indicates **potential** impact, never verified breakage. Tests are listed only when a graph path or a URL match connects them.

**Not analysed:** routes declared outside decorators (Django `urls.py`, `add_url_rule`, Express `app.get`), URLs built at runtime, API clients other than `fetch`/`axios`/test clients/`requests`/`httpx`, dependency injection, reflection, dynamic dispatch, event buses and cross-repository calls.

**Compatibility.** Graphs indexed by this version are stamped `graph_version: 2`. Their symbols carry `decorators`, `http_calls` and `renders_jsx`, and JSX `CALLS` edges carry `syntax` (`jsx-render` / `jsx-event`). Existing graphs need no migration: impact works unchanged, and routes, jobs and HTTP calls are read from each symbol's stored source (its first 60 lines). JSX render/event links exist only in graphs indexed by this version, and the report says so. They appear the next time you index a repository normally; nothing is re-indexed automatically.

## Tests

```bash
python -m pytest -q                       # unit + mocked integration (no network, no DB)
ARIADNE_LIVE_FALKORDB=1 python -m pytest -q tests/test_live.py   # real FalkorDB (uses FALKORDB_* env)
ARIADNE_LIVE_FALKORDB=1 ARIADNE_LIVE_META=1 python -m pytest -q tests/test_live.py
```

The default suite uses a fake FalkorDB client (`tests/fake_falkor.py`) and mocked HTTP for Meta and GitHub. Cypher templates are validated against a real database **only** by `tests/test_live.py`.

Browser regression tests (opt-in; headless Chromium via Playwright, in-memory fixture data, nothing written anywhere) check that the graph canvas actually paints, including with a stale cached stylesheet:

```bash
ARIADNE_BROWSER_TESTS=1 python -m pytest -q tests/test_browser_graph.py
```

**After editing `public/app.js` or `public/styles.css`**, run `python scripts/stamp_assets.py`. It updates the `?v=<hash>` on their URLs in `index.html`, so browsers never pair a new page with a stale cached script or stylesheet. `tests/test_frontend.py` fails if the stamps are out of date.

## Deploying to Vercel

- `app.py` exports the FastAPI `app`. Dependencies are pinned in `pyproject.toml` and locked (including transitive packages) in `uv.lock`, which Vercel installs from. Python is pinned in `.python-version`. The UI lives in `public/` and is served by Vercel's CDN.
- **Fluid compute must be enabled.** `vercel.json` sets `"fluid": true` and `maxDuration: 300`. Without Fluid compute, Hobby functions are capped at 60 s and indexing larger repositories is cut off mid-run. Fluid compute is on by default for new projects; for an older project also check **Settings → Functions → Fluid Compute**. 300 s is the Hobby maximum (Pro allows up to 800).
- **`ARIADNE_INDEX_TOKEN` is required on Vercel.** Without it, `POST /api/index` and `DELETE /api/repos/…` return 503 there (they fail closed), while all read endpoints keep working. Locally the token is optional.
- Set the environment variables above in the Vercel project settings (Production *and* Preview). Use only the names in the table: names from older drafts (`INGEST_*`, `INDEX_DEADLINE_SECONDS`, `MAX_TRAVERSAL_*`, `SNIPPET_*`) are ignored, and a warning naming each one is logged at startup.
- `FALKORDB_SSL`: set it to `true` only if your FalkorDB Cloud endpoint accepts TLS on its port. If it doesn't (the handshake times out), leave it `false`.
- To change dependencies: edit the pins in `pyproject.toml`, run `uv lock`, then rerun the offline and live suites.
- GitHub repos are downloaded as a tarball over HTTPS and processed **in memory**, so `git` is not needed. Other hosts (GitLab, Bitbucket, Codeberg) need a `git` binary, which the Vercel runtime is not documented to provide. Those requests fail with a clear message there.

## Limitations

- Static resolution only, with no type inference beyond `x = Foo()` / `new Foo()`. Dynamic dispatch, re-export barrels, star imports and decorators that rename are not followed. Generic method names (`get`, `save`, `push`, …) are never linked by name alone.
- Bounded indexing: by default 1,500 files, 400 KB per file and a 60 MB archive. Larger repos are indexed partially and marked `truncated`.
- Full re-index only: no incremental updates or commit history. One branch per repository graph.
- Indexing runs inside a single request, so very large repos may hit the platform time limit.

## Acknowledgements

Architecture inspired by [FalkorDB/code-graph](https://github.com/FalkorDB/code-graph) (MIT, © 2024 FalkorDB). Ariadne contains no upstream source code; see the analysis doc for the comparison.

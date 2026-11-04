# Ariadne — Implementation Plan

Ariadne is a repository-intelligence web app. You paste a public Git URL, Ariadne parses the code with tree-sitter, and it stores a code graph in **FalkorDB Cloud**. You can then search, explore, run impact analysis, and ask questions that are answered by the **Meta Model API** from graph evidence, with citations.

Upstream reference: `docs/FALKORDB_CODEGRAPH_ANALYSIS.md`.

## 1. Architecture

One FastAPI app deployed as one Vercel Function, with a build-free static frontend in `public/` that Vercel serves from its CDN.

```
public/                 index.html, app.js, styles.css  (vanilla JS + Cytoscape.js from CDN)
app.py                  Vercel entrypoint → `from ariadne.api import app`
ariadne/
  config.py             env settings (no secrets logged)
  api.py                FastAPI routes, error mapping, NDJSON progress stream
  ingest/urls.py        repo URL validation + normalization (host allow-list)
  ingest/fetch.py       GitHub tarball streamed in memory; git clone fallback
  ingest/filters.py     path/vendor/generated/minified/secret filtering
  parsing/              tree-sitter extraction: python.py, javascript.py (JS+TS)
  model.py              dataclasses: FileInfo, Symbol, CallSite, ImportRecord, Edge, CodeGraph
  resolver.py           cross-file static resolution → edges with confidence
  indexer.py            pipeline: fetch → filter → parse → resolve → persist (+events)
  store.py              FalkorDB Cloud client, graph naming, batched writes, read helper
  queries.py            read API (search, symbol, callers, neighbors, paths, impact, overview)
  qa.py                 question → evidence retrieval → Meta prompt → citation check
  meta_client.py        Meta Model API (chat completions) client
tests/                  unit + mocked + opt-in live integration tests; fixtures/sample_repo
```

There are no background workers and no queues. Indexing runs inside one streamed HTTP request, bounded so it fits Vercel's 300 s Hobby limit (§9).

## 2. Graph schema

One FalkorDB graph per repository: `{FALKORDB_GRAPH_NAME}:{host}__{owner}__{repo}`. The prefix defaults to `ariadne`, and the name is sanitized to `[A-Za-z0-9_.:-]`.

Every code node carries the common label **`Entity`** plus a specific label. The uid is unique per graph, with a range index on `Entity.uid`.

| Label | Key props |
|---|---|
| `Repository` (1 node) | `url, name, host, owner, ref, commit, status (indexing/ready/failed), indexed_at, stats_json, skipped_json` |
| `Entity:File` | `uid="file:"+path, path, name, ext, language, loc, unresolved_calls, external_calls` |
| `Entity:Class` | `uid, name, qualname, kind (class/interface), path, line_start, line_end, signature, docstring, source, language` |
| `Entity:Function` | same + `kind (function/method/arrow)`, `unresolved_calls[]`, `external_calls[]` |
| `Entity:Package` | `uid="pkg:"+lang+":"+name, name, language` (external import target) |

`uid` = `"{path}::{qualname}"`. If the same qualname is defined twice in a file (redefinition or overload), the later ones get `#L{line}`. Lines are **1-based**. `source` is truncated to 60 lines / 6 KB per node.

| Edge | Direction | Props |
|---|---|---|
| `DEFINES` | File→Class/Function, Class→Function, Function→Function (nested) | — |
| `CALLS` | Function/File→Function/Class (Class = instantiation) | `line`, `resolution`, `confidence` |
| `IMPORTS` | File→File | `line` |
| `DEPENDS_ON` | File→Package | `line` |
| `EXTENDS` / `IMPLEMENTS` | Class→Class | `resolution`, `confidence` |

`confidence` takes three values:
- `resolved`: same file, explicit import binding, `self`/`this`/`cls` method in the class chain, or a class-qualified call.
- `inferred`: unique repo-wide name match.
- `ambiguous`: 2–3 candidates, each linked.

Calls that resolve to nothing are kept in `unresolved_calls`. Calls into external packages are kept in `external_calls`.

## 3. Ingestion and indexing pipeline

1. **Validate URL** (`urls.py`):
   - `https` only; hosts github.com, gitlab.com, bitbucket.org, codeberg.org.
   - No userinfo, port, query or fragment; path is `owner/repo[.git][/tree/<ref>]`.
   - The charset is restricted, and a ref may not start with `-`.
2. **Fetch** (`fetch.py`):
   - **GitHub:** `GET api.github.com/repos/{o}/{r}` confirms the repo is public and gets the default branch. The tarball from `codeload.github.com/{o}/{r}/tar.gz/{ref}` is then streamed and read **in memory** with `tarfile` streaming mode. Only regular files that pass filters are read. Symlinks, hardlinks, devices, absolute or `..` paths, and oversize members are skipped. Total download is capped (`ARIADNE_MAX_ARCHIVE_MB`). Nothing is written to disk.
   - **Other hosts:** `git clone --depth 1 --single-branch [--branch ref] -- <url> <tmp>` with `GIT_TERMINAL_PROMPT=0`, `core.symlinks=false`, `protocol.file.allow=never` and a timeout, into a `TemporaryDirectory` that is removed afterwards. If `git` is missing (likely on Vercel), this fails with a clear error.
3. **Filter** (`filters.py`):
   - Supported extensions only.
   - Ignored dirs: `.git`, `node_modules`, `vendor`, `dist`, `build`, `venv`, `.venv`, `__pycache__`, `site-packages`, `.next`, `coverage`, …
   - Skipped files: minified (`*.min.js`, long average lines), generated (`@generated`, `DO NOT EDIT`, `*_pb2.py`), `.d.ts`, oversize files, and secret-named files (`secrets.py`, `credentials.*`, `.env*`).
   - **Content secret scan:** private-key blocks, AWS/GitHub/Slack/Google/Stripe token patterns. A matching file is skipped and reported (path + reason only, never the match).
4. **Parse** (`parsing/`): tree-sitter per file → symbols, call sites (attributed to the innermost enclosing function), imports, base classes. Parse errors are tolerated, and partial trees are flagged with `has_errors`.
5. **Resolve** (`resolver.py`): §4.
6. **Persist** (`store.py`):
   - Parsing and resolution finish in memory first, so a failure never leaves a half-written graph from a failed parse.
   - Then: delete the existing graph, create the index, write `Repository{status:'indexing'}`, write nodes and edges in `UNWIND` batches of 500 with `MERGE` on uid, and finally set `status:'ready'` with stats.
   - Re-indexing therefore replaces the graph, so no duplicates and no stale nodes remain.
7. **Progress:** `POST /api/index` streams NDJSON events: `{stage, message, counts}`, then a final `{stage:"done", summary}` or `{stage:"error", error}`.

## 4. Resolution strategy (static, original)

For each call site in file F (callee name `n`, optional receiver `r`):

1. **No receiver:**
   - Look for a symbol in F's own top-level or enclosing scope → `resolved/same_file`.
   - Otherwise, an import binding `n` → target file symbol → `resolved/import`.
   - Otherwise, a binding from an external package → `external_calls`.
   - Otherwise, a unique repo-wide top-level function or class named `n` → `inferred/name_match`. With 2–3 candidates → `ambiguous`. Otherwise unresolved.
2. **`self`/`cls`/`this` receiver:** look up method `n` in the enclosing class, then its resolved base classes (BFS, depth ≤ 5) → `resolved/self_method`.
3. **Receiver bound to an imported module or namespace** (`import x`, `import * as x`, `from pkg import mod`): symbol `n` in that file → `resolved/module_attr`.
4. **Receiver names a class** (local or imported): method `n` of that class or its bases → `resolved/class_method`.
5. **Other receivers** (`obj.m()`): a unique repo-wide method named `m` that is not in a common-name stop list (`get`, `append`, `push`, `then`, `map`, `log`, …) → `inferred/name_match`. Otherwise unresolved.

Import resolution:
- **Python:** dotted modules map to files by exact and suffix module path (handles `src/` layouts). Relative imports use their dot level.
- **JS/TS:** relative specifiers resolve against the importer's directory, trying the extensions and `index.*`.
- Non-relative specifiers become `Package` nodes, using the npm scope plus name, or the Python top-level module.

## 5. Query and analysis API

All Cypher is fixed templates. Values go in as parameters. Labels and relation types are interpolated only after checking them against an allow-list. Depths and limits are clamped. There is no endpoint that accepts Cypher.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | config presence (booleans only), FalkorDB ping |
| `POST /api/index` | index a repo URL (NDJSON stream); requires `X-Ariadne-Token` when `ARIADNE_INDEX_TOKEN` is set; disabled (503) on Vercel when it is not |
| `GET /api/repos` | indexed repos (Repository node of each `ariadne:*` graph) |
| `DELETE /api/repos/{repo_id}` | drop a repo graph (token-guarded) |
| `GET /api/repos/{id}/overview` | counts by label/edge/confidence, languages, top files, hotspots |
| `GET /api/repos/{id}/search?q=&kind=` | files + symbols, ranked exact > prefix > contains |
| `GET /api/repos/{id}/files?path=` | file detail: symbols, imports, importers, packages |
| `GET /api/repos/{id}/symbol?uid=` | definition + source + callers + callees + hierarchy + unresolved |
| `GET /api/repos/{id}/neighbors?uid=&rels=&direction=&limit=` | subgraph for visualization |
| `GET /api/repos/{id}/path?src=&dst=&rels=&max_depth=` | bounded BFS shortest path |
| `GET /api/repos/{id}/impact?uid=&depth=` | transitive reverse dependents with depth + confidence, affected files |
| `POST /api/repos/{id}/ask` | grounded Q&A |

## 6. Natural-language Q&A flow

1. **Retrieve evidence** (deterministic):
   - Extract identifier-like tokens and quoted names from the question, then search symbols and files.
   - Take the top matches (≤ 6). For each: definition snippet, callers, callees, and the top impact entries if the question mentions impact, break, change or affect.
   - Add the repo overview for broad questions ("what does this repo do", "architecture").
2. **Build the evidence pack:** numbered items `[E1] … [En]`, each with uid, path:line range, a fact, and a truncated snippet. Total is capped at about 24 KB.
3. **Call Meta** (`POST {META_API_BASE_URL}/chat/completions`, `Authorization: Bearer META_API_KEY`, `model=META_MODEL`, default `muse-spark-1.3`, `temperature 0.2`, `max_completion_tokens 900`, timeout `META_API_TIMEOUT`). The system prompt requires the model to:
   - answer only from the evidence;
   - cite `[E#]` and `path:line`;
   - separate **Graph facts** from **Interpretation**;
   - say "insufficient evidence" when appropriate.
4. **Validate:** parse the `[E#]` citations and drop any that reference unknown ids. Return `{answer, citations[], evidence[], model, grounded: bool}`.
5. **If no evidence was found**, return an "insufficient evidence" response without calling the model. **If Meta is unavailable** (no key, timeout, HTTP error), return `mode:"graph_only"` with the evidence and a clear `model_error`. There is **never** another provider.

## 7. Web UI

A single page:
- Header with repo picker and index form, with a live progress log.
- Left: overview cards and search with results.
- Center: Cytoscape graph (neighbors of the selected node, color by label, dashed edges for inferred/ambiguous).
- Right: detail panel with tabs Definition / Callers / Callees / Impact / Path, and an Ask panel with an answer, clickable citations and an evidence list.

## 8. Testing strategy

- **Unit:** URL validation, filters/secret scan, Python and JS/TS extraction, resolver (each strategy plus confidence labels), graph-name sanitization, Meta client (httpx `MockTransport`), Q&A grounding (citation filtering, insufficient evidence, graph-only fallback).
- **Pipeline (offline):** `tests/fixtures/sample_repo` indexed through `index_directory` into a `RecordingStore`. This asserts the exact node and edge set, uid uniqueness, and that re-indexing gives identical batches. Archive ingestion is tested with a synthetic in-memory tarball, including hostile members.
- **Query layer:** the BFS path/impact logic runs over an in-memory edge-provider fake. Cypher templates are checked for parameterization (no user value interpolated).
- **API:** FastAPI `TestClient` with the store dependency replaced by a fake.
- **Live (opt-in):** `ARIADNE_LIVE_FALKORDB=1` runs the fixture end to end against the configured FalkorDB (Cloud or local), including re-index duplicate checks. `ARIADNE_LIVE_META=1` makes one real Meta call. Live tests are reported separately and never claimed unless they were run.

## 9. Vercel deployment

- Root `app.py` exports `app`. Dependencies are in `pyproject.toml`, Python 3.12. Static UI is in `public/`.
- `vercel.json`: `functions."app.py".maxDuration = 300` (Hobby max; Pro can go to 800) and `excludeFiles` for tests/docs.
- **Git binary:** Vercel does not document `git` in the Python runtime, so Ariadne does not depend on it. The GitHub path is pure HTTPS. Non-GitHub hosts require git and fail clearly if it is absent.
- **Filesystem:** nothing persistent. The GitHub path keeps everything in memory, and the git path uses `/tmp` via `TemporaryDirectory` with cleanup in `finally`.
- **Limits:** default caps are `ARIADNE_MAX_FILES=1500`, `ARIADNE_MAX_FILE_KB=400`, `ARIADNE_MAX_ARCHIVE_MB=60` and `ARIADNE_MAX_SOURCE_MB=25`. Responses stay well under 4.5 MB (search/neighbor limits; source truncated).
- **DB connections:** one module-level client, created lazily, with socket timeouts. This suits Fluid compute instance reuse.

## 10. MVP scope and known limitations

- Languages: Python, JavaScript, TypeScript only.
- The resolver is static and heuristic. It has no type inference, so `obj.method()` on an unknown receiver is unresolved unless the method name is unique. Dynamic dispatch, decorators that rename, re-exports through barrels (`export * from`), and Python star imports are not followed.
- Full re-index only (no incremental, no commit history). One branch per repo graph (the latest index wins).
- Large repositories are cut at the file cap, and the summary reports this as `truncated: true`.
- Indexing time is dominated by FalkorDB Cloud round-trips. Batching keeps it to about (nodes + edges)/500 queries.

# FalkorDB Code Graph — Upstream Analysis

**Upstream:** https://github.com/FalkorDB/code-graph
**Revision analyzed:** `e3dd9ffcf5dea7df78ba33349e27f2ad618f8914` (commit dated 2026-11-01), shallow clone
**Package:** `falkordb-code-graph` v0.4.2 (`pyproject.toml`)
**License:** MIT, "Copyright (c) 2024 FalkorDB" (`LICENSE`)

This analysis is based on reading the upstream **source code**. Every claim cites the upstream file it came from. Ariadne does **not** copy upstream code. It is an original implementation that borrows architectural ideas (see §9 on licensing).

---

## 1. Architecture summary

```
            ┌─────────────── app/ (React + Vite SPA, served from app/dist) ─────────────┐
            │  App.tsx, components/code-graph.tsx, chat.tsx, ForceGraph.tsx, Input.tsx    │
            └───────────────┬────────────────────────────────────────────────────────────┘
                            │ fetch('/api/...')
┌───────────────────────────▼──────────────────────────────────────────────────────────┐
│ api/index.py  — FastAPI app: graph_entities, get_neighbors, auto_complete, list_repos,│
│                 repo_info, find_paths, chat, analyze_folder, analyze_repo,            │
│                 switch_commit, list_commits, SPA catch-all                            │
├───────────────┬───────────────────────┬───────────────────────┬──────────────────────┤
│ api/project.py│ api/analyzers/        │ api/graph.py          │ api/llm.py           │
│ clone + run   │ source_analyzer.py    │ Graph (sync writes)   │ graphrag_sdk         │
│ analysis      │ analyzer.py (ABC)     │ AsyncGraphQuery (read)│ KnowledgeGraph +     │
│               │ tree_sitter_base.py   │ naming, indexes       │ LiteLLM text→Cypher  │
│               │ python/ javascript/   │                       │ api/prompts.py       │
│               │ java/ csharp/ kotlin/ │                       │                      │
├───────────────┴───────────────────────┴───────────────────────┴──────────────────────┤
│ api/db.py — FalkorDB client factory (host/port/user/password, or FalkorDBLite)      │
│ api/info.py — repo metadata in Redis hashes ({repo}:{branch}_info)                  │
│ api/git_utils/ — commit-history graph, incremental diffs, switch_commit             │
│ api/mcp/ — MCP server + structural tools (search, callers, impact…) [out of scope]  │
│ api/cli.py — `cgraph` Typer CLI                                                     │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**Entry points**
- HTTP: `api/index.py` (`app = FastAPI()`). It also serves the built SPA from `app/dist` through a catch-all route (`serve_spa`).
- CLI: `api/cli.py` (`[project.scripts] cgraph = "api.cli:app"`).
- MCP: `api/mcp/server.py` (`cgraph-mcp`). Ariadne omits this deliberately.

**Execution flow for `POST /api/analyze_repo`** (`api/index.py:analyze_repo`)
1. `Project.from_git_repository(url)` (`api/project.py`) validates the URL with `validators.url`. It takes the project name from the last URL path segment and runs `git clone <url>` into `./repositories/<name>` on local disk (`_clone_source`).
2. `proj.analyze_sources(ignore)` → `SourceAnalyzer.analyze_local_folder` → `analyze_sources` (`api/analyzers/source_analyzer.py`):
   - `first_pass` parses every file with tree-sitter and writes `File` plus entity nodes and `DEFINES` edges **immediately, one query per node** (`Graph.add_file`, `Graph.add_entity`).
   - `link_imports` adds syntactic File→File `IMPORTS` edges.
   - `second_pass` resolves symbols through **LSP** (multilspy: jedi for Python, Eclipse JDT for Java, OmniSharp for C#) in a thread pool. It then writes `CALLS`, `EXTENDS`, `IMPLEMENTS`, `RETURNS` and `PARAMETERS` edges serially.
   - `graph.derive_overrides()` adds `OVERRIDES` edges derived in Cypher.
3. `proj.process_git_history()` builds a commit graph (`api/git_utils/git_utils.py:build_commit_graph`).
4. The analysis runs inside `run_in_executor` in the same request, so there is **no progress reporting** and no background job.

---

## 2. Repository ingestion

| Aspect | Upstream behavior | Source |
|---|---|---|
| Acquisition | `git clone <url>` via `subprocess.run([...])` (list args, so no shell injection). Full clone, not shallow. Destination `Path.cwd()/repositories/<name>` is deleted with `rmtree` first if it exists. | `api/project.py:_clone_source` |
| URL validation | `validators.url(url)` only. No host allow-list, scheme restriction or credential stripping. | `api/project.py:from_git_repository` |
| Local folders | `analyze_folder` requires a token and checks `resolved_path.is_relative_to(ALLOWED_ANALYSIS_DIR)` | `api/index.py:analyze_folder` |
| Traversal | Hard-coded `rglob` for `*.java, *.py, *.cs, *.js (excluding node_modules), *.kt, *.kts` | `source_analyzer.py:analyze_sources` |
| Ignore rules | Caller-supplied `ignore` list matched by **substring** (`any(i in str(file_path) ...)`). `.gitignore` is not honored. No default vendor/generated filtering except `node_modules` for JS. | `source_analyzer.py:first_pass` |
| Dependencies | The Python analyzer **creates a venv and runs `pip install` / `poetry install`** in the cloned repo so jedi can resolve third-party symbols. Those site-packages files are then appended to the analyzed set. | `api/analyzers/python/analyzer.py:add_dependencies` |
| Dependency marking | `is_dependency` checks for `"venv"` in the path (Python) or a `node_modules` path segment (JS). Dependency entities get nodes but no symbols. | python/javascript `analyzer.py:is_dependency` |
| Secrets | No secret detection. The clone is left on disk. | — |
| Cleanup | The clone is **not** deleted after indexing; it is reused for snippets and git history. | `api/project.py` |
| Incremental | Commit-to-commit diffs are classified into added, deleted and modified (`classify_changes`). The graph is mutated by deleting file subgraphs (`Graph.delete_files`) and re-analyzing. A query "backlog" records the transitions so `switch_commit` can replay them. | `api/git_utils/git_utils.py`, `api/graph.py:enable_backlog/_query` |
| Malformed files | tree-sitter is error-tolerant, so parse errors produce partial trees. LSP failures per file are logged and the file is excluded from edge writes (`second_pass`, "Phase A"). | `source_analyzer.py:second_pass` |

---

## 3. Parsing and code extraction

- **Technology:** `tree-sitter` 0.25 with per-language grammar wheels (`pyproject.toml`). A shared base `TreeSitterAnalyzer` declares `entity_node_types`, `type_resolution_keys` and `method_resolution_keys` (`api/analyzers/tree_sitter_base.py`). Compiled queries are memoized (`analyzer.py:_get_query`).
- **Languages registered:** `.py .java .cs .js .kt .kts`. The C analyzer is commented out (`source_analyzer.py` lines 10, 28–29). **TypeScript is not supported.**
- **Python entities:** `class_definition → Class` and `function_definition → Function`. Methods are labeled `Function` (`python/analyzer.py:entity_node_types`). The docstring is the first `expression_statement` of the body.
- **JavaScript entities:** `function_declaration → Function`, `class_declaration → Class`, `method_definition → Method`. **Arrow functions and `const f = function…` are not entities** (`javascript/analyzer.py`). The docstring is the immediately preceding `comment` sibling.
- **Hierarchy:** Nested entities are linked with `DEFINES` from their parent entity, top-level ones from the `File` (`source_analyzer.py:create_hierarchy/create_entity_hierarchy`).
- **Symbols captured:** Python captures `call` nodes, `superclasses`, typed parameters and return types. JS captures `call_expression` and `class_heritage` identifiers (`add_symbols`). Calls are captured over the entity's **whole subtree**, so a call inside a nested function is also attributed to the enclosing function.
- **Source locations:** Nodes store `path` (**absolute** path of the clone), `src_start` and `src_end` as `node.start_point.row` / `end_point.row`, which are **0-based** (`source_analyzer.py` line 57/74). No source text or signature is stored; the ontology in `llm.py` mentions `src`/`args`, but `add_entity` never sets them.
- **Snippet mismatch:** The MCP snippet reader treats `src_start` as **1-based** (`if start < 1: return None`, `enumerate(fh, start=1)` in `api/mcp/tools/structural.py:_read_snippet`). This appears to shift snippets by one line and drop snippets for definitions on line 1. Ariadne stores **1-based** lines consistently.
- **Cross-file resolution:**
  - The default is **LSP** (`AbstractAnalyzer.resolve` → `lsp.request_definition`). The definition location is then mapped to the nearest enclosing entity (`find_parent`).
  - Python can opt into a static tree-sitter resolver with `CODE_GRAPH_PY_RESOLVER=tree_sitter` (`python/ts_resolver.py`). It handles module-local names, `from X import Y [as Z]`, `import X[as Z]` + dotted access, and a **cross-project bare-name fallback**. It does not handle dynamic dispatch, star imports or inferred types (module docstring).
  - JavaScript and Kotlin use `NullLanguageServer`, and JS `needs_lsp()` returns the default `True`. As a result **JS files are skipped in `second_pass`**, and JS gets no CALLS/EXTENDS edges in the current code (`source_analyzer.py:second_pass`, lines 208–246).
  - Python import edges are purely syntactic with an exact/suffix dotted-module index (`python/analyzer.py:build_import_index/resolve_imports`). The base class's `resolve_imports` returns `[]`, so other languages get no IMPORTS edges.
- **Unresolved or ambiguous references:** Silently dropped. If resolution fails, no edge is written and nothing records that a call existed. Multiple LSP definitions produce multiple edges with no confidence marker.

---

## 4. Graph schema

**Graph naming:** one FalkorDB graph per (project, branch): `code:{project}:{branch}`, default branch `_default` (`api/graph.py:compose_graph_name`). Companion graphs use `_git`, `_schema` and `_tmp` suffixes (`_is_internal_suffix`). Repo metadata (URL, commit) lives in Redis hashes `{repo}:{branch}_info` (`api/info.py:_repo_info_key`).

**Node labels** (`api/graph.py:add_file/add_entity`, `api/llm.py:_define_ontology`)

| Label | Properties | Notes |
|---|---|---|
| `File:Searchable` | `path`, `name`, `ext` | MERGE key = (path, name, ext) |
| `Class:Searchable` | `name`, `path`, `src_start`, `src_end`, `doc` | |
| `Function:Searchable` | same | Python functions and methods |
| `Method`, `Interface`, `Struct`… | same | per-language labels (JS `Method`, Java `Interface`) |

**Edge types**

| Edge | Direction | Producer |
|---|---|---|
| `DEFINES` | File→entity, entity→nested entity | `create_hierarchy` |
| `CALLS` | entity→callable (also →Class for constructor calls in Python, because `callable_definition_node_types` includes `class_definition`) | `second_pass` |
| `EXTENDS` / `IMPLEMENTS` | Class→Class/Interface | `second_pass` |
| `RETURNS` / `PARAMETERS` | Function→Class (type references) | `second_pass` |
| `IMPORTS` | File→File | `link_imports` |
| `OVERRIDES` | Function→Function, prop `depth` | `Graph.derive_overrides` (Cypher over `EXTENDS*1..3`) |

**Identity and deduplication:** `MERGE (c:{label}:Searchable {name, path, src_start, src_end})` (`Graph.add_entity`). Edges use `MERGE (src)-[e:REL]->(dest)` by internal `ID()` (`connect_entities`), which allows **one edge per (src, rel, dest)**. Call-site lines are not kept, except in the unused `function_calls_function` helper.

**Indexes:** a range index on `File(name, ext)` and a **full-text index on `Searchable(name)`** (`Graph.__init__`). Neither `path` nor any unique id is indexed. There are no constraints.

**Write pattern:** one round-trip per node and per edge (`add_entity`, `connect_entities`). On a remote cloud database this is the main cost driver.

---

## 5. Graph queries and analysis

| Capability | Implementation | Source |
|---|---|---|
| Prefix search / autocomplete | `CALL db.idx.fulltext.queryNodes('Searchable', $prefix*)` | `graph.py:prefix_search` |
| Initial sub-graph | `MATCH (src) OPTIONAL MATCH (src)-[e]->(dest) RETURN … LIMIT 500` | `index.py:graph_entities`, `graph.py:get_sub_graph` |
| Neighbors (outgoing only in HTTP API) | `MATCH (n)-[e]->(dest) WHERE ID(n) IN $ids` | `graph.py:get_neighbors` |
| Callers / callees | `(caller)-[:CALLS]->(f)` / `(f)-[:CALLS]->(callee)` | `graph.py:function_called_by/function_calls`, `mcp/tools/structural.py:get_callers/get_callees` |
| Path finding | `MATCH p = (src)-[:CALLS*]->(dest)`, **unbounded** in the HTTP route (no `limit` passed by `index.py:find_paths`) | `graph.py:find_paths` |
| Impact analysis | `(n)<-[:CALLS*1..d]-(impacted)` with depth clamped to `IMPACT_MAX_DEPTH = 10`, MCP only | `mcp/tools/structural.py:impact_analysis` |
| Hybrid code search | BM25 over names and docstrings, path tokens, call-graph centrality, test/vendor penalty. MCP only. | `mcp/tools/structural.py:_hybrid_score` |
| Stats | node and edge counts | `graph.py:stats` |
| Unreachable entities | `WHERE not ()-[]->(n)` | `graph.py:unreachable_entities` |

**Query safety:**
- Parameters are used for values throughout.
- Labels and relation types cannot be parameterized in FalkorDB. They are string-interpolated in `get_neighbors`, `connect_entities` and `add_entity`, and the MCP layer adds a regex guard `_validate_relation` (`structural.py`, `_REL_NAME_RE`).
- Node ids are coerced to ints (`_coerce_node_id`).
- Snippet reads have a symlink-safe path guard (`_snippet_path_allowed`).

**Performance:** variable-length `CALLS*` with no upper bound in `find_paths` can explode on cyclic or dense graphs. `get_sub_graph` returns arbitrary first-500 rows.

---

## 6. Natural-language features

- `POST /api/chat` → `api/llm.py:ask` → **graphrag-sdk `KnowledgeGraph.chat_session()`** with a hand-written ontology and the LiteLLM model `MODEL_NAME` (default `gemini/gemini-flash-lite-latest`).
- Flow: **the LLM generates Cypher** (`prompts.py:CYPHER_GEN_SYSTEM/CYPHER_GEN_PROMPT`), graphrag-sdk executes it against the graph, and the LLM then summarizes the rows (`GRAPH_QA_SYSTEM/GRAPH_QA_PROMPT`).
- **Model-generated Cypher is executed directly.** There is no read-only enforcement, allow-list or validator in upstream code before execution.
- Answers carry **no citations** (file, line), and the stored entities lack source text, so answers cannot quote code.
- The ontology in `llm.py` omits `IMPORTS`, `OVERRIDES`, `Method` and others that the indexer actually writes.

**Ariadne decision:** do not reproduce text-to-Cypher. Use deterministic, parameterized retrieval templates, then ask the model to answer only from the retrieved evidence, with citation validation.

---

## 7. User-facing and operational features

- **Frontend** (`app/src`): React + Vite + Tailwind with a force-directed graph (`ForceGraph.tsx`), repo combobox (`combobox.tsx` → `/api/list_repos`), prefix search input (`Input.tsx` → `/api/auto_complete`), chat with path display (`chat.tsx` → `/api/chat`, `/api/find_paths`), commit list (`commitList.tsx`) and node data panel (`dataPanel.tsx`).
- **Auth:** `SECRET_TOKEN` bearer check. `CODE_GRAPH_PUBLIC=1` opens read endpoints. Write endpoints (`analyze_*`, `switch_commit`) always require the token (`index.py:public_or_auth/token_required`). If `SECRET_TOKEN` is unset, requests without a header are accepted (`_verify_token`).
- **Config (README "Environment variables")**: `FALKORDB_HOST/PORT/USERNAME/PASSWORD`, `CODE_GRAPH_DB_BACKEND` (`falkordb`|`lite`), `FALKORDB_LITE_*`, `SECRET_TOKEN`, `CODE_GRAPH_PUBLIC`, `MODEL_NAME` plus a provider key, `CODE_GRAPH_INDEX_WORKERS`, `CODE_GRAPH_PY_RESOLVER`, `ALLOWED_ANALYSIS_DIR`.
- **DB lifecycle:** there is a `Graph.delete()` method, but no HTTP delete endpoint. Re-analysis MERGEs into the existing graph, and stale nodes from deleted files are only removed on the git-diff path (`delete_files`).
- **Deployment:** `Dockerfile` and `docker-compose.yml` (long-running server with local disk and LSP binaries). This does **not** fit serverless: it needs full clones, `pip install` of target dependencies, JVM/.NET language servers, and a persistent `repositories/` folder.

---

## 8. Feature comparison matrix

Status legend: **Done** = implemented and covered by automated tests in this repo. **Partial** = implemented with a documented limitation. **Omitted** = deliberately out of scope.

> Verification status (2026-11-04):
> - The offline suite passes: 158 tests, using a fake FalkorDB client and mocked Meta and GitHub HTTP.
> - The live suite (`tests/test_live.py`) passes: 4/4 against **FalkorDB Cloud** and the **Meta Model API**. It covers indexing, overview, search, symbol/file detail, neighbors, path, impact, re-index with no duplicates, grounded `/ask`, and token-budget recovery.
> - Real GitHub archive ingestion of 3 public repos was checked manually.

| Upstream capability | Ariadne approach | Status |
|---|---|---|
| Git URL ingestion (`project.py`) | Allow-listed HTTPS hosts. Credential-free URL normalization. GitHub tarball streamed **in memory** (no git binary needed on Vercel). `git clone --depth 1` fallback for other hosts when git exists. Size and file-count caps. | Done |
| Local-folder analysis | Test/CLI helper only (`index_directory`). No HTTP endpoint, because a public server should not read its own disk. | Partial |
| Ignore rules | Default deny-list for vendor, build and VCS dirs. Minified, generated (`@generated`, `DO NOT EDIT`) and lock files skipped. **Secret-looking files skipped** (path names + content patterns). | Done |
| Languages | Python, JavaScript (`.js .jsx .mjs .cjs`), TypeScript (`.ts .tsx`). Java, C#, Kotlin and C omitted for MVP. | Done (3 langs) |
| Entities | File, Class (incl. TS interfaces), Function (functions, methods, arrow functions assigned to names), Package (external dependency) | Done |
| Source text | Stored on nodes (`source`, truncated) plus `signature`, `docstring`, and 1-based `line_start`/`line_end` | Done |
| LSP symbol resolution | **Replaced** by a static, original resolver: same-file, import bindings, `self`/`this`/`cls` method lookup through the inheritance chain, class-qualified calls, module attribute calls, then a unique-name heuristic. Every edge carries `resolution` + `confidence`. | Done (static, heuristic) |
| Unresolved references | Kept as `unresolved_calls` / `external_calls` lists on the caller, never silently dropped | Done |
| IMPORTS edges | Python + JS/TS relative imports → File→File. Non-relative → `DEPENDS_ON` → `Package` | Done |
| EXTENDS / IMPLEMENTS | EXTENDS (Python bases, JS/TS `extends`). TS `implements` → IMPLEMENTS. | Done |
| RETURNS / PARAMETERS | Omitted (low value for MVP Q&A) | Omitted |
| OVERRIDES | Omitted for MVP | Omitted |
| Graph-per-repo naming | `{FALKORDB_GRAPH_NAME}:{owner}__{repo}`, validated charset | Done |
| Repo metadata in Redis hash | Stored as a `Repository` node **inside** the graph (one store, no extra keys) | Done |
| Dedup via MERGE on (name,path,lines) | Deterministic `uid` (`path::Qualified.name`) + range index on `Entity.uid`. Batched `UNWIND … MERGE`. | Done |
| Re-index / incremental (git diff + backlog) | Full rebuild: parse fully in memory first, then delete the graph and write fresh. No commit history. | Partial (no incremental) |
| Commit history / switch_commit | Omitted | Omitted |
| Prefix full-text search | `toLower(name) CONTAINS` with exact > prefix > contains ranking, plus path search for files | Done |
| Neighbors | Both directions, rel-type allow-list, bounded | Done |
| Path finding (unbounded `CALLS*`) | Bounded BFS (depth ≤ 6, frontier cap) over allow-listed relation types, one parameterized query per level | Done |
| Impact analysis (MCP only) | HTTP endpoint. BFS over reverse CALLS/IMPORTS/EXTENDS with per-node minimum depth and weakest-link confidence. Affected-files rollup. | Done |
| Hybrid BM25 search (MCP) | Omitted. Q&A uses keyword evidence retrieval instead. | Omitted |
| Text-to-Cypher chat (graphrag-sdk + LiteLLM) | **Replaced**: deterministic evidence retrieval → Meta Model API → citation validation. No model-generated Cypher. | Done (mocked tests; live depends on key) |
| SPA (React/Vite) | Build-free vanilla JS + Cytoscape.js in `public/` (served by Vercel CDN) | Done |
| Auth token | Optional `ARIADNE_INDEX_TOKEN` guarding the write endpoints (index/delete) | Done |
| MCP server | **Not implemented by requirement** | Omitted |
| FalkorDBLite backend | Not used; FalkorDB Cloud only | Omitted |
| Code coverage (lcov) | Omitted | Omitted |

---

## 9. What to reproduce, adapt, or omit

**Reproduce (concept):** graph-per-repository; File/Class/Function + DEFINES/CALLS/IMPORTS/EXTENDS schema; tree-sitter parsing; callers/callees/neighbors/paths/impact; bounded depth with clamping (`IMPACT_MAX_DEPTH` idea); a relation-name allow-list before string interpolation.

**Adapt:**
- LSP becomes a static resolver, because language servers and `pip install` are infeasible on serverless.
- Per-row writes become batched UNWIND, because cloud round-trip latency dominates.
- Absolute paths become repo-relative.
- Snippets read from disk become stored source, because there is no persistent disk on Vercel.
- 0-based rows become 1-based lines.
- Redis info hashes become an in-graph Repository node.
- Unbounded path search becomes bounded BFS.
- Text-to-Cypher becomes template retrieval plus a grounded answer.

**Omit:** MCP, commit history, switch_commit, lcov coverage, FalkorDBLite, Java/C#/Kotlin/C analyzers, RETURNS/PARAMETERS/OVERRIDES, hybrid BM25 search.

---

## 10. Upstream limitations and tradeoffs observed

1. **LSP dependency** gives better precision for Python, Java and C#, but needs heavyweight binaries, target-repo dependency installs (executing `pip`/`poetry` on untrusted repos, in `python/analyzer.py:add_dependencies`), and long index times.
2. **JS gets no call edges** in the current code path (NullLanguageServer + `needs_lsp()` default True).
3. **Untrusted model Cypher** is executed by the chat feature.
4. **Unbounded path query** in the HTTP `find_paths` route.
5. **One round-trip per write**, which is slow against remote databases.
6. **Stale data on re-analyze** via `analyze_repo`, because MERGE never deletes nodes for files that no longer exist (only the git-diff path deletes them).
7. **Absolute paths** stored in the graph leak server filesystem layout.
8. **Off-by-one** between stored 0-based rows and the 1-based snippet reader (see §3).
9. **Full-text index with `prefix*`** syntax: special characters in user input are passed into the full-text query string (`graph.py:prefix_search`).
10. Clone directories persist on disk and are never cleaned.

Ariadne's own tradeoffs are documented in `docs/IMPLEMENTATION_PLAN.md` §10. The main one: a static resolver misses dynamic dispatch and has to guess on common method names, which is why every edge is confidence-labeled.

---

## 11. License and attribution

- Upstream is **MIT licensed** (`LICENSE`, "Copyright (c) 2024 FalkorDB"). MIT allows reuse with the copyright notice and permission notice preserved in copies or substantial portions.
- **Ariadne copies no upstream source code.** All Ariadne modules were written fresh. Only architectural concepts were adopted: graph-per-repo, label and edge naming, and bounded impact depth. Concepts are not copyrightable, but credit is given in `README.md` ("Acknowledgements").
- If upstream code is ever copied in the future, include the MIT notice in a `THIRD_PARTY_NOTICES.md` and in the file header.
- Third-party runtime dependencies: `falkordb` (MIT), `tree-sitter` + grammars (MIT), `fastapi` (MIT), `httpx` (BSD-3), Cytoscape.js (MIT, loaded from CDN).

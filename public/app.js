/* Ariadne web UI — vanilla JS + Cytoscape.js. All server text is HTML-escaped. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const state = {
  repo: null, current: null, cy: null, health: null, lastEvidence: [], repos: [],
  writesEnabled: true, tokenRequired: false,
  busy: { index: false, ask: false, impact: false, path: false, delete: false, scenario: false },
  lastScenarioTargets: null,
  lastPathDst: null,
  currentNode: null,                             // the loaded selected symbol (uid, name, label, path, lines)
};
const COLORS = { File: "#64748b", Class: "#8b5cf6", Function: "#0ea5e9", Package: "#f59e0b" };

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function loc(n) {
  if (!n) return "";
  // Returns plain text; callers always pass it through h().
  if (!n.line_start) return n.path || n.name || "";
  return n.path + ":" + n.line_start + (n.line_end && n.line_end !== n.line_start ? "-" + n.line_end : "");
}
function displayName(n) { return n.qualname || n.name || n.path || n.uid; }
function tag(label) { return `<span class="tag ${h(label)}">${h((label || "?").slice(0, 4))}</span>`; }
function confBadge(c) { return c && c !== "resolved" ? `<span class="conf ${h(c)}" title="heuristic link">${h(c)}</span>` : ""; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* Human-readable message from a FastAPI error body (string detail or 422 validation list). */
function errorDetail(body, status) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x.loc ? x.loc[x.loc.length - 1] + ": " : "") + (x.msg || "invalid")).join("; ");
  return d ? JSON.stringify(d) : `HTTP ${status}`;
}
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  } catch {
    throw new Error("Network error: the Ariadne server could not be reached.");
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(errorDetail(body, res.status));
  return body;
}
const repoPath = (suffix) => `/api/repos/${encodeURIComponent(state.repo)}${suffix}`;
function writeHeaders() {
  const headers = { "Content-Type": "application/json" };
  const token = $("#index-token").value.trim();
  if (token) headers["X-Ariadne-Token"] = token;
  return headers;
}

/* Latest-request-wins guard: each key keeps a counter; stale responses are ignored. */
const seqs = {};
function nextSeq(key) { seqs[key] = (seqs[key] || 0) + 1; return seqs[key]; }
function isLatest(key, n) { return seqs[key] === n; }

/* Inline status block with an optional Retry button (wired through data-action). */
function statusHtml(kind, message, retryAction) {
  return `<div class="${kind === "error" ? "error" : "muted"}">${h(message)}` +
    (retryAction ? ` <button type="button" class="ghost small-btn" data-action="${h(retryAction)}">Retry</button>` : "") + `</div>`;
}

let toastTimer;
function toast(message, kind = "ok") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "error" ? 7000 : 4000);
}

/* Enable/disable every action button from the current state, with a reason in the tooltip. */
function updateActions() {
  const idx = $("#index-btn");
  idx.disabled = state.busy.index || !state.writesEnabled;
  idx.textContent = state.busy.index ? "Indexing…" : "Index";
  idx.title = !state.writesEnabled ? "Indexing is disabled: the server has no ARIADNE_INDEX_TOKEN configured."
    : "Download, parse and store this repository's code graph";
  const del = $("#repo-delete");
  del.disabled = !state.repo || !state.writesEnabled || state.busy.delete || state.busy.index;
  del.title = !state.writesEnabled ? "Deletion is disabled: the server has no ARIADNE_INDEX_TOKEN configured."
    : !state.repo ? "Select a repository to delete" : "Delete the selected repository's graph";
  const askBtn = $("#ask-btn");
  askBtn.disabled = state.busy.ask || !state.repo;
  askBtn.textContent = state.busy.ask ? "Asking…" : "Ask";
  askBtn.title = state.repo ? "Ask a question answered from graph evidence" : "Select a repository first";
  const sc = $("#impact-btn"), hasText = !!$("#impact-scenario").value.trim();
  sc.disabled = state.busy.scenario || !state.repo || !hasText;
  sc.textContent = state.busy.scenario ? "Analyzing…" : "Analyze impact";
  sc.title = !state.repo ? "Select a repository first" : hasText ? "Match the described change to indexed code and analyze its impact"
    : "Describe a change first";
  const imp = $("#impact-symbol-btn");
  const ready = !!(state.current && state.currentNode && state.currentNode.uid === state.current);
  imp.disabled = state.busy.impact || !ready;
  imp.textContent = state.busy.impact ? "Analyzing…" : "Analyze this symbol";
  imp.title = ready ? "Find everything that depends on the selected symbol"
    : state.current ? "Loading the selected symbol…" : "Pick a symbol in the picker above, or click a graph node";
}

/* ---------------- health, repos ---------------- */
async function loadHealth() {
  $("#status-badges").innerHTML = `<span class="badge">Checking…</span>`;
  try {
    const hlt = await api("/api/health");
    state.health = hlt;
    const b = [];
    const db = hlt.falkordb;
    b.push(db.configured ? (db.reachable ? `<span class="badge ok">FalkorDB connected</span>` : `<button type="button" class="badge bad" data-action="recheck-health" title="${h(db.error || "")} — click to re-check">FalkorDB unreachable ↻</button>`)
      : `<span class="badge bad" title="Set FALKORDB_HOST and credentials on the server">FalkorDB not configured</span>`);
    b.push(hlt.config.meta_configured ? `<span class="badge ok">Meta ${h(hlt.config.meta_model)}</span>` : `<span class="badge warn" title="Answers will be graph-only until META_API_KEY is set">Meta API key missing</span>`);
    $("#status-badges").innerHTML = b.join("");
    state.tokenRequired = !!hlt.config.index_token_required;
    $("#index-token").hidden = !state.tokenRequired;
    state.writesEnabled = hlt.config.writes_enabled !== false;
  } catch (e) {
    $("#status-badges").innerHTML = `<button type="button" class="badge bad" data-action="recheck-health" title="${h(e.message)} — click to re-check">API offline ↻</button>`;
  }
  updateActions();
}

/* Refresh the repository list. Re-selects (and so reloads the panels) only when the
   selection changes or `force` is set, so a background refresh never wipes the user's work. */
async function loadRepos(selectId, { force = false } = {}) {
  const sel = $("#repo-select");
  try {
    const { repos } = await api("/api/repos");
    state.repos = repos;
    sel.innerHTML = `<option value="">— select —</option>` + repos.map((r) =>
      `<option value="${h(r.repo_id)}">${h(r.owner ? r.owner + "/" + r.name : r.repo_id)}${r.status && r.status !== "ready" ? " (" + h(r.status) + ")" : ""}</option>`).join("");
    const has = (id) => id && repos.some((r) => r.repo_id === id);
    const hash = decodeURIComponent(location.hash.slice(1));
    const want = [selectId, hash, repos[0] && repos[0].repo_id].find(has) || null;
    sel.value = want || "";
    if (!repos.length) {
      if (state.repo) await selectRepo(null);
      resetGraph("No repositories indexed yet. Paste a GitHub URL above and press Index.");
      $("#overview").textContent = "No repositories indexed yet.";
    } else if (want !== state.repo || force) {
      await selectRepo(want);
    }
  } catch (e) {
    $("#overview").innerHTML = statusHtml("error", "Could not load repositories: " + e.message, "retry-repos");
    resetGraph("");
    graphStatus("error", "Could not load repositories: " + e.message, () => loadRepos(selectId, { force }));
  }
  updateActions();
}

function repoLabel(id) {
  const r = state.repos.find((x) => x.repo_id === id);
  return r && r.owner ? r.owner + "/" + r.name : id;     // plain text: callers escape it
}

async function selectRepo(id) {
  state.repo = id || null;
  state.current = null;
  state.currentNode = null;
  graph.impactPickerOpen = false;
  state.lastPathDst = null;
  nextSeq("node"); nextSeq("ask"); nextSeq("impact"); nextSeq("path"); nextSeq("search"); nextSeq("pathSearch");
  state.busy.ask = state.busy.impact = state.busy.path = false;
  history.replaceState(null, "", id ? "#" + encodeURIComponent(id) : location.pathname);
  if ($("#repo-select").value !== (id || "")) $("#repo-select").value = id || "";
  $("#search-results").innerHTML = ""; $("#answer").innerHTML = ""; $("#path-results").innerHTML = "";
  $("#detail").textContent = "Pick a search result or a graph node.";
  $("#callers").textContent = "Select a symbol to see what calls it.";
  $("#callees").textContent = "Select a symbol to see what it calls.";
  nextSeq("scenario"); state.busy.scenario = false; clearInterval(scenarioTimer);
  $("#impact").innerHTML = `Describe a change above and press <b>Analyze impact</b>. You don’t need to pick a symbol first.`;
  $("#impact-repo").innerHTML = id ? `Repository: <b>${h(repoLabel(id))}</b>` : "Repository: —";
  $("#impact-search").value = ""; $("#impact-results").innerHTML = "";
  renderImpactSelection();
  $("#path").textContent = "";
  updateActions();
  if (!id) {
    resetGraph("Pick a repository to see its code graph.");
    $("#overview").textContent = "Pick a repository.";
    return;
  }
  showOverviewGraph();                         // loads in parallel with the overview card
  if ($("#search-input").value.trim()) doSearch();
  await loadOverview();
}

async function loadOverview() {
  const seq = nextSeq("overview"), repo = state.repo;
  $("#overview").textContent = "Loading…";
  try {
    const ov = await api(repoPath("/overview"));
    if (isLatest("overview", seq) && state.repo === repo) renderOverview(ov);
  } catch (e) {
    if (isLatest("overview", seq)) $("#overview").innerHTML = statusHtml("error", e.message, "retry-overview");
  }
}

function renderOverview(ov) {
  // The example question names a symbol that really exists in this repository.
  const top = (ov.hotspots || [])[0];
  $("#ask-input").placeholder = top ? `e.g. What calls ${displayName(top)}? What breaks if I change it?`
    : "e.g. What calls function_name? What breaks if I change ClassName?";
  renderScenarioExamples(top ? displayName(top) : null);
  const c = ov.counts || {};
  const r = ov.repo || {};
  const stats = r.stats || {};
  const calls = ov.edges.CALLS || { total: 0 };
  const list = (items, fn) => items && items.length ? `<ul class="kv">${items.map(fn).join("")}</ul>` : `<div class="muted small">none</div>`;
  const link = (uid, text) => `<a data-uid="${h(uid)}" tabindex="0" role="button" title="Open ${h(text)}">${h(text)}</a>`;
  $("#overview").innerHTML = `
    <div class="small muted">${h(r.url || "")}${r.commit ? " @ <code>" + h(String(r.commit).slice(0, 10)) + "</code>" : ""}
      ${r.status && r.status !== "ready" ? `<div class="notice">Status: ${h(r.status)} ${h(r.last_error || "")}</div>` : ""}
      ${r.truncated ? `<div class="notice">Repository exceeded indexing limits; graph is partial.</div>` : ""}</div>
    <div class="stats">
      <div class="stat"><b>${c.File || 0}</b><span>files</span></div>
      <div class="stat"><b>${c.Class || 0}</b><span>classes</span></div>
      <div class="stat"><b>${c.Function || 0}</b><span>functions</span></div>
      <div class="stat"><b>${calls.total}</b><span>calls</span></div>
    </div>
    <div class="small">Call edges: <b>${calls.resolved || 0}</b> resolved · <b>${calls.inferred || 0}</b> inferred · <b>${calls.ambiguous || 0}</b> ambiguous
      ${stats.unresolved_call_names != null ? ` · ${stats.unresolved_call_names} unresolved names` : ""}</div>
    <h3>Languages</h3>${list(Object.entries(ov.languages || {}), ([k, v]) => `<li><span>${h(k)}</span><span>${v}</span></li>`)}
    <h3>Most-called functions</h3>${list(ov.hotspots, (x) => `<li>${link(x.uid, displayName(x))}<span>${x.callers}</span></li>`)}
    <h3>Most-imported files</h3>${list(ov.most_imported, (x) => `<li>${link(x.uid, x.path)}<span>${x.importers}</span></li>`)}
    <h3>External packages</h3>${list(ov.packages, (x) => `<li><span>${h(x.name)}</span><span>${x.files}</span></li>`)}`;
}

/* ---------------- delete (with confirmation) ---------------- */
function openDeleteDialog() {
  if (!state.repo || $("#repo-delete").disabled) return;
  const dlg = $("#confirm-dialog");
  $("#confirm-text").innerHTML = `This permanently deletes the indexed code graph for <b>${h(repoLabel(state.repo))}</b> from FalkorDB. ` +
    `The source repository is not affected, and you can index it again later.`;
  $("#confirm-error").hidden = true;
  $("#confirm-ok").disabled = false; $("#confirm-ok").textContent = "Delete";
  dlg.dataset.repo = state.repo;
  dlg.showModal();
  $("#confirm-cancel").focus();                 // safe default: Enter does not delete
}
async function confirmDelete(ev) {
  ev.preventDefault();
  const dlg = $("#confirm-dialog");
  const id = dlg.dataset.repo;
  if (!id || state.busy.delete) return;
  if (state.tokenRequired && !$("#index-token").value.trim()) {
    $("#confirm-error").textContent = "This server requires the index token. Enter it in the token field at the top, then try again.";
    $("#confirm-error").hidden = false;
    return;
  }
  state.busy.delete = true; updateActions();
  $("#confirm-ok").disabled = true; $("#confirm-ok").textContent = "Deleting…";
  try {
    await api(`/api/repos/${encodeURIComponent(id)}`, { method: "DELETE", headers: writeHeaders() });
    dlg.close();
    toast(`Deleted ${repoLabel(id)}.`);
    if (state.repo === id) history.replaceState(null, "", location.pathname);
    await loadRepos(state.repo === id ? null : state.repo, { force: state.repo === id });
  } catch (e) {
    $("#confirm-error").textContent = "Delete failed: " + e.message;
    $("#confirm-error").hidden = false;
    $("#confirm-ok").disabled = false; $("#confirm-ok").textContent = "Delete";
  } finally {
    state.busy.delete = false; updateActions();
  }
}

/* ---------------- indexing ---------------- */
async function indexRepo(ev) {
  ev.preventDefault();
  if (state.busy.index) return;
  const url = $("#repo-url").value.trim();
  if (!url) { $("#repo-url").focus(); return; }
  state.busy.index = true; updateActions();
  const log = $("#progress-log");
  log.innerHTML = ""; $("#progress").hidden = false; $("#progress-title").textContent = "Indexing " + url;
  $("#progress").classList.remove("failed");
  $("#progress-bar").style.width = "4%";
  const add = (text, cls = "") => { const li = document.createElement("li"); li.textContent = text; if (cls) li.className = cls; log.appendChild(li); log.scrollTop = log.scrollHeight; };
  const fail = (text) => { add(text, "error"); $("#progress-title").textContent = "Indexing failed: " + url; $("#progress").classList.add("failed"); };
  const stagePct = { start: 5, fetch: 25, parse: 50, store: 70, done: 100 };
  let finalEvent = null;
  try {
    let res;
    try {
      res = await fetch("/api/index", { method: "POST", headers: writeHeaders(), body: JSON.stringify({ url, ref: $("#repo-ref").value.trim() || null }) });
    } catch { throw new Error("Network error: the Ariadne server could not be reached."); }
    if (!res.ok) { let body = null; try { body = await res.json(); } catch { /* non-JSON */ } throw new Error(errorDetail(body, res.status)); }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line) continue;
        const e = JSON.parse(line);
        let pct = stagePct[e.stage] || 0;
        if (e.stage === "store" && e.total) pct = 70 + Math.round(28 * (e.done || 0) / e.total);
        if (pct) $("#progress-bar").style.width = pct + "%";
        let msg = e.message;
        if (e.stage === "parse" && e.stats) msg += ` — ${e.stats.files} files, ${e.stats.classes} classes, ${e.stats.functions} functions`;
        if (e.stage === "store" && e.total) msg += ` (${e.done || 0}/${e.total})`;
        if (e.stage === "done") { const s = e.summary; msg += ` in ${s.seconds}s`; finalEvent = e; }
        if (e.stage === "error") { finalEvent = e; fail(msg); continue; }
        add(msg, e.stage === "done" ? "done" : "");
      }
    }
    if (finalEvent && finalEvent.stage === "done") {
      const s = finalEvent.summary;
      const skipped = Object.entries(s.skipped || {}).filter(([k]) => k !== "unsupported").map(([k, v]) => `${k}: ${v}`).join(", ");
      if (skipped) add("Skipped files — " + skipped);
      if (s.truncated) add("File limit reached: the graph covers part of the repository.", "error");
      $("#progress-title").textContent = "Indexed " + s.url;
      toast(`Indexed ${s.url} — ${s.stats.files} files, ${s.stats.functions} functions.`);
      await loadRepos(s.repo_id, { force: true });
    } else if (!finalEvent) {
      fail("Connection closed before indexing finished (server time limit?).");
    }
  } catch (e) {
    fail(e.message);
  } finally {
    state.busy.index = false; updateActions();
    if (!finalEvent || finalEvent.stage !== "done") loadRepos(state.repo);   // refresh statuses, keep the user's selection
  }
}

/* ---------------- search ---------------- */
function resultItem(n, extra = "") {
  const active = n.uid === state.current ? " active" : "";
  return `<li data-uid="${h(n.uid)}" class="${active.trim()}" tabindex="0" role="button" title="Open ${h(displayName(n))}">${tag(n.label)}<span class="name">${h(displayName(n))}${extra}</span><span class="loc">${h(loc(n))}</span></li>`;
}
const doSearch = debounce(async () => {
  const q = $("#search-input").value.trim();
  const ul = $("#search-results");
  const seq = nextSeq("search");
  if (!q) { ul.innerHTML = ""; return; }
  if (!state.repo) { ul.innerHTML = `<li class="muted static">Select a repository first.</li>`; return; }
  ul.innerHTML = `<li class="muted static">Searching…</li>`;
  try {
    const { results } = await api(repoPath(`/search?q=${encodeURIComponent(q)}&kind=${$("#search-kind").value}&limit=40`));
    if (!isLatest("search", seq)) return;
    ul.innerHTML = results.length ? results.map((n) => resultItem(n)).join("") : `<li class="muted static">No matches for “${h(q)}”.</li>`;
  } catch (e) {
    if (isLatest("search", seq)) ul.innerHTML = `<li class="static">${statusHtml("error", e.message, "retry-search")}</li>`;
  }
}, 220);

/* ---------------- node detail ---------------- */
function relList(items, empty) {
  if (!items || !items.length) return `<div class="muted">${h(empty)}</div>`;
  return `<ul class="results">${items.map((n) => {
    const e = n.edge || {};
    const lines = e.lines ? ` <span class="muted small">L${h(e.lines.slice(0, 4).join(","))}</span>` : "";
    return resultItem(n, confBadge(e.confidence) + lines);
  }).join("")}</ul>`;
}
function markActive() {
  $$(".results li[data-uid]").forEach((li) => li.classList.toggle("active", li.dataset.uid === state.current));
}

async function openNode(uid, { tab } = {}) {
  if (!state.repo || !uid) return;
  const seq = nextSeq("node");
  state.current = uid;
  state.currentNode = null;
  state.lastPathDst = null;
  nextSeq("impact"); nextSeq("path"); state.busy.impact = state.busy.path = false;   // results belong to the previous node
  updateActions(); markActive(); renderImpactSelection();
  $("#detail").textContent = "Loading…";
  // Switch tabs immediately so the click visibly does something while the symbol loads.
  if (tab) showTab(tab); else if ($(".tabs .active").dataset.tab === "ask") showTab("detail");
  try {
    const d = await api(repoPath(`/symbol?uid=${encodeURIComponent(uid)}`));
    if (!isLatest("node", seq)) return;
    renderDetail(d);
    $("#callers").innerHTML = relList(d.callers, "No callers in the graph.");
    $("#callees").innerHTML = relList(d.callees, "No resolved callees.") +
      (d.node.external_calls && d.node.external_calls.length ? `<h3>External calls</h3><div class="small">${d.node.external_calls.map((x) => `<code>${h(x)}</code>`).join(" ")}</div>` : "") +
      (d.node.unresolved_calls && d.node.unresolved_calls.length ? `<h3>Unresolved calls</h3><div class="small muted">${d.node.unresolved_calls.map((x) => `<code>${h(x)}</code>`).join(" ")}</div>` : "");
    state.currentNode = { ...d.node, uid };
    renderImpactSelection(); updateActions();
    $("#path").innerHTML = `<div class="muted">Search a target to find a path from <b>${h(displayName(d.node))}</b>.</div>`;
    $$("#path-results li").forEach((li) => li.classList.remove("picked"));
    markActive();
    await showNeighbors(uid);
  } catch (e) {
    if (!isLatest("node", seq)) return;
    $("#detail").innerHTML = statusHtml("error", e.message, "retry-node");
    renderImpactSelection(e.message); updateActions();
    if ($(".tabs .active").dataset.tab !== "impact") showTab("detail");
  }
}

function renderDetail(d) {
  const n = d.node;
  const parts = [`<div>${tag(n.label)} <b>${h(displayName(n))}</b> ${n.kind ? `<span class="muted small">${h(n.kind)}</span>` : ""}</div>`,
    `<div class="small muted"><code>${h(loc(n))}</code></div>`];
  if (n.signature) parts.push(`<pre>${h(n.signature)}</pre>`);
  if (n.docstring) parts.push(`<p class="small">${h(n.docstring)}</p>`);
  if (d.defined_in && d.defined_in.length) parts.push(`<h3>Defined in</h3>${relList(d.defined_in, "")}`);
  if (d.bases && d.bases.length) parts.push(`<h3>Extends / implements</h3>${relList(d.bases, "")}`);
  if (d.subclasses && d.subclasses.length) parts.push(`<h3>Subclasses</h3>${relList(d.subclasses, "")}`);
  if (d.children && d.children.length) parts.push(`<h3>Defines</h3>${relList(d.children, "")}`);
  if (d.imports) parts.push(`<h3>Imports</h3>${relList(d.imports, "No in-repo imports.")}`);
  if (d.imported_by) parts.push(`<h3>Imported by</h3>${relList(d.imported_by, "Not imported by other files.")}`);
  if (d.packages && d.packages.length) parts.push(`<h3>Packages</h3><div class="small">${d.packages.map((p) => `<code>${h(p.name)}</code>`).join(" ")}</div>`);
  if (d.used_by) parts.push(`<h3>Used by</h3>${relList(d.used_by, "Not used by any indexed file.")}`);
  if (n.source) parts.push(`<h3>Source</h3><pre>${h(n.source)}</pre>`);
  $("#detail").innerHTML = parts.join("");
}

/* ---------------- graph ----------------
 * One graph view at a time, tracked in `graph.mode`:
 *   overview  – repository graph (GET /graph), loaded when a repo is selected or indexed
 *   neighbors – neighborhood of the selected node (GET /neighbors); direction applies here
 *   impact / path – drawn from those analyses; filters hide edge types client-side
 * `graph.seq` discards responses that a newer request has superseded.
 */
const graph = { mode: null, center: null, seq: 0, autoFit: true, fitting: false, noSize: false, labels: null, hovered: null, impactPickerOpen: false };
const OVERVIEW_LIMIT = 150;
const REL_NAMES = { CALLS: "calls", IMPORTS: "imports", EXTENDS: "extends", IMPLEMENTS: "implements", DEFINES: "defines", DEPENDS_ON: "packages" };
const DIR_WORDS = { both: "", out: "outgoing ", in: "incoming " };

function selectedRels() { return $$("#rel-filters input:checked").map((i) => i.value); }
function relNames(rels) { return rels.map((r) => REL_NAMES[r] || r.toLowerCase()).join(", "); }

function graphStatus(kind, message, retry) {
  const el = $("#graph-status");
  if (!kind) { el.hidden = true; el.className = "graph-status"; el.innerHTML = ""; return; }
  el.hidden = false;
  el.className = "graph-status " + kind;
  el.innerHTML = `<div>${h(message)}</div>` + (retry ? `<div><button type="button" class="ghost">Retry</button></div>` : "");
  if (retry) el.querySelector("button").addEventListener("click", retry);
}
function setCaption(text) { const el = $("#graph-caption"); el.textContent = text || ""; el.title = text || ""; }

function updateGraphControls() {
  $("#fit-btn").disabled = !(state.cy && state.cy.nodes(":visible").length);
  $("#graph-home").disabled = !state.repo || graph.mode === "overview";
  const dirOn = graph.mode === "neighbors";
  $("#direction").disabled = !dirOn;
  $("#direction").title = dirOn ? "Edge direction relative to the selected node"
    : "Direction applies to a node's neighborhood. Click a node first.";
}
function destroyGraph() {
  if (state.cy) { state.cy.destroy(); state.cy = null; }
  graph.labels = null;
  hideTip();
  const note = $("#graph-labels"); if (note) note.hidden = true;
  updateGraphControls();
}
function resetGraph(message) {
  graph.mode = null; graph.center = null; graph.seq++;
  destroyGraph(); setCaption(""); graphStatus("empty", message);
}

/* Zoom and pan so every visible node fits in the canvas. */
/* ---------------- graph readability ----------------
 * Layout: cose tuned on the real indexed repos. The built-in defaults lay nodes out as bare 18px dots
 * (nodeDimensionsIncludeLabels: false, gravity 1, componentSpacing 40), so labels piled on top of each other.
 * Labels: a constant on-screen size, placed greedily (below / above / right / left of the node) by importance
 * so that no label overlaps another label or a node. Labels that do not fit are hidden, never their nodes or
 * edges, and the on-canvas note says how many are shown; hover shows the full name, zooming in reveals more.
 */
const LABEL_PX = 11, LABEL_MAX_W = 110, CENTER_LABEL_MAX_W = 220;   // on-screen px
const NODE_SIZE = 18, CENTER_SIZE = 30, NODE_MIN_PX = 11, CENTER_MIN_PX = 18;
const SMALL_GRAPH = 60;                          // up to this many nodes, the layout reserves room for every label
const LABEL_PLACES = [
  { v: "bottom", h: "center", my: 1, mx: 0 }, { v: "top", h: "center", my: -1, mx: 0 },
  { v: "center", h: "right", my: 0, mx: 1 }, { v: "center", h: "left", my: 0, mx: -1 }];

/* Deterministic start (circle in id order): the same graph always gets the same layout. */
function seedPositions(uids) {
  const sorted = [...uids].sort();
  const r = Math.max(200, sorted.length * 18), out = new Map();
  sorted.forEach((u, i) => out.set(u, { x: r * Math.cos((2 * Math.PI * i) / sorted.length), y: r * Math.sin((2 * Math.PI * i) / sorted.length) }));
  return out;
}

function layoutOptions(name, cy) {
  if (name === "breadthfirst") {                 // impact / path trees
    return { name: "breadthfirst", animate: false, fit: false, directed: true, spacingFactor: 1.15,
             nodeDimensionsIncludeLabels: true, avoidOverlap: true };
  }
  const small = cy.nodes().length <= SMALL_GRAPH;
  // Small graphs reserve label space (every label can fit); dense ones stay compact so nodes remain
  // visible, and placeLabels() decides which labels fit.
  const p = small ? { labels: true, defines: 60, other: 130, gravity: 0.25, components: 120 }
    : { labels: false, defines: 40, other: 90, gravity: 0.25, components: 60 };
  return {
    name: "cose", animate: false, fit: false, randomize: false, nodeDimensionsIncludeLabels: p.labels,
    nodeRepulsion: () => 40000, nodeOverlap: 20,
    idealEdgeLength: (e) => (e.data("type") === "DEFINES" ? p.defines : p.other),   // a file's symbols stay close
    edgeElasticity: (e) => (e.data("type") === "DEFINES" ? 60 : 120),
    gravity: p.gravity, componentSpacing: p.components, nestingFactor: 1.2,
    numIter: 2500, initialTemp: 1000, coolingFactor: 0.99, minTemp: 1.0,
  };
}

/* Run the layout, then fit (and place labels) once it has actually finished. */
function runLayout(name, centerUid) {
  const cy = state.cy;
  const lay = cy.layout(layoutOptions(name, cy));
  lay.one("layoutstop", () => { if (state.cy === cy) fitGraph(false); });
  lay.run();
}

function placeLabels() {
  const cy = state.cy;
  if (!cy || !cy.width() || !cy.height()) return;
  const z = cy.zoom(), gap = 3 / z, pad = 1.5 / z, ext = cy.extent();
  // Keep nodes clickable when zoomed out (>= NODE_MIN_PX on screen), but never grow a node past 90% of
  // the distance to its nearest neighbour: the layout guarantees no overlap at the base size, enlarging must not break that.
  const all = cy.nodes(":visible"), pos = all.map((n) => n.position());
  const nearest = pos.map((a, i) => pos.reduce((m, b, j) => (i === j ? m : Math.min(m, Math.hypot(a.x - b.x, a.y - b.y))), Infinity));
  cy.batch(() => {
    cy.nodes().style({ "font-size": LABEL_PX / z, "text-max-width": LABEL_MAX_W / z, "text-margin-x": 0, "text-margin-y": 0,
      "text-valign": "bottom", "text-halign": "center" });
    all.forEach((n, i) => {
      const center = n.data("center") === 1, base = center ? CENTER_SIZE : NODE_SIZE;
      const size = Math.max(base, Math.min((center ? CENTER_MIN_PX : NODE_MIN_PX) / z, 0.9 * nearest[i]));
      n.style({ width: size, height: size });
    });
    cy.nodes("[center = 1]").style("text-max-width", CENTER_LABEL_MAX_W / z);
    cy.edges().style("font-size", 9 / z);
  });
  const nodes = cy.nodes(":visible").sort((a, b) =>
    b.data("center") - a.data("center") || b.degree(false) - a.degree(false) || (a.id() < b.id() ? -1 : 1));
  const bodies = nodes.map((n) => ({ id: n.id(), b: n.boundingBox({ includeLabels: false, includeEdges: false }) }));
  const hit = (a, b) => a.x1 < b.x2 + pad && b.x1 < a.x2 + pad && a.y1 < b.y2 + pad && b.y1 < a.y2 + pad;
  const inside = (b) => b.x1 >= ext.x1 + pad && b.x2 <= ext.x2 - pad && b.y1 >= ext.y1 + pad && b.y2 <= ext.y2 - pad;
  const placed = [], choice = new Map();
  nodes.forEach((n) => {
    const lb = n.boundingBox({ includeNodes: false, includeEdges: false, includeLabels: true });
    const p = n.position(), half = parseFloat(n.style("width")) / 2;
    for (const pl of LABEL_PLACES) {
      const cx = p.x + pl.mx * (half + gap + lb.w / 2), cyy = p.y + pl.my * (half + gap + lb.h / 2);
      const box = { x1: cx - lb.w / 2, x2: cx + lb.w / 2, y1: cyy - lb.h / 2, y2: cyy + lb.h / 2 };
      if (inside(box) && !placed.some((q) => hit(q, box)) && !bodies.some((o) => o.id !== n.id() && hit(o.b, box))) {
        placed.push(box); choice.set(n.id(), pl); break;
      }
    }
  });
  cy.batch(() => nodes.forEach((n) => {
    const pl = choice.get(n.id());
    n.toggleClass("nolabel", !pl);
    if (pl) n.style({ "text-valign": pl.v, "text-halign": pl.h, "text-margin-y": pl.my * gap, "text-margin-x": pl.mx * gap });
  }));
  // Verify against Cytoscape's own rendered label geometry (the estimate above can be a pixel off):
  // in priority order, drop any label that still touches a kept label or another node.
  const kept = [], tol = 0.5 / z;                // real overlap only: touching boxes are fine
  const overlap = (a, b) => a.x1 < b.x2 - tol && b.x1 < a.x2 - tol && a.y1 < b.y2 - tol && b.y1 < a.y2 - tol;
  const onScreen = (b) => b.x1 >= ext.x1 && b.x2 <= ext.x2 && b.y1 >= ext.y1 && b.y2 <= ext.y2;
  nodes.forEach((n) => {
    if (!choice.has(n.id())) return;
    const real = n.boundingBox({ includeNodes: false, includeEdges: false, includeLabels: true });
    if (kept.some((q) => overlap(q, real)) || bodies.some((o) => o.id !== n.id() && overlap(o.b, real)) || !onScreen(real)) {
      n.addClass("nolabel"); choice.delete(n.id());
    } else kept.push(real);
  });
  graph.labels = { shown: choice.size, total: nodes.length };
  const note = $("#graph-labels");
  note.hidden = choice.size >= nodes.length;
  note.textContent = `${choice.size}/${nodes.length} labels shown`;
  note.title = `${nodes.length - choice.size} labels are hidden because they would overlap. ` +
    "Hover a node for its full name, or zoom in to reveal more labels. No nodes or edges are hidden.";
}

/* Hover: full name tooltip + neighbourhood highlight with edge-type labels. */
function highlight(node) {
  const cy = state.cy;
  graph.hovered = node || null;
  if (!cy) return;
  cy.batch(() => {
    cy.elements().removeClass("hl faded");
    if (node) { const nb = node.closedNeighborhood(); nb.addClass("hl"); cy.elements().not(nb).addClass("faded"); }
  });
  if (node) showTip(node); else hideTip();
}
function showTip(node) {
  const tip = $("#graph-tip"), p = node.renderedPosition(), box = $("#graph").getBoundingClientRect();
  tip.replaceChildren();
  const name = document.createElement("b"); name.textContent = node.data("full");
  const meta = document.createElement("div"); meta.className = "small muted";
  meta.textContent = [node.data("kind"), node.data("loc")].filter(Boolean).join(" · ");
  tip.append(name, meta);
  tip.hidden = false;
  const w = tip.offsetWidth, hgt = tip.offsetHeight;
  tip.style.left = Math.max(4, Math.min(box.width - w - 4, p.x + 14)) + "px";
  tip.style.top = Math.max(4, Math.min(box.height - hgt - 4, p.y - hgt - 10)) + "px";
}
function hideTip() { const t = $("#graph-tip"); if (t) t.hidden = true; }

function fitGraph(animate = true) {
  const cy = state.cy;
  if (!cy) return;
  cy.resize();                                   // container may have changed size since layout
  const eles = cy.elements(":visible");
  if (!eles.length) return;
  cy.stop();
  graph.fitting = true;                          // our own zoom/pan must not count as a user move
  if (animate) cy.animate({ fit: { eles, padding: 30 } }, { duration: 250, complete: () => { graph.fitting = false; placeLabels(); } });
  else { cy.fit(eles, 30); graph.fitting = false; placeLabels(); }
  graph.autoFit = true;
}
/* Container resized: keep the graph fitted unless the user has zoomed/panned since the last fit. */
function onGraphResize() {
  if (!state.cy) return;
  ensureCanvasSize();
  state.cy.resize();
  if (graph.noSize && state.cy.width() && state.cy.height()) {   // canvas became visible: draw now
    graph.noSize = false;
    graphStatus(null);
    fitGraph(false);
    return;
  }
  if (graph.autoFit) fitGraph(false);
  else placeLabels();                            // visible area changed: re-check which labels fit
}

function drawGraph(nodes, edges, centerUid, layout = "cose") {
  if (typeof cytoscape === "undefined") {
    destroyGraph();
    graphStatus("error", "The graph library (Cytoscape) failed to load. Check your network or content blocker, then reload the page.");
    return false;
  }
  destroyGraph();
  ensureCanvasSize();
  const ids = new Set(nodes.map((n) => n.uid));
  const seeds = seedPositions(nodes.map((n) => n.uid));
  const els = nodes.map((n) => ({
    data: { id: n.uid, label: n.name || n.path || n.uid, kind: n.label, center: n.uid === centerUid ? 1 : 0,
            full: displayName(n), loc: loc(n) },
    position: seeds.get(n.uid),
  })).concat(edges.filter((e) => ids.has(e.src) && ids.has(e.dst)).map((e, i) => ({ data: { id: "e" + i, source: e.src, target: e.dst, type: e.type, conf: e.confidence || "resolved" } })));
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--ink").trim() || "#222";
  const panel = css.getPropertyValue("--panel").trim() || "#fff";
  state.cy = cytoscape({
    container: $("#graph-canvas"), elements: els, minZoom: 0.05, maxZoom: 2.5,
    style: [
      // Label size and placement are managed by placeLabels() so they stay readable at any zoom.
      { selector: "node", style: { "background-color": (el) => COLORS[el.data("kind")] || "#999", label: "data(label)", color: ink,
          "font-size": LABEL_PX, "min-zoomed-font-size": 0, "text-wrap": "ellipsis", "text-max-width": LABEL_MAX_W,
          "text-valign": "bottom", "text-margin-y": 3, "text-background-color": panel, "text-background-opacity": 0.85,
          "text-background-padding": 1.5, "text-background-shape": "roundrectangle", width: NODE_SIZE, height: NODE_SIZE } },
      { selector: "node[center = 1]", style: { width: CENTER_SIZE, height: CENTER_SIZE, "border-width": 3, "border-color": "#fbbf24", "font-weight": 700 } },
      { selector: "node.nolabel", style: { "text-opacity": 0, "text-background-opacity": 0 } },
      { selector: "edge", style: { width: 1.4, "curve-style": "bezier", "target-arrow-shape": "triangle", "arrow-scale": 0.8, "line-color": "#94a3b8", "target-arrow-color": "#94a3b8",
          label: "", "font-size": 9, "min-zoomed-font-size": 0, color: "#64748b", "text-rotation": "autorotate",
          "text-background-color": panel, "text-background-opacity": 0.85, "text-background-padding": 1 } },
      { selector: "edge[type = 'CALLS']", style: { "line-color": "#0ea5e9", "target-arrow-color": "#0ea5e9" } },
      { selector: "edge[type = 'EXTENDS'], edge[type = 'IMPLEMENTS']", style: { "line-color": "#8b5cf6", "target-arrow-color": "#8b5cf6" } },
      { selector: "edge[type = 'DEFINES']", style: { "line-color": "#cbd5e1", "target-arrow-color": "#cbd5e1", width: 1 } },
      { selector: "edge[conf = 'inferred']", style: { "line-style": "dashed" } },
      { selector: "edge[conf = 'ambiguous']", style: { "line-style": "dotted" } },
      // Hover/selection: highlight the neighbourhood, name its edges, fade the rest (nothing is removed).
      { selector: "edge.hl", style: { label: "data(type)", width: 2.4, "z-index": 9 } },
      { selector: "node.hl", style: { "z-index": 9, "text-opacity": 1, "text-background-opacity": 0.95 } },
      { selector: ".faded", style: { opacity: 0.18 } },
      { selector: ".filtered", style: { display: "none" } },
    ],
    layout: { name: "preset", fit: false },
  });
  state.cy.on("tap", "node", (evt) => { hideTip(); openNode(evt.target.id()); });
  state.cy.on("mouseover", "node", (evt) => highlight(evt.target));
  state.cy.on("mouseout", "node", () => highlight(null));
  // Leaving the canvas straight from a node fires no node mouseout: clear the hover state here too.
  $("#graph-canvas").onmouseleave = () => highlight(null);
  state.cy.on("tapstart", () => hideTip());
  state.cy.on("zoom", debounce(() => placeLabels(), 90));
  state.cy.on("zoom pan", () => { if (!graph.fitting) graph.autoFit = false; if (graph.hovered) highlight(null); });
  runLayout(layout, centerUid);
  updateGraphControls();
  // Never report a drawn graph on a canvas that cannot show it.
  if (!state.cy.width() || !state.cy.height()) {
    graph.noSize = true;
    graphStatus("error", "The graph is loaded but its canvas has no visible size, so nothing can be drawn. " +
      "Widen the window, or reload the page (Ctrl+F5) if it stays blank.");
  } else {
    graph.noSize = false;
    graphStatus(null);
  }
  return true;
}

/* The canvas gets its size from inline styles (index.html) and .graph-canvas. If neither applied
   (stale or blocked stylesheet), size it explicitly from its #graph parent. */
function ensureCanvasSize() {
  const c = $("#graph-canvas");
  if (c.clientWidth > 0 && c.clientHeight > 0) return true;
  Object.assign(c.style, { position: "absolute", top: "0", right: "0", bottom: "0", left: "0" });
  return c.clientWidth > 0 && c.clientHeight > 0;
}

/* impact / path views: hide edge types that are unticked (and nodes left with no visible edge). */
function applyClientFilter() {
  const cy = state.cy;
  if (!cy) return 0;
  const rels = new Set(selectedRels());
  let hidden = 0;
  cy.batch(() => {
    cy.edges().forEach((e) => { const off = !rels.has(e.data("type")); e.toggleClass("filtered", off); if (off) hidden++; });
    cy.nodes().forEach((n) => n.toggleClass("filtered", n.data("center") !== 1 &&
      n.connectedEdges().filter((e) => !e.hasClass("filtered")).length === 0));
  });
  placeLabels();
  updateGraphControls();
  return hidden;
}

async function loadGraph() {
  if (!state.repo || !graph.mode) return;
  if (graph.mode === "impact" || graph.mode === "path") {
    const hidden = applyClientFilter();
    const base = $("#graph-caption").dataset.base || "";
    setCaption(base + (hidden ? ` · ${hidden} edge(s) hidden by filters` : ""));
    return;
  }
  const rels = selectedRels();
  const seq = ++graph.seq;
  const mode = graph.mode, center = graph.center, dir = $("#direction").value;
  if (!rels.length) {
    destroyGraph(); setCaption("");
    graphStatus("empty", "No relationship types selected. Tick at least one filter above.");
    return;
  }
  graphStatus("loading", mode === "overview" ? "Loading repository graph…" : "Loading neighborhood…");
  try {
    const g = mode === "overview"
      ? await api(repoPath(`/graph?rels=${rels.join(",")}&limit=${OVERVIEW_LIMIT}`))
      : await api(repoPath(`/neighbors?uid=${encodeURIComponent(center)}&rels=${rels.join(",")}&direction=${dir}&limit=80`));
    if (seq !== graph.seq) return;               // superseded by a newer request
    if (mode === "overview") {
      if (!g.nodes.length) {
        destroyGraph(); setCaption("");
        graphStatus("empty", `This repository has no ${relNames(rels)} relationships. Enable more relationship types above.`);
        return;
      }
      if (!drawGraph(g.nodes, g.edges, null, "cose")) return;
      setCaption(`Repository graph · ${g.truncated ? `${g.nodes.length} most-connected of ${g.total_nodes}` : g.nodes.length} nodes · ` +
        `${g.edges.length} edges (${relNames(rels)}). Click a node to explore its neighborhood.`);
    } else {
      if (!drawGraph(g.nodes, g.edges, center, "cose")) return;
      const c = g.nodes.find((n) => n.uid === center);
      const name = c ? displayName(c) : center;
      setCaption(g.edges.length
        ? `Neighborhood of ${name} · ${g.edges.length} ${DIR_WORDS[dir]}edges (${relNames(rels)})`
        : `${name} has no ${DIR_WORDS[dir]}${relNames(rels)} relationships. Try another direction or more relationship types.`);
    }
  } catch (e) {
    if (seq !== graph.seq) return;
    destroyGraph(); setCaption("");
    graphStatus("error", "Could not load the graph: " + e.message, loadGraph);
  }
}
function showOverviewGraph() { graph.mode = "overview"; graph.center = null; updateGraphControls(); return loadGraph(); }
function showNeighbors(uid) { graph.mode = "neighbors"; graph.center = uid; updateGraphControls(); return loadGraph(); }
/* Draw an analysis result (impact / path) and take over the canvas. */
function showAnalysisGraph(mode, nodes, edges, centerUid, caption) {
  graph.mode = mode; graph.center = centerUid; graph.seq++;   // cancels any in-flight graph request
  if (!drawGraph(nodes, edges, centerUid, "breadthfirst")) return;
  $("#graph-caption").dataset.base = caption;
  loadGraph();                                   // applies the current filters + caption
}

/* ---------------- impact ----------------
 * Selected symbol: the same state.current / state.currentNode used by the graph, Search,
 * Definition, Callers and Callees. The Impact tab shows it and has its own picker
 * (GET /search: indexed symbols only); picking opens the symbol like a graph click does,
 * which also centres and highlights it in the graph.
 *
 * Report (GET /impact → report): 1 Summary (incl. coverage) · 2 Dependency paths ·
 * 3 Architecture & layers · 4 User flows / UI impact · 5 Affected files · 6 Explanation & evidence.
 * Badges: "Graph-confirmed" (a graph edge), "Confirmed by syntax" (an explicit construct such as a
 * route decorator or JSX), "Inferred" (naming convention or URL match). The client invents nothing.
 */
const IMPACT_RELS = ["CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS"];
const IMPACT_VERBS = { CALLS: "calls", IMPORTS: "imports", EXTENDS: "extends", IMPLEMENTS: "implements", MEMBER_OF: "is defined in" };
const UI_LAYERS = ["UI page", "UI component", "UI event handler"];

function impactDepth() {
  const el = $("#impact-depth");
  let d = parseInt(el.value, 10);
  if (!Number.isFinite(d)) d = 3;
  d = Math.max(1, Math.min(6, d));
  el.value = d;                                  // show the value actually used
  return d;
}

/* ----- selected symbol + picker ----- */
function renderImpactSelection(error) {
  const box = $("#impact-selected"), picker = $("#impact-picker");
  const n = state.currentNode;
  if (error) {
    box.innerHTML = `<div class="small muted">Selected symbol</div>${statusHtml("error", "Could not load the selected symbol: " + error)}`;
    picker.hidden = false;
  } else if (!state.current) {
    box.innerHTML = `<div class="small muted">Selected symbol</div><div><b>No symbol selected.</b> Search below, or click a node in the graph or a search result.</div>`;
    picker.hidden = false;
  } else if (!n || n.uid !== state.current) {
    box.innerHTML = `<div class="small muted">Selected symbol</div><div class="muted">Loading <code>${h(state.current)}</code>…</div>`;
  } else {
    box.innerHTML = `<div class="small muted">Selected symbol</div>
      <div class="impact-sel-row">${tag(n.label)} <a class="sym target" data-uid="${h(n.uid)}" tabindex="0" role="button" title="Open definition">${h(displayName(n))}</a>
        ${n.kind ? `<span class="muted small">${h(n.kind)}</span>` : ""}
        <button type="button" class="ghost small-btn" id="impact-change" title="Pick a different symbol">Change symbol</button></div>
      <div class="small muted"><code>${h(loc(n) || n.uid)}</code></div>`;
    picker.hidden = !graph.impactPickerOpen;
  }
}
const impactSearch = debounce(async () => {
  const q = $("#impact-search").value.trim();
  const ul = $("#impact-results");
  const seq = nextSeq("impactSearch");
  if (!q) { ul.innerHTML = ""; return; }
  if (!state.repo) { ul.innerHTML = `<li class="muted static">Select a repository first.</li>`; return; }
  ul.innerHTML = `<li class="muted static">Searching…</li>`;
  try {
    const { results } = await api(repoPath(`/search?q=${encodeURIComponent(q)}&limit=12`));
    if (!isLatest("impactSearch", seq)) return;
    ul.innerHTML = results.length ? results.map((n) => resultItem(n)).join("") : `<li class="muted static">No indexed symbol matches “${h(q)}”.</li>`;
  } catch (e) {
    if (isLatest("impactSearch", seq)) ul.innerHTML = `<li class="static">${statusHtml("error", e.message, "retry-impact-search")}</li>`;
  }
}, 220);
function pickForImpact(uid) {
  graph.impactPickerOpen = false;
  $("#impact-search").value = "";
  $("#impact-results").innerHTML = "";
  openNode(uid, { tab: "impact" });              // same selection path as a graph click: also centres it in the graph
}

/* ----- report rendering helpers ----- */
function basisBadge(basis) {
  const map = { graph: ["confirmed", "Graph-confirmed"], syntax: ["confirmed", "Confirmed by syntax"],
                "graph-inferred": ["inferred", "Graph · inferred calls"], heuristic: ["inferred", "Inferred"],
                caveat: ["caveat", "Potential impact"] };
  const [cls, label] = map[basis] || ["inferred", "Inferred"];
  return `<span class="basis ${cls}">${h(label)}</span>`;
}
function reachBadge(reach) {
  return reach === "graph" ? `<span class="basis confirmed">Graph-confirmed path</span>`
    : `<span class="basis inferred">Inferred link</span>`;
}
function symChip(u, rep, target, withLoc = false) {
  const n = rep.nodes[u] || { uid: u };
  const name = n.qualname || n.name || n.path || u;
  const where = loc(n);
  const a = `<a class="sym${u === target ? " target" : ""}" data-uid="${h(u)}" tabindex="0" role="button" title="${h(where || name)} — open definition">${h(name)}</a>`;
  return withLoc ? `<span class="node">${a}${where ? `<span class="where">${h(where)}</span>` : ""}</span>` : a;
}
function edgeArrow(e) {
  let label = IMPACT_VERBS[e.type] || String(e.type || "").toLowerCase();
  if (e.syntax === "jsx-render") label = "renders";
  else if (e.syntax === "jsx-event") label = `wires ${e.event || "event"}`;
  else if (e.type === "HTTP") label = `HTTP ${e.detail || ""} · inferred`;
  const inferred = e.type === "HTTP" || (e.confidence && e.confidence !== "resolved");
  const lines = e.lines && e.lines.length ? ` · call at line ${e.lines.slice(0, 3).join(", ")}` : "";
  const conf = e.type !== "HTTP" && e.confidence && e.confidence !== "resolved" ? ` (${e.confidence})` : "";
  return `<span class="arrow${inferred ? " inferred" : ""}" title="${h(label + conf + lines)}">→ <span class="small">${h(label)}${h(conf)}</span></span>`;
}
function chainHtml(nodes, edges, rep, target) {
  return `<div class="chain">${nodes.map((u, i) => symChip(u, rep, target, true) + (i < edges.length ? edgeArrow(edges[i]) : "")).join("")}</div>`;
}
function layerChips(layers) {
  return (layers || []).map((l) => `<span class="layer ${l.basis === "syntax" ? "confirmed" : "inferred"}" title="${h((l.basis === "syntax" ? "Confirmed by syntax: " : "Inferred: ") + l.evidence)}">${h(l.layer)}${l.basis === "syntax" ? "" : " · inferred"}</span>`).join("");
}
function renderInline(text) { return h(text).replace(/`([^`]+)`/g, "<code>$1</code>"); }   /* backticks → <code>; all else escaped */

function renderImpact(r) {
  const rep = r.report, s = rep.summary, t = s.target, c = rep.coverage, ui = rep.ui;
  const tn = rep.nodes[t] || r.target;
  const out = [];

  // 1. Summary (incl. coverage)
  const notEstablished = [];
  if (ui.status !== "established") notEstablished.push("UI/API flow");
  if (!rep.paths.length) notEstablished.push("dependents");
  const targets = s.targets || [t];
  const layerNames = [...new Set(rep.layers.map((l) => l.layer))];
  out.push(`<section class="imp-sec" aria-label="Summary"><h3>Summary</h3>
    ${targets.length > 1 ? `<div class="imp-head small">Targets: ${targets.map((u) => symChip(u, rep, t, true)).join(" ")}</div>`
      : `<div class="imp-head">${symChip(t, rep, t)} <span class="muted small"><code>${h(s.location || "")}</code></span></div>`}
    <div class="chips">${layerChips(s.target_layers) || `<span class="muted small">No architectural layer identified for the selected symbol.</span>`}</div>
    <div class="stats imp-stats">
      <div class="stat"><b>${s.direct}</b><span>direct</span></div><div class="stat"><b>${s.transitive}</b><span>transitive</span></div>
      <div class="stat"><b>${s.files}</b><span>files</span></div><div class="stat"><b>${s.depth}</b><span>depth</span></div>
    </div>
    <div class="small">Potentially affected layers: ${layerNames.length ? h(layerNames.join(", ")) : `<span class="muted">none identified</span>`}</div>
    <div class="small">Relationships followed: ${s.rels.map((x) => `<code>${h(x)}</code>`).join(" ")}${s.seeds > 1 ? ` · includes ${s.seeds - 1} member(s) of the selected ${h(tn.label || "")}` : ""}${c.truncated ? ` · <span class="error">result truncated</span>` : ""}</div>
    <div class="small muted coverage-line">Coverage: graph version ${c.graph_version}, ${Object.entries(c.languages).map(([k, v]) => `${v} ${h(k)}`).join(", ") || "no"} files indexed; ${c.unresolved_calls_in_scope} unresolved call site(s) not followed${notEstablished.length ? `; not established: ${h(notEstablished.join(", "))}` : ""}.</div>
  </section>`);

  // 2. Dependency paths (grouped by entry point)
  if (rep.paths.length) {
    const groups = new Map();
    rep.paths.forEach((p) => { if (!groups.has(p.root)) groups.set(p.root, []); groups.get(p.root).push(p); });
    out.push(`<section class="imp-sec"><h3>Dependency paths <span class="muted small">(${rep.paths.length}, ${groups.size} entry point${groups.size > 1 ? "s" : ""})</span></h3>
      ${[...groups.entries()].map(([root, ps], gi) => `<details class="imp-group" ${gi < 4 ? "open" : ""}><summary>${symChip(root, rep, t)} ${layerChips(((rep.nodes[root] || {}).layers || []).slice(0, 2))}
        <span class="muted small">${ps.length} path${ps.length > 1 ? "s" : ""}</span></summary>
        ${ps.map((p) => `<div class="imp-path"><span class="kind ${p.kind}">${p.kind === "direct" ? "Direct" : "Transitive"}</span>
          ${p.confidence === "resolved" ? basisBadge("graph") : `<span class="conf ${h(p.confidence)}" title="includes heuristic call resolution">graph · ${h(p.confidence)} calls</span>`}
          ${chainHtml(p.nodes, p.edges, rep, t)}</div>`).join("")}</details>`).join("")}</section>`);
  } else {
    out.push(`<section class="imp-sec"><h3>Dependency paths</h3><div class="muted">No dependents found following ${h(s.rels.join(", "))} up to depth ${s.depth}. Try a greater depth or more relationship types.</div></section>`);
  }

  // 3. Architecture & layers
  out.push(`<section class="imp-sec"><h3>Architecture &amp; layers</h3>${rep.layers.length ? rep.layers.map((l) => `
    <details class="imp-group"><summary><b>${h(l.layer)}</b> <span class="muted small">${l.members.length} · ${l.confirmed} confirmed by syntax, ${l.inferred} inferred</span></summary>
      <ul class="imp-list">${l.members.map((m) => `<li>${symChip(m.uid, rep, t, true)} ${basisBadge(m.basis)} ${reachBadge(m.reach)}
        <div class="small muted">${h(m.evidence)}</div></li>`).join("")}</ul>
    </details>`).join("") : `<div class="muted small">No architectural layer could be identified among the dependents. Nothing is guessed.</div>`}</section>`);

  // 4. User flows / UI impact
  const uiPaths = rep.paths.filter((p) => p.nodes.some((u) => ((rep.nodes[u] || {}).layers || []).some((l) => UI_LAYERS.includes(l.layer))));
  out.push(`<section class="imp-sec"><h3>User flows / UI impact</h3>
    ${ui.status === "established"
      ? `<div class="notice ok">${h(ui.message)}</div>
         ${ui.flows.map((f) => `<div class="imp-path">${chainHtml(f.nodes, f.edges, rep, t)}<div class="small muted">${basisBadge("heuristic")} ${h(f.link.call)} ⇢ ${h(f.link.route)}: ${h(f.link.rule)}</div></div>`).join("")}
         ${!ui.flows.length ? uiPaths.slice(0, 5).map((p) => `<div class="imp-path">${basisBadge("graph")}${chainHtml(p.nodes, p.edges, rep, t)}</div>`).join("") : ""}`
      : `<div class="notice ${ui.status === "no_frontend" ? "muted" : "warn"}"><b>Not established from indexed code.</b> ${h(ui.message)}</div>`}
  </section>`);

  // 5. Affected files, with their affected symbols and line references
  const byFile = new Map();
  Object.values(rep.nodes).filter((n) => n.depth > 0 && n.path).forEach((n) => {
    if (!byFile.has(n.path)) byFile.set(n.path, []);
    byFile.get(n.path).push(n);
  });
  out.push(`<section class="imp-sec"><h3>Affected files</h3>${byFile.size ? `<ul class="imp-files">${[...byFile.entries()]
    .sort((a, b) => b[1].length - a[1].length || (a[0] < b[0] ? -1 : 1)).map(([path, ns]) => `<li>
      <a data-uid="${h("file:" + path)}" tabindex="0" role="button" title="Open ${h(path)}">${h(path)}</a> <span class="muted small">${ns.length} symbol${ns.length > 1 ? "s" : ""}</span>
      <div class="chips">${ns.sort((a, b) => (a.line_start || 0) - (b.line_start || 0)).map((n) => `${symChip(n.uid, rep, t)}<span class="where">${n.line_start ? "L" + n.line_start + (n.line_end && n.line_end !== n.line_start ? "–" + n.line_end : "") : ""}</span>`).join(" ")}</div>
    </li>`).join("")}</ul>` : `<div class="muted small">No affected files (no dependents found).</div>`}</section>`);

  // 6. Explanation & evidence
  const ins = rep.inspection;
  out.push(`<section class="imp-sec"><h3>Suggested inspection</h3>
    ${ins.callers.length ? `<div class="small">Direct callers: ${ins.callers.map((u) => symChip(u, rep, t, true)).join(" ")}</div>` : `<div class="small muted">No direct callers.</div>`}
    ${ins.files.length ? `<div class="small">Files with the most affected symbols: ${ins.files.slice(0, 5).map((f) => `<a data-uid="${h("file:" + f.path)}" tabindex="0" role="button">${h(f.path)}</a> (${f.symbols})`).join(", ")}</div>` : ""}
    ${ins.tests.length ? `<div class="small">Tests reaching it:</div><ul class="imp-list">${ins.tests.map((x) =>
      `<li>${symChip(x.uid, rep, t, true)} ${basisBadge(x.basis)} <span class="small muted">${h(x.evidence)}</span></li>`).join("")}</ul>`
      : `<div class="small muted">${h(ins.tests_note || "")}</div>`}
  </section>`);
  out.push(`<section class="imp-sec"><h3>Explanation &amp; evidence</h3>
    <div class="small muted">Potential impact derived from graph paths, not verified breakage.</div>
    <ul class="imp-list">${rep.explanation.map((e) => `<li>${basisBadge(e.basis)} ${renderInline(e.text)}</li>`).join("")}</ul>
    ${rep.behaviors.length ? `<h4>Potentially affected behaviour</h4><ul class="imp-list">${rep.behaviors.map((b) =>
      `<li>${basisBadge(b.basis)} ${renderInline(b.text)} <span class="small muted" title="${h(b.evidence)}">(${h(b.layer)})</span></li>`).join("")}</ul>` : ""}
    <details class="imp-group"><summary><b>Evidence &amp; coverage</b></summary>
      <ul class="imp-list small">
        <li>Graph version ${c.graph_version}: ${h(c.facts_source)}${c.derived_nodes ? ` (${c.derived_nodes} symbol(s) read from stored source)` : ""}</li>
        <li>${c.files_in_repo} files indexed · ${c.frontend_files} JSX/TSX files · ${c.api_clients_found} JS/TS API client(s)</li>
        <li>Edge confidence along the paths: ${Object.entries(c.edge_confidence).map(([k, v]) => `${v} ${h(k)}`).join(", ") || "—"}</li>
        <li>${c.unresolved_calls_in_scope} call site(s) in the analysed code could not be resolved and were not followed.</li>
        ${c.not_analysed.map((x) => `<li class="muted">Not analysed: ${h(x)}</li>`).join("")}
      </ul></details>
  </section>`);
  return out.join("");
}

/* A server that predates the report still returns the legacy list: say so instead of failing. */
function renderLegacyImpact(r) {
  return `<div class="notice warn">This server returned impact data without the expanded report, so it is running an older
    Ariadne version. Restart it (<code>python -m uvicorn app:app</code>) to get dependency paths, layers and UI impact.</div>
    <div>${r.impacted.length} dependent(s):</div><ul class="results">${r.impacted.map((n) => resultItem(n, ` <span class="muted small">depth ${h(n.depth)}</span>`)).join("")}</ul>`;
}

async function runImpact() {
  if (!state.current || !state.currentNode || state.currentNode.uid !== state.current) {
    $("#impact").innerHTML = statusHtml("error", "Pick a symbol in “Or analyze one specific symbol”, or describe a change above.");
    return;
  }
  if (state.busy.impact) return;
  const rels = selectedRels().filter((x) => IMPACT_RELS.includes(x));
  if (!rels.length) {
    $("#impact").innerHTML = statusHtml("error", "Tick at least one of calls, imports, extends or implements above the graph: impact follows those relationships.");
    return;
  }
  const seq = nextSeq("impact"), uid = state.current, depth = impactDepth();
  state.busy.impact = true; updateActions();
  $("#impact").innerHTML = `<div class="muted">Analyzing dependents of <b>${h(displayName(state.currentNode))}</b> (depth ${depth})…</div>`;
  try {
    const r = await api(repoPath(`/impact?uid=${encodeURIComponent(uid)}&depth=${depth}&rels=${rels.join(",")}`));
    if (!isLatest("impact", seq)) return;
    $("#impact").innerHTML = r.report ? renderImpact(r) : renderLegacyImpact(r);
    if (r.impacted.length) {
      const nodes = [r.target, ...r.impacted];
      const edges = r.impacted.flatMap((n) => (n.parents && n.parents.length ? n.parents : [{ to: n.from, type: n.via, confidence: n.confidence }])
        .map((p) => ({ src: n.uid, dst: p.to, type: p.type, confidence: p.confidence })));
      showAnalysisGraph("impact", nodes.filter((n) => n.uid), edges, r.target.uid,
        `Impact of ${displayName(r.target)} · ${r.impacted.length} dependents (depth ${r.depth})`);
    }
  } catch (e) {
    if (isLatest("impact", seq)) $("#impact").innerHTML = statusHtml("error", e.message, "retry-impact");
  } finally {
    if (isLatest("impact", seq)) { state.busy.impact = false; updateActions(); }
  }
}

/* ---------------- impact scenario (primary workflow) ----------------
 * POST /impact/scenario {scenario, targets?, depth, rels, use_model}
 *   → resolution {status: analyzed | needs_confirmation | no_match, candidates, notes, model}
 *   → report (same deterministic impact report as the symbol workflow) when targets are settled.
 * Targets are never chosen silently: weak/ambiguous matches come back for confirmation, and the
 * user can always tick/untick candidates and re-analyze.
 */
const SCENARIO_EXAMPLES = [
  "If I change the message format returned by the API, what could be affected?",
  "What happens if authentication middleware changes?",
  "If I rename this field in the user model, which routes, services, and UI components depend on it?",
  "If I change the retry behavior, what functions and tests should I inspect?",
  "What could break if this API response becomes nullable?",
];
const CONF_ORDER = { high: 0, medium: 1, low: 2 };

function renderScenarioExamples(hotspot) {
  const ex = (hotspot ? [`If I change ${hotspot}, what could be affected?`] : []).concat(SCENARIO_EXAMPLES);
  $("#impact-examples").innerHTML = ex.map((t) => `<button type="button" class="chip-btn" data-example="${h(t)}" title="Use this example">${h(t)}</button>`).join("");
}
function scenarioText() { return $("#impact-scenario").value.trim(); }

function renderTargets(resp) {
  const res = resp.resolution;
  const cands = res.candidates || [];
  const status = { analyzed: "ok", needs_confirmation: "warn", no_match: "muted" }[res.status] || "warn";
  const model = res.model || {};
  const rows = cands.map((c) => `<li class="target-row">
      <label class="target-pick"><input type="checkbox" data-target="${h(c.uid)}" ${c.selected || (res.status === "needs_confirmation" && c.suggested) ? "checked" : ""}
        aria-label="Use ${h(c.qualname || c.name || c.uid)} as a target"></label>
      <div class="target-body">
        <div>${tag(c.label)} <a class="sym" data-uid="${h(c.uid)}" tabindex="0" role="button" title="Open definition">${h(c.qualname || c.name || c.path || c.uid)}</a>
          <span class="conf-badge ${h(c.confidence)}">${h(c.confidence)} confidence</span>
          ${c.ambiguous ? `<span class="basis inferred" title="Several symbols share this name">ambiguous</span>` : ""}
          ${c.model_suggested ? `<span class="basis inferred" title="${h(c.model_reason || "")}">model-ranked</span>` : ""}</div>
        <div class="where">${h(loc(c) || c.uid)}</div>
        <div class="small muted">${h(c.match)} match · ${h((c.reasons || []).join("; "))}</div>
      </div></li>`).join("");
  return `<section class="imp-sec" aria-label="Matched targets"><h3>Matched targets</h3>
    <div class="notice ${status}">${h(res.message)}</div>
    ${cands.length ? `<ul class="imp-list targets">${rows}</ul>
      <button type="button" class="ghost" id="impact-reanalyze">Analyze selected targets</button>` : ""}
    ${res.unmatched && res.unmatched.length ? `<div class="small muted">Not found in the graph: ${res.unmatched.map((u) => `<code>${h(u)}</code>`).join(", ")}</div>` : ""}
    ${res.concepts && res.concepts.length ? `<div class="small muted">Concepts recognised: ${h(res.concepts.join(", "))} (offered only where indexed syntax confirms them)</div>` : ""}
    ${model.used ? `<div class="small muted">Meta model ${h(model.model || "")}: ranked ${(model.picked || []).length} of the candidates shown to it${model.rejected && model.rejected.length ? `; rejected ${model.rejected.length} reference(s) to things not in the candidate list` : ""}${model.error ? ` · ${h(model.error)}` : ""}.</div>` : ""}
    ${(res.notes || []).map((n) => `<div class="notice">${h(n)}</div>`).join("")}
  </section>`;
}

function renderScenario(resp) {
  const parts = [`<section class="imp-sec" aria-label="Scenario"><h3>Scenario</h3>
    <div class="scenario-text">${h(resp.scenario)}</div>
    <div class="small muted">Repository: ${h(resp.repo.label)} · read-only analysis of the indexed graph</div></section>`,
    renderTargets(resp)];
  if (resp.report) parts.push(renderImpact(resp.report));
  return parts.join("");
}

function checkedTargets() { return $$("#impact input[data-target]:checked").map((i) => i.dataset.target); }
function updateReanalyze() {
  const btn = document.getElementById("impact-reanalyze");   // rendered with the matched targets
  if (btn) { const n = checkedTargets().length; btn.disabled = !n || state.busy.scenario; btn.textContent = n ? `Analyze ${n} selected target${n > 1 ? "s" : ""}` : "Select at least one target"; }
}

let scenarioTimer;
async function runScenario(targets) {
  const text = scenarioText();
  if (!state.repo) { $("#impact").innerHTML = statusHtml("error", "Select a repository first."); return; }
  if (!text) { $("#impact").innerHTML = statusHtml("error", "Describe the change you’re considering first."); $("#impact-scenario").focus(); return; }
  if (state.busy.scenario) return;
  const rels = selectedRels().filter((x) => IMPACT_RELS.includes(x));
  if (!rels.length) {
    $("#impact").innerHTML = statusHtml("error", "Tick at least one of calls, imports, extends or implements above the graph: impact follows those relationships.");
    return;
  }
  const seq = nextSeq("scenario"), depth = impactDepth(), useModel = $("#impact-use-model").checked;
  state.busy.scenario = true; state.lastScenarioTargets = targets || null; updateActions();
  const started = Date.now();
  const progress = () => {
    const s = Math.round((Date.now() - started) / 1000);
    $("#impact").innerHTML = `<div class="muted">${targets ? `Analyzing ${targets.length} selected target(s)` : "Matching your description to indexed symbols"} (depth ${depth})… ${s}s
      ${!targets && useModel && s >= 3 ? `<div class="small">If there is no exact match, the Meta model is ranking real candidates; this can take up to a minute.</div>` : ""}</div>`;
  };
  progress(); clearInterval(scenarioTimer); scenarioTimer = setInterval(progress, 1000);
  try {
    const body = { scenario: text, depth, rels, use_model: useModel, ...(targets ? { targets } : {}) };
    const r = await api(repoPath("/impact/scenario"), { method: "POST", body: JSON.stringify(body) });
    if (!isLatest("scenario", seq)) return;
    clearInterval(scenarioTimer);
    $("#impact").innerHTML = renderScenario(r);
    updateReanalyze();
    const rep = r.report;
    // No report (targets still to confirm / no match): don't leave an earlier impact on the canvas.
    if ((!rep || !rep.impacted.length) && graph.mode === "impact") showOverviewGraph();
    if (rep && rep.impacted.length) {
      const nodes = [...rep.targets, ...rep.impacted];
      const edges = rep.impacted.flatMap((n) => (n.parents && n.parents.length ? n.parents : [{ to: n.from, type: n.via, confidence: n.confidence }])
        .map((p) => ({ src: n.uid, dst: p.to, type: p.type, confidence: p.confidence })));
      showAnalysisGraph("impact", nodes.filter((n) => n.uid), edges, rep.target.uid,
        `Impact of scenario · ${rep.targets.length} target(s) · ${rep.impacted.length} dependents (depth ${rep.depth})`);
    }
  } catch (e) {
    if (isLatest("scenario", seq)) { clearInterval(scenarioTimer); $("#impact").innerHTML = statusHtml("error", e.message, "retry-scenario"); }
  } finally {
    if (isLatest("scenario", seq)) { state.busy.scenario = false; updateActions(); updateReanalyze(); }
  }
}

/* ---------------- path ---------------- */
const pathSearch = debounce(async () => {
  const q = $("#path-target").value.trim();
  const ul = $("#path-results");
  const seq = nextSeq("pathSearch");
  if (!q) { ul.innerHTML = ""; return; }
  if (!state.repo) { ul.innerHTML = `<li class="muted static">Select a repository first.</li>`; return; }
  ul.innerHTML = `<li class="muted static">Searching…</li>`;
  try {
    const { results } = await api(repoPath(`/search?q=${encodeURIComponent(q)}&limit=8`));
    if (!isLatest("pathSearch", seq)) return;
    ul.innerHTML = results.length
      ? (state.current ? "" : `<li class="muted static">Select a source node first (search or click the graph), then pick a target.</li>`) +
        results.map((n) => resultItem(n)).join("")
      : `<li class="muted static">No matches for “${h(q)}”.</li>`;
  } catch (e) {
    if (isLatest("pathSearch", seq)) ul.innerHTML = `<li class="static">${statusHtml("error", e.message, "retry-path-search")}</li>`;
  }
}, 220);

async function runPath(dst) {
  // Claim the latest slot first: an earlier path request still in flight must not
  // overwrite the message shown for this attempt, even when we return early below.
  const seq = nextSeq("path"), src = state.current;
  state.busy.path = false;
  if (!state.current) { $("#path").textContent = "Select a source node first (search or click the graph)."; return; }
  if (!dst) return;
  const rels = selectedRels().filter((r) => r !== "DEPENDS_ON");
  if (!rels.length) { $("#path").textContent = "Tick at least one relationship type (other than packages) above the graph."; return; }
  if (dst === state.current) { $("#path").textContent = "The target is the selected node itself. Pick a different target."; return; }
  state.lastPathDst = dst; state.busy.path = true;
  $$("#path-results li[data-uid]").forEach((li) => li.classList.toggle("picked", li.dataset.uid === dst));
  $("#path").textContent = "Searching…";
  try {
    const r = await api(repoPath(`/path?src=${encodeURIComponent(src)}&dst=${encodeURIComponent(dst)}&rels=${rels.join(",")}&undirected=${$("#path-undirected").checked}`));
    if (!isLatest("path", seq)) return;
    if (!r.found) { $("#path").innerHTML = `No path from <b>${h(displayName(r.src))}</b> to <b>${h(displayName(r.dst))}</b> within ${r.max_depth} hops` + ($("#path-undirected").checked ? "." : `. Try “ignore edge direction”.`); return; }
    $("#path").innerHTML = `<div>${r.length} hop(s):</div><ul class="results">${r.nodes.map((n, i) => resultItem(n, i < r.hops.length ? ` <span class="muted small">—${h(r.hops[i].type)}→</span>${confBadge(r.hops[i].confidence)}` : "")).join("")}</ul>`;
    showAnalysisGraph("path", r.nodes, r.hops.map((x) => ({ src: x.from, dst: x.to, type: x.type, confidence: x.confidence })), r.nodes[0].uid,
      `Path ${displayName(r.nodes[0])} → ${displayName(r.nodes[r.nodes.length - 1])} · ${r.length} hop(s)`);
  } catch (e) {
    if (isLatest("path", seq)) $("#path").innerHTML = statusHtml("error", e.message, "retry-path");
  } finally {
    if (isLatest("path", seq)) state.busy.path = false;
  }
}

/* ---------------- ask ---------------- */
function renderMarkdown(text) {
  const esc = h(text);
  return esc.split(/\n{2,}/).map((para) => "<p>" + para
    .replace(/```([\s\S]*?)```/g, (_, c) => `<pre>${c}</pre>`)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[(E\d{1,3})\]/g, '<span class="cite" data-ev="$1" tabindex="0" role="button" title="Show evidence $1">$1</span>')
    .replace(/\n/g, "<br>") + "</p>").join("");
}
/* Render an /ask response. Every caveat (no match, cut off, uncited, weakly cited) is shown
   where it cannot be missed, never as a footnote under an answer that looks complete. */
function renderAnswer(r) {
  const cited = new Set(r.citations || []);
  const evCount = (r.evidence || []).length;
  const badges = {
    model: `<span class="badge ok">${h(r.model)} · ${(r.citations || []).length} citations</span>`,
    graph_only: `<span class="badge warn">graph-only</span> <span class="small error">${h(r.model_error || "")}</span>`,
    no_match: `<span class="badge warn">no matching symbol</span>`,
  };
  const top = [];
  if (r.mode === "model" && r.complete === false) {
    top.push(`<div class="notice strong" role="alert"><b>Incomplete answer.</b> The model reached its output limit, so this answer is cut off. ` +
      `<button type="button" class="ghost small-btn" data-action="retry-ask">Ask again</button></div>`);
  }
  if (r.mode === "model" && !r.grounded) {
    top.push(`<div class="notice strong">The model cited no graph evidence. Treat this answer as unverified.</div>`);
  }
  if (r.unmatched && r.unmatched.length && r.mode !== "no_match") {
    top.push(`<div class="notice">Not found in the graph: ${r.unmatched.map((u) => `<code>${h(u)}</code>`).join(", ")}</div>`);
  }
  const notes = [];
  if (r.note) notes.push(`<div class="notice">${h(r.note)}</div>`);
  if (r.invalid_citations && r.invalid_citations.length) notes.push(`<div class="notice">Removed citations to unknown evidence: ${h(r.invalid_citations.join(", "))}</div>`);
  if (r.unsupported_citations && r.unsupported_citations.length) {
    notes.push(`<div class="notice">Check these citations: ${h(r.unsupported_citations.join(", "))} do not mention the code named in the sentence that cites them.</div>`);
  }
  const suggestions = r.mode === "no_match" && r.suggestions && r.suggestions.length
    ? `<h3>Close matches in the graph</h3><ul class="results">${r.suggestions.map((s) => resultItem(s)).join("")}</ul>` : "";
  const evidence = evCount ? `<h3>Evidence (${evCount})</h3>
      <ul class="evidence">${r.evidence.map((e) => {
        const used = r.mode !== "model" || cited.has(e.id);
        return `<li id="ev-${h(e.id)}" class="${used ? "" : "uncited"}">
        <div class="ev-head"><span class="cite">${h(e.id)}</span><b>${h(e.title)}</b>
        ${e.uid ? `<a class="link small" data-uid="${h(e.uid)}" tabindex="0" role="button" title="Open in Definition">${h(e.location || "open")}</a>` : ""}<span class="muted small">${h(e.kind)}${used ? "" : " · not cited"}</span></div>
        <ul>${e.facts.slice(0, 12).map((f) => `<li>${h(f)}</li>`).join("")}</ul>
        ${e.snippet ? `<details><summary class="small">source</summary><pre>${h(e.snippet)}</pre></details>` : ""}</li>`;
      }).join("")}</ul>` : "";
  return `<div class="mode">${badges[r.mode] || `<span class="badge bad">${h(r.mode)}</span>`}</div>
      ${top.join("")}
      <div class="answer${r.complete === false ? " incomplete" : ""}">${renderMarkdown(r.answer)}</div>
      ${notes.join("")}${suggestions}${evidence}`;
}

async function ask(ev) {
  if (ev) ev.preventDefault();
  if (state.busy.ask) return;                    // Ctrl+Enter must not bypass the disabled button
  const q = $("#ask-input").value.trim();
  if (!state.repo) { $("#answer").innerHTML = `<div class="error">Select a repository first.</div>`; return; }
  if (!q) { $("#answer").innerHTML = `<div class="error">Type a question first.</div>`; $("#ask-input").focus(); return; }
  const seq = nextSeq("ask"), repo = state.repo;
  state.busy.ask = true; updateActions();
  $("#answer").innerHTML = `<div class="muted">Retrieving graph evidence and asking the model…</div>`;
  try {
    const r = await api(repoPath("/ask"), { method: "POST", body: JSON.stringify({ question: q }) });
    if (!isLatest("ask", seq) || state.repo !== repo) return;
    state.lastEvidence = r.evidence;
    $("#answer").innerHTML = renderAnswer(r);
  } catch (e) {
    if (isLatest("ask", seq)) $("#answer").innerHTML = statusHtml("error", e.message, "retry-ask");
  } finally {
    if (isLatest("ask", seq)) { state.busy.ask = false; updateActions(); }
  }
}

/* ---------------- tabs & wiring ---------------- */
function showTab(name, { focus = false } = {}) {
  $$(".tabs button").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
    if (on && focus) b.focus();
  });
  $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== name));
}
function tabKeys(e) {                            // WAI-ARIA tabs: arrows, Home, End
  const tabs = $$(".tabs button");
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  showTab(tabs[(next + tabs.length) % tabs.length].dataset.tab, { focus: true });
}

const ACTIONS = {
  "recheck-health": () => loadHealth(),
  "retry-repos": () => loadRepos(state.repo, { force: true }),
  "retry-overview": () => loadOverview(),
  "retry-search": () => doSearch(),
  "retry-node": () => openNode(state.current),
  "retry-impact": () => runImpact(),
  "retry-path": () => runPath(state.lastPathDst),
  "retry-path-search": () => pathSearch(),
  "retry-impact-search": () => impactSearch(),
  "retry-scenario": () => runScenario(state.lastScenarioTargets || undefined),
  "retry-ask": () => ask(),
};

document.addEventListener("DOMContentLoaded", () => {
  $("#index-form").addEventListener("submit", indexRepo);
  $("#repo-select").addEventListener("change", (e) => selectRepo(e.target.value));
  $("#repo-delete").addEventListener("click", openDeleteDialog);
  $("#confirm-form").addEventListener("submit", confirmDelete);
  $("#confirm-cancel").addEventListener("click", () => $("#confirm-dialog").close());
  $("#progress-close").addEventListener("click", () => ($("#progress").hidden = true));
  $("#search-input").addEventListener("input", doSearch);
  $("#search-kind").addEventListener("change", doSearch);
  $("#ask-form").addEventListener("submit", ask);
  $("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(e); });
  $("#impact-btn").addEventListener("click", () => runScenario());
  $("#impact-symbol-btn").addEventListener("click", runImpact);
  $("#impact-scenario").addEventListener("input", updateActions);
  $("#impact-scenario").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runScenario(); } });
  $("#impact").addEventListener("change", (e) => { if (e.target.matches("input[data-target]")) updateReanalyze(); });
  $("#impact-depth").addEventListener("change", impactDepth);
  $("#impact-search").addEventListener("input", impactSearch);
  $("#impact-search").addEventListener("keydown", (e) => {        // Enter picks the first result
    const first = $("#impact-results li[data-uid]");
    if (e.key === "Enter" && first) { e.preventDefault(); pickForImpact(first.dataset.uid); }
  });
  $("#impact-depth").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); runImpact(); } });
  $("#path-target").addEventListener("input", pathSearch);
  $("#path-undirected").addEventListener("change", () => state.lastPathDst && runPath(state.lastPathDst));
  $("#fit-btn").addEventListener("click", () => fitGraph(true));
  $("#graph-home").addEventListener("click", () => showOverviewGraph());
  $("#direction").addEventListener("change", () => graph.mode === "neighbors" && loadGraph());
  $("#rel-filters").addEventListener("change", () => loadGraph());
  if (window.ResizeObserver) {                 // watch the canvas itself too: it can gain size without #graph changing
    const ro = new ResizeObserver(debounce(onGraphResize, 120));
    ro.observe($("#graph")); ro.observe($("#graph-canvas"));
  }
  updateGraphControls();
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $(".tabs").addEventListener("keydown", tabKeys);
  window.addEventListener("hashchange", () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id && id !== state.repo && state.repos.some((r) => r.repo_id === id)) selectRepo(id);
  });

  document.body.addEventListener("click", (e) => {
    const action = e.target.closest("[data-action]");
    if (action && ACTIONS[action.dataset.action]) { ACTIONS[action.dataset.action](); return; }
    const cite = e.target.closest(".cite[data-ev]");
    if (cite) {
      const li = document.getElementById("ev-" + cite.dataset.ev);
      if (li) { li.scrollIntoView({ behavior: "smooth", block: "center" }); li.classList.add("flash"); setTimeout(() => li.classList.remove("flash"), 1200); }
      return;
    }
    const ex = e.target.closest("[data-example]");
    if (ex) { $("#impact-scenario").value = ex.dataset.example; updateActions(); $("#impact-scenario").focus(); return; }
    if (e.target.closest("#impact-reanalyze")) { const t = checkedTargets(); if (t.length) runScenario(t); return; }
    if (e.target.closest("#impact-change")) {
      graph.impactPickerOpen = true; renderImpactSelection(); $("#impact-search").focus(); return;
    }
    const impactPick = e.target.closest("#impact-results li[data-uid]");
    if (impactPick) { pickForImpact(impactPick.dataset.uid); return; }
    const pathPick = e.target.closest("#path-results li[data-uid]");
    if (pathPick) { runPath(pathPick.dataset.uid); return; }
    const item = e.target.closest("[data-uid]");
    if (item && item.dataset.uid) {
      if (item.closest("summary")) e.preventDefault();   // open the symbol, don't toggle its <details> group
      // Results inside Impact/Path belong to that analysis: open the clicked symbol in Definition.
      const fromAnalysis = item.closest("[data-pane=impact], [data-pane=path]");
      openNode(item.dataset.uid, fromAnalysis ? { tab: "detail" } : {});
    }
  });
  // Keyboard: Enter / Space activate focusable rows, links and citations.
  document.body.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const el = e.target.closest("[data-uid][tabindex], .cite[data-ev][tabindex]");
    if (!el || el !== e.target) return;
    e.preventDefault();
    el.click();
  });

  updateActions();
  loadHealth();
  loadRepos();
});

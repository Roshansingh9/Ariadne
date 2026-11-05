"""Natural-language change scenarios → indexed graph targets → impact report.

"If I change the retry behavior, what should I inspect?" is answered in steps that each
stay grounded in the indexed graph:

1. **Terms.** Identifiers, quoted/backticked names, paths and content words are pulled
   from the scenario (stop-words and change-words dropped).
2. **Symbol matches.** Each term is searched in the graph (same search as the UI). The
   match kind (exact / prefix / substring) sets the confidence. Only an *exact, unique*
   match of an identifier-like term (snake_case, CamelCase, dotted, path or quoted) is
   **high** confidence.
3. **Concept matches.** Words such as "API", "route", "model", "UI", "job" or "database"
   select real nodes of that layer (route decorators, JSX components, model base classes,
   external calls). Never auto-selected; they boost a term match that lands on them.
4. **Selection.** High-confidence targets are analysed immediately. Anything weaker is
   returned for the user to confirm. Nothing ambiguous or weak is selected silently.
5. **Optional model ranking.** With no high-confidence target and the Meta API configured,
   the model may *rank the numbered candidates*: it can only refer to `C#` ids it was
   shown; every id is validated, invented ones are rejected and reported, and its picks
   are still only suggestions the user confirms.
6. **Impact.** `GraphQueries.impact_many` traverses from the confirmed targets and the
   deterministic report (paths, layers, UI flows, tests, explanation) is built from graph
   facts. The model never writes the report.

Fields, attributes, return types, nullability and message formats are **not** indexed:
a scenario about a data shape is traced from the code that defines or handles it, and the
response says so in `notes`.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from . import frameworks as F
from .meta_client import MetaAPIError, MetaClient
from .qa import STOPWORDS, looks_like_identifier
from .queries import GraphQueries, NodeNotFound, brief

MAX_SCENARIO_CHARS = 1000
MAX_CANDIDATES = 24
MAX_TARGETS = 10
MAX_MODEL_PICKS = 5

_CHANGE_WORDS = set("""
change changes changed changing modify modifies modified modifying rename renames renamed renaming
update updates updated updating remove removes removed removing delete deleted add adds added adding
refactor refactored replace replaced affect affected affects impact impacted happen happens break
breaks broken could would should might what which who where when how if this that these those
inspect check depend depends dependent dependents become becomes became returned returns
behavior behaviour logic code thing things way also both across whole entire new old different
""".split())
_TERM = re.compile(r"`([^`]{2,100})`|\"([^\"]{2,100})\"|'([^'\s]{2,100})'|([A-Za-z_][A-Za-z0-9_./:-]{1,})")

# Concept words → the layer whose indexed nodes they refer to (see frameworks.classify).
CONCEPTS = {
    "HTTP route": {"api", "apis", "endpoint", "endpoints", "route", "routes", "request", "requests", "response",
                   "responses", "webhook", "webhooks", "http", "handler", "handlers", "rest"},
    "UI component": {"ui", "page", "pages", "component", "components", "button", "form", "screen", "frontend", "view"},
    "Data model / schema": {"model", "models", "schema", "schemas", "field", "fields", "entity", "dto", "table", "column"},
    "Background job / event handler": {"job", "jobs", "worker", "workers", "queue", "task", "tasks", "background",
                                       "cron", "scheduler", "consumer"},
    "External service / database": {"database", "db", "sql", "storage", "redis", "cache", "integration",
                                    "third-party", "external"},
    "API client": {"client", "fetch", "frontend-api"},
}
# Words that often name code differently from how people say them.
# General vocabulary only (never tuned to a particular repository); plurals are handled separately.
SYNONYMS = {"authentication": ["auth"], "authenticate": ["auth"], "authorization": ["auth", "permission"],
            "login": ["auth"], "retries": ["retry"], "configuration": ["config"], "settings": ["config"]}
_CONCEPT_WORDS = {w for vocab in CONCEPTS.values() for w in vocab}
# Words that describe what the user wants *reported*, not what changes.
_REPORT_WORDS = {"test", "tests", "testing", "function", "functions", "method", "methods", "file", "files",
                 "symbol", "symbols", "caller", "callers", "dependency", "dependencies", "flow", "flows"}

_NOTES = [
    (re.compile(r"\b(field|fields|attribute|attributes|column|property|properties|nullable|null|none|optional|"
                r"type|types|format|schema|payload|shape|return(?:ed|s)?|response)\b", re.I),
     "Data shapes (fields, attributes, return types, nullability, message formats) are not indexed. Impact is traced "
     "from the code that defines or handles the matched targets; whether a specific field is read is not verified."),
    (re.compile(r"\b(rename|renamed|renaming)\b", re.I),
     "A rename can also break string references, configuration, serialized data and dynamic access, which the graph "
     "does not capture."),
    (re.compile(r"\b(behaviou?r|logic|retry|retries|timeout|order(?:ing)?|side effects?|semantics)\b", re.I),
     "Behavioural changes are not visible statically: dependents are places that may observe the change, not "
     "places proven to depend on the old behaviour."),
]


def scenario_terms(scenario: str) -> list[dict]:
    """[{term, quoted, identifier}] in order of appearance, de-duplicated (case-insensitive)."""
    out, seen = [], set()
    for m in _TERM.finditer(scenario):
        quoted = m.group(1) or m.group(2) or m.group(3)
        t = (quoted or m.group(4) or "").strip().strip(".,:;()?!")
        if t.endswith("()"):
            t = t[:-2]
        key = t.lower()
        if not t or len(t) < 3 or key in seen:
            continue
        if not quoted and (key in STOPWORDS or key in _CHANGE_WORDS or key in _REPORT_WORDS):
            continue
        if not quoted and key in _CONCEPT_WORDS and not any(ch in t for ch in "_./"):
            continue                              # handled as a concept (layer), not searched as a name
        seen.add(key)
        out.append({"term": t, "quoted": bool(quoted), "identifier": bool(quoted) or looks_like_identifier(t)})
    return out[:12]


def detect_concepts(scenario: str) -> list[str]:
    words = set(re.findall(r"[a-z][a-z-]+", scenario.lower()))
    return [layer for layer, vocab in CONCEPTS.items() if words & vocab]


def change_notes(scenario: str) -> list[str]:
    return [note for pat, note in _NOTES if pat.search(scenario)]


def _brief(n: dict) -> dict:
    b = brief(n)
    if n.get("label") and not n.get("labels"):    # already a brief (e.g. a search hit): keep its real label
        b["label"] = n["label"]
    return {k: b.get(k) for k in ("uid", "name", "qualname", "label", "kind", "path", "line_start", "line_end")
            if b.get(k) is not None}


def resolve(gq: GraphQueries, scenario: str) -> dict:
    """Candidates for the scenario, with why each matched and how strongly. Read-only."""
    terms = scenario_terms(scenario)
    concepts = detect_concepts(scenario)
    cands: dict[str, dict] = {}

    def add(hit: dict, match: str, term: Optional[str], confidence: str, reason: str, ambiguous: bool = False):
        rank = {"high": 3, "medium": 2, "low": 1}
        c = cands.get(hit["uid"])
        if c is None:
            c = cands[hit["uid"]] = {**_brief(hit), "match": match, "term": term, "confidence": confidence,
                                     "reasons": [], "ambiguous": ambiguous}
        elif rank[confidence] > rank[c["confidence"]]:
            c.update(match=match, term=term, confidence=confidence, ambiguous=ambiguous)
        if reason not in c["reasons"]:
            c["reasons"].append(reason)

    unmatched = []
    for t in terms:
        variants = [t["term"]] + [v for v in SYNONYMS.get(t["term"].lower(), []) if v != t["term"].lower()]
        if not t["identifier"] and len(t["term"]) > 4 and t["term"].lower().endswith("s"):
            variants.append(t["term"][:-1])
        found = False
        for v in variants:
            hits = gq.search(v, "all", limit=8)
            exact = [h for h in hits if h["rank"] == 0 or (h.get("path") or "").lower() == v.lower()]
            prefix = [h for h in hits if h["rank"] == 1 and h not in exact]
            sub = [h for h in hits if h["rank"] >= 2 and h not in exact]
            label = f"“{t['term']}”" + (f" (as “{v}”)" if v != t["term"] else "")
            if exact:
                strong = t["identifier"] and v == t["term"] and len(exact) == 1
                for h in exact[:6]:
                    add(h, "exact", t["term"], "high" if strong else "medium",
                        f"exact name match for {label}" + (f"; {len(exact)} symbols share this name" if len(exact) > 1 else ""),
                        ambiguous=len(exact) > 1)
                found = True
            for h in prefix[:4]:
                add(h, "prefix", t["term"], "medium" if t["identifier"] else "low", f"name starts with {label}")
                found = True
            for h in sub[:3]:
                add(h, "substring", t["term"], "low", f"name contains {label}")
                found = True
            if found and exact:
                break
        if not found:
            unmatched.append(t["term"])

    # Concept words: real nodes of the mentioned layer (only syntax-confirmed members are offered).
    concept_hits = {}
    for layer in concepts:
        for n in gq.layer_nodes(layer)[:8]:
            concept_hits.setdefault(n["uid"], (n, layer))
    for uid, (n, layer) in concept_hits.items():
        reason = f"is a {layer.lower()} (confirmed by syntax), mentioned as a concept"
        if uid in cands:
            c = cands[uid]
            c["reasons"].append(reason)
            if c["confidence"] == "low":
                c["confidence"] = "medium"
        else:
            add(n, "concept", None, "low", reason)

    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(cands.values(), key=lambda c: (order[c["confidence"]], c["match"] != "exact", c.get("path") or "", c["uid"]))
    return {"terms": [t["term"] for t in terms], "concepts": concepts, "unmatched": unmatched,
            "candidates": ranked[:MAX_CANDIDATES], "notes": change_notes(scenario)}


# --------------------------------------------------------------------------
# Optional: the Meta model ranks the *given* candidates (never invents targets)
# --------------------------------------------------------------------------

RANK_SYSTEM = """You map a proposed code change to the code it is about, in ONE repository.
You are given numbered CANDIDATES that really exist in the repository's code graph. Choose the candidates that the
change is most likely about. Rules:
- Use only the candidate ids shown (C1, C2, ...). Never invent names, files or ids.
- Choose at most 5. If none fit, choose none.
- Output JSON only, exactly: {"targets": [{"ref": "C1", "why": "<= 20 words"}], "none_fit": false}"""


def rank_with_model(meta: MetaClient, scenario: str, candidates: list[dict], repo_label: str) -> dict:
    refs = {f"C{i + 1}": c for i, c in enumerate(candidates)}
    lines = []
    for ref, c in refs.items():
        where = f"{c.get('path')}:{c['line_start']}" if c.get("line_start") else (c.get("path") or "")
        lines.append(f"[{ref}] {c.get('label')} `{c.get('qualname') or c.get('name')}` ({where}) — matched: {'; '.join(c['reasons'])}")
    messages = [{"role": "system", "content": RANK_SYSTEM},
                {"role": "user", "content": f"Repository: {repo_label}\n\nPROPOSED CHANGE: {scenario}\n\nCANDIDATES:\n"
                                            + "\n".join(lines) + "\n\nJSON only."}]
    out = {"used": True, "picked": [], "rejected": [], "reasons": {}, "error": None, "model": None}
    try:
        res = meta.chat(messages, temperature=0.0, max_completion_tokens=900)
    except MetaAPIError as exc:
        out["error"] = str(exc)
        return out
    out["model"] = res.model
    return {**out, **validate_model_ranking(res.content, refs)}


def validate_model_ranking(content: str, refs: dict) -> dict:
    """Keep only picks that name a candidate the model was shown. Everything else is rejected."""
    picked, rejected, reasons, error = [], [], {}, None
    m = re.search(r"\{.*\}", content or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("targets", []), list):
        return {"picked": [], "rejected": [], "reasons": {}, "error": "The model did not return the required JSON; ignored."}
    for t in data.get("targets", [])[:MAX_MODEL_PICKS + 3]:
        ref = str((t or {}).get("ref", "")).strip() if isinstance(t, dict) else str(t)
        if ref in refs and refs[ref]["uid"] not in picked and len(picked) < MAX_MODEL_PICKS:
            picked.append(refs[ref]["uid"])
            reasons[refs[ref]["uid"]] = str(t.get("why", ""))[:200] if isinstance(t, dict) else ""
        else:
            rejected.append(ref[:60] or "(empty)")
    return {"picked": picked, "rejected": rejected, "reasons": reasons, "error": error}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def analyze(gq: GraphQueries, meta: Optional[MetaClient], scenario: str, targets: Optional[list[str]] = None,
            depth: int = 3, rels=None, use_model: bool = True) -> dict:
    scenario = (scenario or "").strip()
    if not scenario:
        raise ValueError("Describe the change you're considering.")
    if len(scenario) > MAX_SCENARIO_CHARS:
        raise ValueError(f"Scenario is too long (max {MAX_SCENARIO_CHARS} characters).")
    repo = gq.ensure_exists()
    repo_label = repo.get("url") or repo.get("name") or gq.repo_id
    res = resolve(gq, scenario)
    cands = res["candidates"]
    model_info = {"used": False}

    if targets is not None:                       # the user confirmed / corrected the targets
        targets = list(dict.fromkeys(t for t in targets if t))
        if not targets:
            raise ValueError("Select at least one target to analyze.")
        if len(targets) > MAX_TARGETS:
            raise ValueError(f"At most {MAX_TARGETS} targets at once.")
        found = gq.nodes(targets)
        unknown = [t for t in targets if t not in found]
        if unknown:
            raise NodeNotFound(f"Unknown target {unknown[0]!r}: not a symbol in this repository's graph.")
        known = {c["uid"] for c in cands}
        for t in targets:                         # chosen outside the candidate list (e.g. symbol picker)
            if t not in known:
                cands.append({**_brief(found[t]), "match": "user", "term": None, "confidence": "high",
                              "reasons": ["selected by you"], "ambiguous": False})
        selected, status = targets, "analyzed"
        message = f"Analyzing {len(selected)} confirmed target(s)."
    else:
        selected = [c["uid"] for c in cands if c["confidence"] == "high"][:MAX_TARGETS]
        if selected:
            status = "analyzed"
            message = (f"Matched {len(selected)} target(s) by exact name. Review the matches below; deselect any that "
                       "are wrong or add alternatives and re-analyze.")
        elif cands:
            status = "needs_confirmation"
            message = ("No exact, unambiguous match. Choose the target(s) your change is about, then press "
                       "“Analyze selected targets”.")
            if use_model and meta is not None and meta.configured:
                model_info = rank_with_model(meta, scenario, cands, repo_label)
                for c in cands:
                    if c["uid"] in model_info.get("picked", []):
                        c["model_suggested"] = True
                        c["model_reason"] = model_info["reasons"].get(c["uid"], "")
                        c["reasons"].append("ranked as relevant by the Meta model (from the candidates shown to it)")
                if model_info.get("picked"):
                    message = ("No exact match. The suggested targets below were ranked by the Meta model from real "
                               "indexed candidates; confirm them (or pick others), then press “Analyze selected targets”.")
        else:
            status = "no_match"
            message = ("No matching indexed symbol found. Name a function, class, file or route as it appears in the "
                       "code (for example `send_message` or `api/index.py`), or use the symbol picker.")

    for c in cands:
        c["selected"] = c["uid"] in selected
        c["suggested"] = c["selected"] or bool(c.get("model_suggested")) or (status == "needs_confirmation" and c["confidence"] == "medium")
    order = {"high": 0, "medium": 1, "low": 2}
    cands.sort(key=lambda c: (not c["selected"], not c["suggested"], order.get(c["confidence"], 3)))   # suggestions first
    report = gq.impact_many(selected, depth, rels) if status == "analyzed" else None
    return {
        "scenario": scenario,
        "repo": {"id": gq.repo_id, "label": repo_label},
        "resolution": {**res, "candidates": cands, "selected": selected, "status": status, "message": message,
                       "model": {k: v for k, v in model_info.items() if k != "reasons"}},
        "report": report,
    }

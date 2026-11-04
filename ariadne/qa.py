"""Graph-grounded question answering.

1. Deterministic retrieval: question → identifier terms → graph search →
   definitions, callers, callees, impact (and the overview only for broad
   questions), as numbered evidence.
2. If the question names something that is not in the graph, Ariadne says so
   and suggests close matches. It does not hand unrelated facts to the model.
3. The Meta Model API answers **only** from that evidence and cites `[E#]`.
4. Citations are validated: unknown ids are removed, and a citation whose
   evidence does not mention the code names in its sentence is flagged.

No model-generated Cypher is ever executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .meta_client import MetaAPIError, MetaClient
from .queries import GraphQueries

MAX_ENTITIES = 6
MAX_EVIDENCE_CHARS = 24000
SNIPPET_LINES = 25
MAX_QUESTION_CHARS = 1000

_TERM_RE = re.compile(r"`([^`]{2,100})`|\"([^\"]{2,100})\"|([A-Za-z_][A-Za-z0-9_./-]{2,})")
_CITE_RE = re.compile(r"\[E(\d{1,3})\]")
CALLER_WORDS = re.compile(r"\b(?:what|who|which\s+\w+)\s+(?:calls?|invokes?|uses?)\b|\bcallers?\s+of\b|"
                          r"\bcalled\s+(?:by|from)\b|\bwhere\s+is\b.*\b(?:called|used|invoked)\b|\bwho\s+uses\b", re.I)
CALLEE_WORDS = re.compile(r"\bwhat\s+does\b.*\bcall\b|\bcallees?\s+of\b|\bcalls\s+made\s+by\b", re.I)
IMPACT_WORDS = re.compile(r"\b(impact|affect|affects|break|breaks|change|changing|modify|depends? on|"
                          r"blast radius|ripple|downstream|upstream|who uses|used by)\b", re.I)
BROAD_WORDS = re.compile(r"\b(overview|architecture|structure|what does (this|the) (repo|project|codebase)|"
                         r"summar|main (components|modules)|entry ?points?|organized|layout)\b", re.I)
STOPWORDS = set("""
a an the and or but if then else for while of to in on at by with from into onto about over under
is are was were be been being do does did done doing have has had having can could should would will
shall may might must not no yes this that these those it its there here what which who whom whose
where when why how all any each every some many much more most other such only own same so than too
very just also me my we our you your they their them he she his her i
function functions method methods class classes file files code repo repository project module modules
call calls called calling caller callers callee callees use uses used using defined define defines
definition implement implemented implementation work works happen happens explain show tell find list
please give does happen value return returns variable variables thing things part parts line lines
""".split())


@dataclass
class Evidence:
    id: str
    kind: str
    title: str
    facts: list[str] = field(default_factory=list)
    uid: Optional[str] = None
    path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    snippet: Optional[str] = None

    def location(self) -> str:
        if not self.path:
            return ""
        if self.line_start:
            end = f"-{self.line_end}" if self.line_end and self.line_end != self.line_start else ""
            return f"{self.path}:{self.line_start}{end}"
        return self.path

    def render(self) -> str:
        head = f"[{self.id}] {self.kind.upper()}: {self.title}"
        loc = self.location()
        if loc:
            head += f"  ({loc})"
        body = "\n".join(f"  - {f}" for f in self.facts)
        out = head + ("\n" + body if body else "")
        if self.snippet:
            out += f"\n  source:\n```\n{self.snippet}\n```"
        return out

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "facts": self.facts,
                "uid": self.uid, "path": self.path, "line_start": self.line_start,
                "line_end": self.line_end, "location": self.location(), "snippet": self.snippet}


def extract_terms(question: str) -> list[str]:
    terms: list[str] = []
    for m in _TERM_RE.finditer(question):
        t = (m.group(1) or m.group(2) or m.group(3) or "").strip().strip(".,:;()?!")
        if not t or t.lower() in STOPWORDS or len(t) < 3:
            continue
        if t.endswith("()"):
            t = t[:-2]
        if t not in terms:
            terms.append(t)
    # Identifier-looking terms (snake_case, camelCase, dotted, paths) first.
    def score(t: str) -> int:
        s = 0
        if any(c in t for c in "_./"):
            s -= 2
        if re.search(r"[a-z][A-Z]", t) or t[:1].isupper():
            s -= 1
        return s
    return sorted(terms, key=score)[:10]


def looks_like_identifier(term: str) -> bool:
    """snake_case, dotted, path-like, camelCase or Capitalized: almost certainly a code name."""
    return any(c in term for c in "_./") or bool(re.search(r"[a-z][A-Z]", term)) or term[:1].isupper()


def detect_intent(question: str) -> str:
    if IMPACT_WORDS.search(question):
        return "impact"
    if CALLER_WORDS.search(question):
        return "callers"
    if CALLEE_WORDS.search(question):
        return "callees"
    if BROAD_WORDS.search(question):
        return "overview"
    return "definition"


def _loc(n: dict) -> str:
    if n.get("line_start"):
        return f"{n.get('path')}:{n['line_start']}"
    return n.get("path") or n.get("name") or n.get("uid", "")


def _snippet(source: Optional[str]) -> Optional[str]:
    if not source:
        return None
    lines = source.splitlines()
    return "\n".join(lines[:SNIPPET_LINES]) + ("\n…" if len(lines) > SNIPPET_LINES else "")


def _edge_note(edge: dict) -> str:
    conf = edge.get("confidence")
    lines = edge.get("lines") or ([edge["line"]] if edge.get("line") else [])
    parts = []
    if lines:
        parts.append("line " + ", ".join(str(x) for x in lines[:5]))
    if conf and conf != "resolved":
        parts.append(f"{conf} via {edge.get('resolution')}")
    return f" ({'; '.join(parts)})" if parts else ""


@dataclass
class Retrieval:
    evidence: list[Evidence]
    matched: list[str]                  # qualnames/paths of the symbols used as evidence
    terms: list[str]                    # terms extracted from the question
    unmatched: list[str]                # terms with no exact or prefix match in the graph
    suggestions: list[dict]             # close (substring) matches for unmatched terms
    intent: str
    broad: bool


def retrieve_evidence(gq: GraphQueries, question: str) -> tuple[list[Evidence], list[str]]:
    """Return (evidence, matched_terms). Deterministic for a given graph."""
    r = retrieve(gq, question)
    return r.evidence, r.matched


def retrieve(gq: GraphQueries, question: str) -> Retrieval:
    evidence: list[Evidence] = []

    def add(kind: str, title: str, **kw) -> Evidence:
        ev = Evidence(id=f"E{len(evidence) + 1}", kind=kind, title=title, **kw)
        evidence.append(ev)
        return ev

    terms = extract_terms(question)
    intent = detect_intent(question)
    broad = bool(BROAD_WORDS.search(question))
    picked: list[dict] = []
    seen: set[str] = set()
    unmatched: list[str] = []
    suggestions: list[dict] = []
    for t in terms:
        if len(picked) >= MAX_ENTITIES:
            break
        hits = gq.search(t, "all", limit=5)
        found = False
        for hit in hits:
            exactish = hit["rank"] <= 1 or "/" in t or "." in t
            if not exactish:
                continue
            found = True                      # the term names something real, even if already picked
            if hit["uid"] not in seen:
                seen.add(hit["uid"])
                picked.append(hit)
            break
        if not found:
            unmatched.append(t)
            for hit in hits[:3]:              # substring matches: offered, never used as evidence
                if hit["uid"] not in seen and len(suggestions) < 6 and \
                        all(x["uid"] != hit["uid"] for x in suggestions):
                    suggestions.append({**{k: hit.get(k) for k in ("uid", "name", "qualname", "label", "path",
                                                                    "line_start", "line_end", "kind")},
                                        "term": t})

    for hit in picked:
        detail = gq.symbol(hit["uid"], limit=12)
        node = detail["node"]
        label = node.get("label")
        facts = [f"{label} `{node.get('qualname') or node.get('name') or node.get('path')}`"
                 + (f" ({node.get('kind')})" if node.get("kind") else "")]
        if node.get("signature"):
            facts.append(f"signature: {node['signature']}")
        if node.get("docstring"):
            facts.append(f"docstring: {node['docstring'][:300]}")
        for p in detail["defined_in"]:
            facts.append(f"defined in {p['label']} `{p.get('qualname') or p.get('path')}`")
        if detail.get("bases"):
            facts.append("extends/implements: " + ", ".join(
                f"`{b.get('qualname') or b.get('name')}` [{_loc(b)}]{_edge_note(b['edge'])}" for b in detail["bases"]))
        if detail.get("subclasses"):
            facts.append("subclassed by: " + ", ".join(
                f"`{b.get('qualname')}` [{_loc(b)}]" for b in detail["subclasses"]))
        if detail.get("children") and label in ("Class", "File"):
            facts.append("defines: " + ", ".join(f"`{c.get('name')}` (line {c.get('line_start')})"
                                                  for c in detail["children"][:15]))
        if node.get("external_calls"):
            facts.append("calls external: " + ", ".join(node["external_calls"][:10]))
        if node.get("unresolved_calls"):
            facts.append("calls that could not be resolved in the graph: " + ", ".join(node["unresolved_calls"][:10]))
        if label == "File":
            for key, verb in (("imports", "imports"), ("imported_by", "imported by"), ("packages", "depends on packages")):
                if detail.get(key):
                    facts.append(f"{verb}: " + ", ".join(f"`{x.get('path') or x.get('name')}`" for x in detail[key][:12]))
        # State absences explicitly so the model can say "none" instead of filling the gap.
        if not detail["callers"]:
            facts.append("callers in the graph: none found")
        if label == "Function" and not detail["callees"]:
            facts.append("calls to other indexed functions: none found")
        add("definition", node.get("qualname") or node.get("path") or node["uid"], facts=facts,
            uid=node["uid"], path=node.get("path"), line_start=node.get("line_start"),
            line_end=node.get("line_end"), snippet=_snippet(node.get("source")))

        if detail["callers"]:
            add("callers", f"callers of {node.get('qualname') or node.get('name')}", uid=node["uid"],
                facts=[f"`{c.get('qualname') or c.get('path')}` calls it at "
                       f"[{c.get('path')}:{c['edge'].get('line', '?')}]{_edge_note(c['edge'])}"
                       for c in detail["callers"]])
        if detail["callees"]:
            add("callees", f"functions called by {node.get('qualname') or node.get('name')}", uid=node["uid"],
                facts=[f"calls `{c.get('qualname') or c.get('name')}` [{_loc(c)}]{_edge_note(c['edge'])}"
                       for c in detail["callees"]])

    if picked and intent == "impact":
        imp = gq.impact(picked[0]["uid"], depth=3)
        facts = [f"depth {n['depth']}: `{n.get('qualname') or n.get('path')}` [{_loc(n)}] via {n['via']}"
                 + (f" ({n['confidence']})" if n["confidence"] != "resolved" else "")
                 for n in imp["impacted"][:25]]
        if imp["affected_files"]:
            facts.append("affected files: " + ", ".join(f"{f['path']} ({f['symbols']})" for f in imp["affected_files"][:12]))
        if not imp["impacted"]:
            facts.append("no dependents found in the graph (within depth 3)")
        add("impact", f"impact of changing {picked[0].get('qualname') or picked[0].get('path')}",
            uid=picked[0]["uid"], facts=facts)

    # Repository-level facts only when the question is about the repository as a whole.
    # A question about a symbol that is not in the graph must not be answered from these.
    if broad:
        ov = gq.overview()
        r = ov["repo"]
        facts = [f"repository {r.get('url') or r.get('name')} at commit {str(r.get('commit') or '?')[:12]}",
                 "node counts: " + ", ".join(f"{k}={v}" for k, v in sorted(ov["counts"].items())),
                 "languages (files): " + ", ".join(f"{k}={v}" for k, v in ov["languages"].items()),
                 "top directories: " + ", ".join(f"{d['dir']} ({d['files']})" for d in ov["directories"][:10])]
        if ov["hotspots"]:
            facts.append("most-called functions: " + ", ".join(
                f"`{h.get('qualname')}` [{_loc(h)}] ({h['callers']} callers)" for h in ov["hotspots"]))
        if ov["most_imported"]:
            facts.append("most-imported files: " + ", ".join(f"{m['path']} ({m['importers']})" for m in ov["most_imported"]))
        if ov["packages"]:
            facts.append("external packages: " + ", ".join(f"{p['name']} ({p['files']} files)" for p in ov["packages"]))
        add("overview", "repository overview", facts=facts)

    return Retrieval(evidence=evidence, matched=[h.get("qualname") or h.get("path") for h in picked],
                     terms=terms, unmatched=unmatched, suggestions=suggestions, intent=intent, broad=broad)


SYSTEM_PROMPT = """You are Ariadne, a code-intelligence assistant. You answer questions about ONE software repository using ONLY the numbered evidence provided, which was retrieved from the repository's code graph (nodes, edges and source snippets).

Rules:
- Use only the evidence. Never rely on outside knowledge or guess names, files, lines or behaviour.
- Answer the question directly in the first sentence. If the evidence says no callers, callees or dependents were found, say exactly that.
- If the question names something listed under NOT FOUND, say plainly that it is not in the indexed graph. Do not substitute other symbols and do not describe unrelated code.
- Cite evidence inline as [E#] right after each claim, citing only evidence that states that claim. Give repository-relative locations as path:line.
- Format: **Answer** (1-3 sentences), then **Graph facts** (at most 6 bullets, each cited). Add **Interpretation** (1-2 clearly hedged sentences) only if it adds something.
- Edges marked "inferred" or "ambiguous" are heuristic; say so when you rely on them. "Unresolved" calls are not linked in the graph.
- If the evidence does not answer the question, say "The graph evidence is insufficient to answer this." and state what is missing. Never invent an answer.
- Keep the whole answer under 150 words."""

FOCUS = {
    "callers": "The question asks which code calls {target}. Answer with its callers first.",
    "callees": "The question asks what {target} calls. Answer with its callees first.",
    "impact": "The question asks what depends on {target} and would be affected by changing it.",
    "definition": "The question is about {target}.",
    "overview": "The question is about the repository as a whole.",
}


def build_messages(question: str, evidence: list[Evidence], repo_label: str,
                   intent: str = "definition", matched: Optional[list[str]] = None,
                   unmatched: Optional[list[str]] = None) -> list[dict]:
    blocks, total = [], 0
    for ev in evidence:
        text = ev.render()
        if total + len(text) > MAX_EVIDENCE_CHARS:
            text = text[: max(0, MAX_EVIDENCE_CHARS - total)] + "\n…(evidence truncated)"
            blocks.append(text)
            break
        blocks.append(text)
        total += len(text)
    target = ", ".join(f"`{m}`" for m in (matched or [])[:3]) or "the symbols in the evidence"
    focus = FOCUS.get(intent, FOCUS["definition"]).format(target=target)
    missing = ("\nNOT FOUND in the indexed graph: " + ", ".join(f"`{u}`" for u in unmatched)) if unmatched else ""
    user = (f"Repository: {repo_label}\n\nEVIDENCE:\n" + "\n\n".join(blocks)
            + f"\n\nFOCUS: {focus}{missing}"
            + f"\n\nQUESTION: {question}\n\nAnswer using only the evidence above, with [E#] citations.")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def validate_citations(answer: str, evidence: list[Evidence]) -> tuple[str, list[str], list[str]]:
    valid_ids = {e.id for e in evidence}
    cited, invalid = [], []
    for m in _CITE_RE.finditer(answer):
        eid = f"E{m.group(1)}"
        (cited if eid in valid_ids else invalid).append(eid)
    cleaned = answer
    for bad in set(invalid):
        cleaned = cleaned.replace(f"[{bad}]", "[citation removed]")
    return cleaned, sorted(set(cited), key=lambda x: int(x[1:])), sorted(set(invalid))


_IDENT_RE = re.compile(r"`([^`\n]{2,120})`")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def unsupported_citations(answer: str, evidence: list[Evidence]) -> list[str]:
    """Citations whose evidence mentions none of the `code` names in the citing sentence.

    Deliberately conservative: sentences without backticked code names are not judged.
    """
    texts = {e.id: e.render().lower() for e in evidence}
    flagged: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(answer):
        ids = [f"E{m}" for m in _CITE_RE.findall(sentence)]
        names = [n.strip().rstrip("()") for n in _IDENT_RE.findall(sentence)]
        tails = [t for t in (re.split(r"::|[./]", n)[-1].lower() for n in names if n) if len(t) >= 3]
        if not ids or not tails:
            continue
        for eid in ids:
            if eid in texts and not any(t in texts[eid] for t in tails):
                flagged.add(eid)
    return sorted(flagged, key=lambda x: int(x[1:]))


def no_match_answer(r: "Retrieval", repo_label: str) -> str:
    names = ", ".join(f"`{t}`" for t in (r.unmatched or r.terms)) or "anything in your question"
    lines = [f"No symbol matching {names} was found in the indexed graph of {repo_label}."]
    if r.suggestions:
        lines.append("Did you mean one of these?")
        lines += [f"- `{x.get('qualname') or x.get('name') or x.get('path')}` ({x.get('path')}"
                  + (f":{x['line_start']}" if x.get("line_start") else "") + ")" for x in r.suggestions]
    lines.append("Ask about a specific file, class or function by its exact name, "
                 "for example: “What calls `function_name`?”")
    return "\n".join(lines)


def graph_only_answer(evidence: list[Evidence]) -> str:
    lines = ["**Graph facts** (deterministic summary — no model was used):"]
    for ev in evidence[:8]:
        loc = ev.location()
        first = ev.facts[0] if ev.facts else ""
        lines.append(f"- [{ev.id}] {ev.title}{f' ({loc})' if loc else ''}: {first}")
    return "\n".join(lines)


def answer_question(gq: GraphQueries, meta: MetaClient, question: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("Question is empty.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"Question is too long (max {MAX_QUESTION_CHARS} characters).")
    repo = gq.ensure_exists()
    repo_label = repo.get("url") or repo.get("name") or gq.repo_id
    r = retrieve(gq, question)
    evidence = r.evidence
    base = {"question": question, "matched": r.matched, "intent": r.intent, "unmatched": r.unmatched,
            "suggestions": r.suggestions, "evidence": [e.to_dict() for e in evidence]}

    if not evidence:
        # Nothing in the graph matches: say so, offer close matches, never call the model.
        return {**base, "mode": "no_match", "grounded": False, "citations": [], "complete": True,
                "answer": no_match_answer(r, repo_label)}

    # When other symbols matched, report only unmatched *code-like* names ("errors" is just prose).
    missing = [u for u in r.unmatched if looks_like_identifier(u)]
    base["unmatched"] = missing

    if not meta.configured:
        return {**base, "mode": "graph_only", "grounded": True, "citations": [e.id for e in evidence],
                "complete": True, "model_error": "Meta Model API is not configured (META_API_KEY missing).",
                "answer": graph_only_answer(evidence)}

    messages = build_messages(question, evidence, repo_label, r.intent, r.matched, missing)
    try:
        result = meta.chat(messages)
    except MetaAPIError as exc:
        return {**base, "mode": "graph_only", "grounded": True, "citations": [e.id for e in evidence],
                "complete": True, "model_error": str(exc), "answer": graph_only_answer(evidence)}

    answer, cited, invalid = validate_citations(result.content, evidence)
    if result.truncated:
        answer = answer.rstrip() + "\n\n(Answer cut off: the model reached its output limit.)"
    return {**base, "mode": "model", "model": result.model, "answer": answer, "citations": cited,
            "invalid_citations": invalid, "unsupported_citations": unsupported_citations(answer, evidence),
            "grounded": bool(cited),
            "note": ("Matched only repository-level evidence; no specific symbol matched the question."
                     if all(e.kind == "overview" for e in evidence) else None),
            "truncated": result.truncated, "complete": not result.truncated,
            "usage": result.usage}

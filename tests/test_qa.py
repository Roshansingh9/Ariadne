"""Natural-language Q&A grounding (mocked Meta client, fixture graph)."""

import pytest

from ariadne.meta_client import ChatResult, MetaAPIError
from ariadne.qa import (MAX_EVIDENCE_CHARS, Evidence, answer_question, build_messages, detect_intent,
                        extract_terms, retrieve, retrieve_evidence, unsupported_citations,
                        validate_citations)


class FakeMeta:
    def __init__(self, reply="", error=None, configured=True):
        self.reply, self.error, self.configured = reply, error, configured
        self.messages = None

    finish_reason = "stop"

    def chat(self, messages, **kw):
        self.messages = messages
        if self.error:
            raise self.error
        return ChatResult(content=self.reply, model="muse-spark-1.3", usage={}, finish_reason=self.finish_reason)


def test_extract_terms_prefers_identifiers():
    terms = extract_terms("What calls `Order.validate` and what does the save function do in shop/models.py?")
    assert terms[:2] == ["Order.validate", "shop/models.py"]
    assert "save" in terms and "what" not in terms and "function" not in terms


def test_evidence_contains_definition_callers_impact(memory_queries):
    ev, matched = retrieve_evidence(memory_queries, "What breaks if I change Base.validate?")
    kinds = [e.kind for e in ev]
    assert matched[0] == "Base.validate"
    assert kinds[:2] == ["definition", "callers"] and "impact" in kinds
    d = ev[0]
    assert (d.path, d.line_start, d.line_end) == ("shop/models.py", 11, 12)
    assert "def validate(self)" in d.snippet
    callers = ev[1]
    assert any("Base.save" in f and "shop/models.py:8" in f for f in callers.facts)
    impact = next(e for e in ev if e.kind == "impact")
    assert any("OrderService.place" in f and "inferred" in f for f in impact.facts)


def test_broad_question_uses_overview(memory_queries):
    ev, matched = retrieve_evidence(memory_queries, "Give me an overview of the architecture")
    assert matched == [] and [e.kind for e in ev] == ["overview"]


def test_answer_with_valid_and_invalid_citations(memory_queries):
    meta = FakeMeta(reply="**Graph facts**\n- `Base.save` calls it [E2] at shop/models.py:8 [E1].\n"
                          "- Made up [E99].\n**Interpretation**\n- Fine.")
    r = answer_question(memory_queries, meta, "Who calls Base.validate?")
    assert r["mode"] == "model" and r["grounded"]
    assert r["citations"] == ["E1", "E2"] and r["invalid_citations"] == ["E99"]
    assert "[E99]" not in r["answer"] and "[citation removed]" in r["answer"]
    system, user = meta.messages
    assert "ONLY the numbered evidence" in system["content"]
    assert "insufficient" in system["content"]
    assert "[E1] DEFINITION" in user["content"] and "Who calls Base.validate?" in user["content"]
    assert "shop/models.py:11-12" in user["content"]


def test_no_evidence_does_not_call_model(memory_queries, monkeypatch):
    meta = FakeMeta(reply="should not be used")
    monkeypatch.setattr(memory_queries, "overview", lambda: (_ for _ in ()).throw(AssertionError()))
    r = answer_question(memory_queries, meta, "zzz qqq")
    assert r["mode"] == "no_match" and meta.messages is None
    assert r["evidence"] == [] and r["citations"] == [] and r["grounded"] is False
    assert "No symbol matching `zzz`, `qqq`" in r["answer"]


def test_meta_failure_returns_graph_only(memory_queries):
    r = answer_question(memory_queries, FakeMeta(error=MetaAPIError("Meta Model API rate limit reached (429).", 429)),
                        "Who calls fmt?")
    assert r["mode"] == "graph_only" and "429" in r["model_error"]
    assert "no model was used" in r["answer"] and r["evidence"]


def test_unconfigured_meta_returns_graph_only(memory_queries):
    r = answer_question(memory_queries, FakeMeta(configured=False), "Who calls fmt?")
    assert r["mode"] == "graph_only" and "META_API_KEY" in r["model_error"]


def test_uncited_answer_is_not_grounded(memory_queries):
    r = answer_question(memory_queries, FakeMeta(reply="It is probably fine."), "Who calls fmt?")
    assert r["mode"] == "model" and r["grounded"] is False and r["citations"] == []


def test_question_validation(memory_queries):
    import pytest
    with pytest.raises(ValueError):
        answer_question(memory_queries, FakeMeta(), "   ")
    with pytest.raises(ValueError):
        answer_question(memory_queries, FakeMeta(), "x" * 1001)


def test_evidence_pack_is_bounded():
    big = [Evidence(id=f"E{i}", kind="definition", title="t", snippet="x" * 5000) for i in range(1, 20)]
    _, user = build_messages("q", big, "repo")
    assert len(user["content"]) < MAX_EVIDENCE_CHARS + 2000
    assert "evidence truncated" in user["content"]


def test_validate_citations_dedupes_and_orders():
    ev = [Evidence(id="E1", kind="k", title="t"), Evidence(id="E2", kind="k", title="t")]
    text, cited, invalid = validate_citations("[E2] [E1] [E2] [E3]", ev)
    assert cited == ["E1", "E2"] and invalid == ["E3"] and "[E3]" not in text


# --- symbol-focused retrieval and the no-match path -----------------------------------------

def _never_overview(memory_queries, monkeypatch):
    monkeypatch.setattr(memory_queries, "overview",
                        lambda: (_ for _ in ()).throw(AssertionError("overview must not be used")))


@pytest.mark.parametrize("q,intent", [
    ("What calls send_message?", "callers"), ("who calls fmt", "callers"), ("callers of Base.save", "callers"),
    ("where is fmt used?", "callers"), ("what does main call?", "callees"),
    ("what breaks if I change fmt", "impact"), ("give me an overview of the architecture", "overview"),
    ("explain Order.total", "definition"),
])
def test_detect_intent(q, intent):
    assert detect_intent(q) == intent


def test_unknown_symbol_says_so_without_unrelated_facts(memory_queries, monkeypatch):
    """Regression: "what calls order" on a repo without `order` used to answer from the repo overview."""
    _never_overview(memory_queries, monkeypatch)
    meta = FakeMeta(reply="must not be called")
    r = answer_question(memory_queries, meta, "What calls OrderValidatorFactory?")
    assert r["mode"] == "no_match" and meta.messages is None
    assert r["unmatched"] == ["OrderValidatorFactory"] and r["evidence"] == []
    assert "No symbol matching `OrderValidatorFactory` was found" in r["answer"]
    assert "exact name" in r["answer"]
    for unrelated in ("fmt", "sum_items", "most-called"):         # no repository facts presented as an answer
        assert unrelated not in r["answer"]


def test_no_match_offers_close_symbols_as_suggestions_only(memory_queries, monkeypatch):
    _never_overview(memory_queries, monkeypatch)
    r = answer_question(memory_queries, FakeMeta(reply="unused"), "what calls alidate")
    assert r["mode"] == "no_match" and r["evidence"] == []
    names = {x["qualname"] for x in r["suggestions"]}
    assert names and all("validate" in n.lower() for n in names)
    assert all(x["term"] == "alidate" and x["uid"] for x in r["suggestions"])
    assert "Did you mean" in r["answer"]


def test_known_symbol_question_retrieves_it_and_its_callers(memory_queries, monkeypatch):
    _never_overview(memory_queries, monkeypatch)
    meta = FakeMeta(reply="**Answer** `sum_items` calls `fmt` [E2].")
    r = answer_question(memory_queries, meta, "What calls fmt?")
    assert r["mode"] == "model" and r["intent"] == "callers" and r["matched"] == ["fmt"]
    kinds = [e["kind"] for e in r["evidence"]]
    assert kinds[:2] == ["definition", "callers"] and "overview" not in kinds
    assert any("sum_items" in f for f in r["evidence"][1]["facts"])
    user = meta.messages[1]["content"]
    assert "FOCUS: The question asks which code calls `fmt`" in user and "NOT FOUND" not in user
    assert r["unsupported_citations"] == [] and r["complete"] is True


def test_lowercase_name_of_existing_class_is_matched(memory_queries):
    r = retrieve(memory_queries, "what calls order")
    assert r.matched[0] == "Order" and r.intent == "callers" and r.unmatched == []


def test_absent_callers_are_stated_explicitly(memory_queries):
    ev, _ = retrieve_evidence(memory_queries, "What calls showPrice?")
    assert ev[0].uid == "web/api.ts::showPrice"
    assert "callers in the graph: none found" in ev[0].facts
    assert all(e.kind != "callers" for e in ev)


def test_partially_unmatched_question_tells_the_model(memory_queries):
    meta = FakeMeta(reply="`fmt` is called by `sum_items` [E2]. `GhostThing` is not in the graph.")
    r = answer_question(memory_queries, meta, "What calls fmt and GhostThing?")
    assert r["mode"] == "model" and r["unmatched"] == ["GhostThing"]
    assert "NOT FOUND in the indexed graph: `GhostThing`" in meta.messages[1]["content"]
    assert "Do not substitute other symbols" in meta.messages[0]["content"]


def test_prose_words_are_not_reported_as_missing_symbols(memory_queries):
    r = answer_question(memory_queries, FakeMeta(reply="ok [E1]"), "What calls fmt when errors happen?")
    assert r["unmatched"] == []


def test_truncated_answer_is_marked_incomplete(memory_queries):
    meta = FakeMeta(reply="**Answer** `fmt` is called by [E2")
    meta.finish_reason = "length"
    r = answer_question(memory_queries, meta, "What calls fmt?")
    assert r["truncated"] is True and r["complete"] is False
    assert r["answer"].endswith("(Answer cut off: the model reached its output limit.)")


def test_unsupported_citation_is_flagged():
    ev = [Evidence(id="E1", kind="definition", title="fmt", facts=["Function `fmt`"]),
          Evidence(id="E2", kind="callers", title="callers of fmt", facts=["`sum_items` calls it"])]
    text = "`sum_items` calls `fmt` [E2]. `zebra_router` handles retries [E1].\nPlain prose [E1]."
    assert unsupported_citations(text, ev) == ["E1"]
    assert unsupported_citations("`sum_items` calls it [E2].", ev) == []


def test_system_prompt_demands_concise_grounded_answers():
    system, _ = build_messages("q", [Evidence(id="E1", kind="k", title="t")], "repo")
    for rule in ("ONLY the numbered evidence", "NOT FOUND", "under 150 words", "first sentence",
                 "citing only evidence that states that claim"):
        assert rule in system["content"], rule

"""Meta Model API client tests (mocked HTTP — no live calls)."""

import json

import httpx
import pytest

from ariadne.config import Settings
from ariadne.meta_client import MetaAPIError, MetaClient

KEY = "LLM_123456789_testkeyvaluexxxxxxxxxxxx"


def client(handler, **settings):
    s = Settings(meta_api_key=KEY, meta_model="muse-spark-1.3", **settings)
    return MetaClient(s, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def ok_response(content="hello [E1]"):
    return httpx.Response(200, json={
        "id": "c1", "object": "chat.completion", "created": 1, "model": "muse-spark-1.3",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "refusal": None}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3}})


def test_request_format_matches_meta_docs():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return ok_response()

    r = client(handler).chat([{"role": "user", "content": "hi"}])
    assert seen["url"] == "https://api.meta.ai/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "muse-spark-1.3"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["max_completion_tokens"] == 1500 and seen["body"]["temperature"] == 0.2
    assert r.content == "hello [E1]" and r.usage["prompt_tokens"] == 10


def test_custom_base_url():
    seen = []
    client(lambda req: (seen.append(str(req.url)), ok_response())[1],
           meta_base_url="https://proxy.example/v1").chat([])
    assert seen == ["https://proxy.example/v1/chat/completions"]


def test_retries_on_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"},
                                  json={"error": {"message": "slow down", "type": "rate_limit_error"}})
        return ok_response()

    assert client(handler).chat([]).content.startswith("hello")
    assert len(calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retryable_errors(status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": "nope", "type": "invalid_request_error"}})

    with pytest.raises(MetaAPIError) as exc:
        client(handler).chat([])
    assert len(calls) == 1 and exc.value.status == status
    assert KEY not in str(exc.value)


def test_gives_up_after_max_attempts():
    with pytest.raises(MetaAPIError) as exc:
        client(lambda req: httpx.Response(503, json={"error": {"message": "overloaded"}})).chat([])
    assert exc.value.status == 503


def test_timeout_and_bad_shapes():
    def timeout(req):
        raise httpx.ReadTimeout("t", request=req)
    with pytest.raises(MetaAPIError, match="timed out"):
        client(timeout).chat([])
    with pytest.raises(MetaAPIError, match="Unexpected response"):
        client(lambda req: httpx.Response(200, json={"foo": 1})).chat([])
    with pytest.raises(MetaAPIError, match="no content"):
        client(lambda req: ok_response(content="")).chat([])


def _length_response(content=""):
    return httpx.Response(200, json={
        "model": "muse-spark-1.3",
        "choices": [{"index": 0, "finish_reason": "length",
                     "message": {"role": "assistant", "content": content, "refusal": None}}],
        "usage": {"completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 20}}})


def test_reasoning_budget_exhaustion_retries_with_larger_budget():
    budgets = []

    def handler(req):
        budgets.append(json.loads(req.content)["max_completion_tokens"])
        return _length_response() if len(budgets) == 1 else ok_response("ok")

    r = client(handler).chat([], max_completion_tokens=20)
    assert r.content == "ok" and budgets == [20, 1024]


def test_reasoning_budget_exhaustion_twice_gives_clear_error():
    with pytest.raises(MetaAPIError, match="token budget") as exc:
        client(lambda req: _length_response()).chat([])
    assert exc.value.kind == "length"


def test_truncated_content_is_flagged():
    r = client(lambda req: _length_response("partial answer")).chat([])
    assert r.content == "partial answer" and r.truncated


def test_partial_answer_is_retried_with_larger_budget():
    budgets = []

    def handler(req):
        budgets.append(json.loads(req.content)["max_completion_tokens"])
        return _length_response("partial") if len(budgets) == 1 else ok_response("complete answer [E1]")

    r = client(handler).chat([], max_completion_tokens=1500)
    assert budgets == [1500, 4500] and r.content == "complete answer [E1]" and not r.truncated


def test_partial_answer_kept_if_retry_fails():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return _length_response("partial answer")
        return httpx.Response(400, json={"error": {"message": "bad", "type": "invalid_request_error"}})

    r = client(handler).chat([])
    assert len(calls) == 2 and r.content == "partial answer" and r.truncated


def test_longer_partial_wins_when_both_truncated():
    calls = []

    def handler(req):
        calls.append(1)
        return _length_response("short" if len(calls) == 1 else "a much longer partial answer")

    r = client(handler).chat([])
    assert r.content == "a much longer partial answer" and r.truncated


def test_unconfigured_never_calls_network():
    def handler(req):
        raise AssertionError("network must not be used")
    m = MetaClient(Settings(), transport=httpx.MockTransport(handler))
    assert not m.configured
    with pytest.raises(MetaAPIError, match="not configured"):
        m.chat([])


def test_retries_stop_when_time_budget_is_spent(monkeypatch):
    import ariadne.meta_client as mc
    monkeypatch.setattr(mc, "TOTAL_BUDGET_SECONDS", 5.0)        # less than backoff + margin
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(MetaAPIError):
        client(handler).chat([])
    assert len(calls) == 1

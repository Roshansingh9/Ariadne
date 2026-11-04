"""Meta Model API client (chat completions).

Verified against Meta's developer docs (dev.meta.ai, Nov 2026):
  POST https://api.meta.ai/v1/chat/completions
  Authorization: Bearer <key>
  body: {model, messages, temperature, max_completion_tokens, ...}
  response: choices[0].message.content
  errors: {"error": {message, type, param, code}}; retry 429/500/503 with backoff.

This is the only model provider Ariadne uses. There is no fallback.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Settings

RETRYABLE = {429, 500, 503}
# Whole chat() call (all retries) must fit well inside the 300 s Vercel function limit.
TOTAL_BUDGET_SECONDS = 200.0
MAX_ATTEMPTS = 3


class MetaAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, kind: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.kind = kind


@dataclass
class ChatResult:
    content: str
    model: str
    usage: dict
    finish_reason: Optional[str] = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


# max_completion_tokens counts reasoning tokens as well as visible output (Meta docs),
# so a small budget can be consumed entirely by reasoning. Retry once with a larger one.
BUDGET_RETRY_FACTOR = 3
BUDGET_RETRY_CAP = 6000
BUDGET_RETRY_FLOOR = 1024


class _BudgetExhausted(Exception):
    pass


class MetaClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.BaseTransport] = None,
                 sleep=time.sleep):
        self.settings = settings
        self._transport = transport
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return self.settings.meta_configured

    def chat(self, messages: list[dict], *, temperature: float = 0.2,
             max_completion_tokens: int = 1500) -> ChatResult:
        if not self.configured:
            raise MetaAPIError("Meta Model API is not configured (set META_API_KEY).", kind="not_configured")
        self._deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        bigger = min(max(max_completion_tokens * BUDGET_RETRY_FACTOR, BUDGET_RETRY_FLOOR), BUDGET_RETRY_CAP)
        try:
            first = self._chat_once(messages, temperature, max_completion_tokens)
        except _BudgetExhausted:
            first = None
        if first is not None:
            # Partial answer cut off at the limit: retry once with a larger budget if time allows.
            # Keep the partial answer (flagged `truncated`) if the retry cannot improve on it.
            if not first.truncated or bigger <= max_completion_tokens or self._remaining() < 10:
                return first
            try:
                second = self._chat_once(messages, temperature, bigger)
            except (_BudgetExhausted, MetaAPIError):
                return first
            return second if (not second.truncated or len(second.content) >= len(first.content)) else first
        # No content at all: the whole budget went to reasoning.
        if bigger <= max_completion_tokens:
            raise MetaAPIError("Meta model used its whole token budget on reasoning and returned no "
                               "answer.", kind="length") from None
        if self._remaining() < 10:
            raise MetaAPIError("Meta model used its whole token budget on reasoning and there is no "
                               "time left to retry.", kind="length") from None
        try:
            return self._chat_once(messages, temperature, bigger)
        except _BudgetExhausted:
            raise MetaAPIError("Meta model used its whole token budget on reasoning and returned no "
                               "answer, even after a retry with a larger budget.", kind="length") from None

    def _chat_once(self, messages: list[dict], temperature: float, max_completion_tokens: int) -> ChatResult:
        url = f"{self.settings.meta_base_url}/chat/completions"
        body = {
            "model": self.settings.meta_model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_completion_tokens,
        }
        headers = {"Authorization": f"Bearer {self.settings.meta_api_key}",
                   "Content-Type": "application/json"}
        timeout = httpx.Timeout(max(5.0, min(self.settings.meta_timeout, self._remaining())), connect=10.0)
        last: Optional[MetaAPIError] = None
        with httpx.Client(timeout=timeout, transport=self._transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    resp = client.post(url, json=body, headers=headers)
                except httpx.TimeoutException as exc:
                    raise MetaAPIError("Meta Model API request timed out.", kind="timeout") from exc
                except httpx.HTTPError as exc:
                    raise MetaAPIError(f"Could not reach the Meta Model API ({type(exc).__name__}).",
                                       kind="network") from exc
                if resp.status_code == 200:
                    return self._parse(resp)
                last = self._error(resp)
                if resp.status_code not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                    raise last
                wait = self._backoff(resp, attempt)
                if self._remaining() < wait + 10:
                    raise last
                self._sleep(wait)
        raise last or MetaAPIError("Meta Model API request failed.")

    def _remaining(self) -> float:
        deadline = getattr(self, "_deadline", None)
        return TOTAL_BUDGET_SECONDS if deadline is None else deadline - time.monotonic()

    @staticmethod
    def _backoff(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 8.0)
            except ValueError:
                pass
        return min(0.75 * (2 ** attempt) + random.random() * 0.5, 8.0)

    @staticmethod
    def _error(resp: httpx.Response) -> MetaAPIError:
        kind, message = None, ""
        try:
            err = resp.json().get("error") or {}
            kind = err.get("type") or err.get("code")
            message = str(err.get("message") or "")[:300]
        except (ValueError, AttributeError):
            pass
        friendly = {
            401: "Meta Model API rejected the API key (401).",
            402: "Meta Model API billing issue (402).",
            403: "The Meta API key lacks access to this model (403).",
            404: "Meta model or endpoint not found (404) — check META_MODEL / META_API_BASE_URL.",
            429: "Meta Model API rate limit reached (429).",
            504: "Meta Model API timed out on the server (504).",
        }.get(resp.status_code, f"Meta Model API error (HTTP {resp.status_code}).")
        if message and resp.status_code not in (401,):
            friendly = f"{friendly} {message}"
        return MetaAPIError(friendly, status=resp.status_code, kind=kind)

    def _parse(self, resp: httpx.Response) -> ChatResult:
        try:
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MetaAPIError("Unexpected response shape from the Meta Model API.", kind="bad_response") from exc
        content = msg.get("content")
        finish = choice.get("finish_reason")
        if not content and finish == "length":
            raise _BudgetExhausted()
        if not content:
            refusal = msg.get("refusal")
            raise MetaAPIError(f"Meta model returned no content{': ' + refusal if refusal else ''}.",
                               kind="empty")
        return ChatResult(content=content, model=data.get("model") or self.settings.meta_model,
                          usage=data.get("usage") or {}, finish_reason=finish)

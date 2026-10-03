"""Anthropic adapter (official SDK, Messages API)."""
from __future__ import annotations

from typing import Any, Iterator, Optional

import anthropic as sdk  # absolute import: this module is explain.providers.anthropic

from ..config import reasoning_effort
from ..schema import (InferenceRequest, InferenceResult, InferenceUsage,
                      ProviderCapabilities)
from .base import InferenceProvider

DEFAULT_MAX_TOKENS = 16000  # the Messages API requires max_tokens


def _g(obj: Any, *path: str) -> Any:
    for p in path:
        obj = getattr(obj, p, None) if obj is not None else None
    return obj


class AnthropicProvider(InferenceProvider):
    name = "anthropic"

    # shared REASONING_EFFORT -> output_config.effort
    _EFFORT = {"none": "low", "minimal": "low", "low": "low", "medium": "medium",
               "high": "high", "xhigh": "xhigh", "max": "max"}

    def __init__(self, api_key: str, model: str):
        super().__init__(api_key, model)
        self.client = sdk.Anthropic(api_key=api_key, max_retries=0)  # EXPLAIN owns retries
        self._effort_ok = True  # flips off if the model rejects output_config.effort

    def capabilities(self) -> ProviderCapabilities:
        # Anthropic reports no separate thinking-token count: thinking is inside
        # output_tokens, so reasoning_tokens stays UNAVAILABLE rather than guessed.
        return ProviderCapabilities(
            token_usage=True, cached_tokens=True, reasoning_tokens=False,
            tool_tokens=False, response_id=True, streaming_ttft=True, count_tokens=True)

    def _params(self, req: InferenceRequest) -> dict:
        p: dict[str, Any] = {
            "model": req.model or self.model,
            "max_tokens": req.max_output_tokens or DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system:
            p["system"] = ([{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
                           if req.cache_system else req.system)
        if req.temperature is not None:  # newer models reject non-default sampling
            p["temperature"] = req.temperature
        effort = self._EFFORT.get(reasoning_effort() or "")
        if effort and self._effort_ok:
            p["output_config"] = {"effort": effort}
        return p

    def _rejects_effort(self, e: Exception) -> bool:
        """Models without effort support 400; drop it once and retry."""
        if (self._effort_ok and isinstance(e, sdk.BadRequestError)
                and "effort" in str(e).lower()):
            self._effort_ok = False
            return True
        return False

    def _call(self, req):
        try:
            return self.client.messages.create(**self._params(req))
        except sdk.BadRequestError as e:
            if not self._rejects_effort(e):
                raise
            return self._call(req)

    def _stream_call(self, req) -> Iterator[tuple[str, Any]]:
        try:
            yield from self._stream_once(req)
        except sdk.BadRequestError as e:
            if not self._rejects_effort(e):
                raise
            yield from self._stream_once(req)

    def _stream_once(self, req) -> Iterator[tuple[str, Any]]:
        with self.client.messages.stream(**self._params(req)) as s:
            for text in s.text_stream:
                yield text, None
            yield "", s.get_final_message()

    def count_tokens(self, req) -> Optional[int]:
        try:
            kw: dict[str, Any] = {"model": req.model or self.model,
                                  "messages": [{"role": "user", "content": req.prompt}]}
            if req.system:
                kw["system"] = req.system
            return self.client.messages.count_tokens(**kw).input_tokens
        except Exception:  # noqa: BLE001
            return None

    def normalize_usage(self, raw, req) -> InferenceUsage:
        u = _g(raw, "usage")
        fresh, read, write = (_g(u, "input_tokens"), _g(u, "cache_read_input_tokens"),
                              _g(u, "cache_creation_input_tokens"))
        out = _g(u, "output_tokens")
        # Anthropic's input_tokens excludes cached tokens; OpenAI/Gemini include them.
        # Normalize to "total input" so the common schema means one thing.
        total_in = None if fresh is None else fresh + (read or 0) + (write or 0)
        return InferenceUsage(
            provider=self.name, model=_g(raw, "model") or req.model or self.model,
            request_id=_g(raw, "id"),
            input_tokens=total_in, output_tokens=out,
            cached_tokens=read, cache_write_tokens=write,  # writes are billed at the input rate here
            reasoning_tokens=None, tool_tokens=None,
            total_tokens=None if total_in is None or out is None else total_in + out,
            output_includes_reasoning=True)

    def normalize_response(self, raw, req, text=None) -> InferenceResult:
        usage = self.normalize_usage(raw, req)
        if text is None:
            text = "".join(b.text for b in (_g(raw, "content") or []) if getattr(b, "type", "") == "text")
        reason = _g(raw, "stop_reason")
        cat = _g(raw, "stop_details", "category")
        return InferenceResult(
            provider=self.name, model=usage.model or self.model, text=text, usage=usage,
            finish_reason=f"{reason}:{cat}" if cat else reason)

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (sdk.APIConnectionError, sdk.RateLimitError, sdk.InternalServerError)):
            return True
        return isinstance(exc, sdk.APIStatusError) and exc.status_code in (408, 409, 429, 529)
